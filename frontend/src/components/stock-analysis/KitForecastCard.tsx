import { useEffect, useRef, useState } from 'react'
import { Loader2, TrendingUp, AlertTriangle } from 'lucide-react'
import { api, type KitPrediction } from '@/lib/api'
import { toast } from '@/components/Toast'

/**
 * KiT 扩散模型预测 — 触发按钮 hook + 进度/摘要卡。
 *
 * 流式一次性结果不入 TanStack Query 缓存(设计偏差已记录), 用 hook 状态持有,
 * symbol 切换时重置并清空图表叠加(onForecast(null))。
 *
 * 阶段文案与后端 NDJSON stage 事件一一对应(env/data/model/sampling)。
 */

const STAGE_TEXT: Record<string, string> = {
  env: '检查 KiT 环境',
  data: '加载日线数据',
  model: '加载模型 (首次较慢)',
  sampling: '扩散采样中 (CPU 约 1~3 分钟)',
}

export type KitPhase = 'idle' | 'streaming' | 'done' | 'error'

export function useKitPrediction(
  symbol: string,
  onForecast: (p: KitPrediction | null) => void,
) {
  const [phase, setPhase] = useState<KitPhase>('idle')
  const [stageText, setStageText] = useState('')
  const [result, setResult] = useState<KitPrediction | null>(null)
  const runIdRef = useRef(0)

  // 切股重置: 清空结果 + 图表叠加
  useEffect(() => {
    runIdRef.current += 1
    setPhase('idle')
    setStageText('')
    setResult(null)
    onForecast(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbol])

  const run = async () => {
    if (!symbol || phase === 'streaming') return
    const myRun = ++runIdRef.current
    setPhase('streaming')
    setStageText(STAGE_TEXT.env)
    try {
      let lastPrediction: KitPrediction | null = null
      for await (const chunk of api.kitPredictStream(symbol)) {
        if (runIdRef.current !== myRun) return // 已切股, 丢弃本轮
        switch (chunk.type) {
          case 'stage':
            setStageText(STAGE_TEXT[chunk.stage ?? ''] ?? chunk.detail ?? '处理中')
            break
          case 'done':
            if (chunk.prediction) {
              lastPrediction = chunk.prediction
              setResult(chunk.prediction)
              onForecast(chunk.prediction)
            }
            break
          case 'error':
            setPhase('error')
            setStageText('')
            toast(chunk.message ?? 'KiT 预测失败', 'error')
            return
          default:
            break // ping 等未知类型静默忽略
        }
      }
      // 流正常结束但无 prediction → 视为失败, 避免卡死
      if (lastPrediction) setPhase('done')
      else {
        setPhase('error')
        setStageText('')
        toast('KiT 预测未返回结果', 'error')
      }
    } catch {
      // HTTP 校验错误(api 内已 toast)或网络错误
      setPhase('error')
      setStageText('')
    }
  }

  return { run, phase, stageText, result }
}

const pct = (v: number) => `${(v * 100).toFixed(1)}%`
const signedPct = (v: number) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`

/** 进度/摘要卡: 挂在看板图表下方。按钮在页头(见 extension.tsx)。 */
export function KitForecastCard({ kit }: { kit: ReturnType<typeof useKitPrediction> }) {
  const { phase, stageText, result } = kit
  if (phase === 'idle' || phase === 'streaming') {
    if (phase !== 'streaming') return null
    return (
      <div className="flex items-center gap-2 px-4 py-2.5 border-t border-border/40 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-purple-400" />
        <span>{stageText || '处理中'}</span>
      </div>
    )
  }
  if (phase === 'error') {
    return (
      <div className="flex items-center gap-2 px-4 py-2.5 border-t border-border/40 text-xs text-bear">
        <AlertTriangle className="h-3.5 w-3.5" />
        <span>KiT 预测失败, 请查看通知或重试</span>
      </div>
    )
  }
  if (!result) return null
  const s = result.summary
  return (
    <div className="px-4 py-2.5 border-t border-border/40">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="inline-flex items-center gap-1 font-medium text-purple-300">
          <TrendingUp className="h-3.5 w-3.5" />
          KiT 未来 20 日
        </span>
        <span className="text-muted">
          收益区间
          <span className={`font-mono font-bold ml-1 ${s.median_return >= 0 ? 'text-bull' : 'text-bear'}`}>
            {signedPct(s.p10_return)} ~ {signedPct(s.p90_return)}
          </span>
          <span className="text-foreground/80">
            (中位 <span className={`font-mono ${s.median_return >= 0 ? 'text-bull' : 'text-bear'}`}>{signedPct(s.median_return)}</span>)
          </span>
        </span>
        <span className="text-muted">
          最大可能涨幅 <span className="font-mono text-bull">{pct(s.median_max_gain)}</span>
          {' · '}最大可能回撤 <span className="font-mono text-bear">{pct(s.median_max_drawdown)}</span>
        </span>
        <span className="ml-auto text-[10px] text-muted/60">
          KiT-L (284M) · 训练截至 {result.model.cutoff} · 仅供研究, 非投资建议
        </span>
      </div>
    </div>
  )
}
