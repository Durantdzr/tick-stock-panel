/**
 * 市场同步卡 (数据页 crypto/us 常驻卡片)。
 *
 * 由原 CryptoSyncConfig / USSyncConfig 弹窗升级而来: 手动同步两按钮 +
 * 最近一次结果摘要 + 白名单 (crypto 另加概念映射) 配置入口。同步经任务库
 * 跟踪 (label=<market>_sync), 成功后失效日志卡与 data-status。
 */
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, Settings2 } from 'lucide-react'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const BTN = 'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
const DIALOG = 'fixed inset-0 z-50 flex items-center justify-center bg-black/40'
const DIALOG_BOX = 'w-[520px] max-w-[92vw] rounded-card border border-border bg-elevated p-4 shadow-lg'
const TA = 'w-full h-40 rounded-btn border border-border bg-surface p-2 text-xs font-mono text-foreground'

type Market = 'crypto' | 'us'

type SyncStatus = {
  ok?: boolean | null
  running?: boolean
  skipped?: string
  started_at?: string
  finished_at?: string
  elapsed_s?: number
  instruments?: number
  error?: string
  daily?: { ok?: boolean; rows?: number; symbols?: number; errors?: string[] }
  minute?: { ok?: boolean; rows?: number; symbols?: number; errors?: string[] } | null
}

function parseSymbols(text: string): string[] {
  return [...new Set(text.split(/[\s,;，；]+/).map(s => s.trim().toUpperCase()).filter(Boolean))]
}

/** 加载中/失败占位弹层: 保存保持禁用 — 数据未就绪时绝不允许提交,
 * 否则空初值保存会把未加载的一侧白名单作为显式空列表提交, 静默清空用户配置。 */
function DialogPending({ title, failed, onClose }: {
  title: string
  failed: boolean
  onClose: () => void
}) {
  return (
    <div className={DIALOG} onClick={onClose}>
      <div className={DIALOG_BOX} onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium text-foreground mb-2">{title}</h3>
        <div className={`w-full h-40 rounded-btn border flex items-center justify-center text-xs ${failed ? 'border-danger/40 bg-danger/5 text-danger' : 'border-border bg-surface text-muted'}`}>
          {failed ? '加载失败，请关闭后重试' : '加载中…'}
        </div>
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" className={`${BTN} border border-border text-secondary`} onClick={onClose}>取消</button>
          <button type="button" className={`${BTN} bg-accent text-white`} disabled>保存</button>
        </div>
      </div>
    </div>
  )
}

/** 通用编辑弹层: 数据就绪后才挂载 (textarea 初值 useState 快照依赖此约定)。 */
function TextDialog({ title, initial, onClose, onSave }: {
  title: string
  initial: string
  onClose: () => void
  onSave: (text: string) => void
}) {
  const [text, setText] = useState(initial)
  return (
    <div className={DIALOG} onClick={onClose}>
      <div className={DIALOG_BOX} onClick={(e) => e.stopPropagation()}>
        <h3 className="text-sm font-medium text-foreground mb-2">{title}</h3>
        <textarea className={TA} value={text}
          onChange={(e) => setText(e.target.value)} spellCheck={false} />
        <div className="mt-3 flex justify-end gap-2">
          <button type="button" className={`${BTN} border border-border text-secondary`} onClick={onClose}>取消</button>
          <button type="button" className={`${BTN} bg-accent text-white hover:bg-accent/90`}
            onClick={() => onSave(text)}>保存</button>
        </div>
      </div>
    </div>
  )
}

export function MarketSyncCard({ market }: { market: Market }) {
  const qc = useQueryClient()
  const isCrypto = market === 'crypto'
  const statusKey = isCrypto ? 'crypto-sync-status' : 'us-sync-status'

  const status = useQuery({
    queryKey: [statusKey],
    queryFn: isCrypto ? api.cryptoStatus : api.usStatus,
    refetchInterval: (q) => (q.state.data?.running ? 3000 : false),
  })
  const sync = useMutation({
    mutationFn: (withMinute: boolean) => (isCrypto ? api.cryptoSync(withMinute) : api.usSync(withMinute)),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: [statusKey] })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      qc.invalidateQueries({ queryKey: QK.marketSyncJobs(`${market}_sync`) })
      toast(`同步已启动 (job ${res.job_id})，进度见下方日志`, 'success')
    },
    onError: (e: unknown) => toast(`同步启动失败: ${(e as Error)?.message ?? e}`, 'error'),
  })
  const st = status.data as SyncStatus | undefined
  const running = st?.running === true

  // ── 配置弹层 ──
  const [dialog, setDialog] = useState<null | 'spot' | 'futures' | 'us' | 'concepts'>(null)
  const universeQ = useQuery<{ spot?: string[]; futures?: string[]; us?: string[] }>({
    queryKey: ['market-universe', market],
    queryFn: async () => (isCrypto ? await api.cryptoUniverse() : await api.usUniverse()),
    enabled: dialog === 'spot' || dialog === 'futures' || dialog === 'us',
  })
  const conceptsQ = useQuery({
    queryKey: ['market-concepts', market],
    queryFn: api.cryptoConcepts,
    enabled: dialog === 'concepts',
  })
  useEffect(() => {
    if (universeQ.error) toast(`白名单读取失败: ${(universeQ.error as Error).message}`, 'error')
    if (conceptsQ.error) toast(`概念映射读取失败: ${(conceptsQ.error as Error).message}`, 'error')
  }, [universeQ.error, conceptsQ.error])
  // 概念映射空数据也允许打开编辑 (后端对空映射 422 拒绝保存); 加载中/失败由弹层门控兜底

  const saveUniverse = useMutation({
    mutationFn: async (payload: { spot?: string[]; futures?: string[]; us?: string[] }): Promise<void> => {
      if (isCrypto) await api.updateCryptoUniverse(payload.spot ?? [], payload.futures ?? [])
      else await api.updateUsUniverse(payload.us ?? [])
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['market-universe', market] })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
      setDialog(null)
      toast('白名单已保存，下次同步按新清单拉取', 'success')
    },
    onError: (e: unknown) => toast(`白名单保存失败: ${(e as Error)?.message ?? e}`, 'error'),
  })
  const saveConcepts = useMutation({
    mutationFn: (concepts: Record<string, string[]>) => api.updateCryptoConcepts(concepts),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['market-concepts', market] })
      setDialog(null)
      toast('概念映射已保存，看板概念排名按新映射聚合', 'success')
    },
    onError: (e: unknown) => toast(`概念映射保存失败: ${(e as Error)?.message ?? e}`, 'error'),
  })

  const initialText = (() => {
    if (dialog === 'spot') return (universeQ.data as { spot?: string[] } | undefined)?.spot?.join('\n') ?? ''
    if (dialog === 'futures') return (universeQ.data as { futures?: string[] } | undefined)?.futures?.join('\n') ?? ''
    if (dialog === 'us') return (universeQ.data as { us?: string[] } | undefined)?.us?.join('\n') ?? ''
    if (dialog === 'concepts') return JSON.stringify(conceptsQ.data ?? {}, null, 2)
    return ''
  })()

  const dialogTitle = dialog === 'spot' ? '现货白名单 (每行一个交易对，如 BTCUSDT)'
    : dialog === 'futures' ? '合约白名单 (每行一个 symbol，如 BTCUSDT，内部自动加 .P)'
      : dialog === 'us' ? '美股白名单 (每行一个代码，如 AAPL)'
        : dialog === 'concepts' ? '概念映射 (JSON：概念 → 成员数组)'
          : ''

  const onDialogSave = (text: string) => {
    if (dialog === 'concepts') {
      let parsed: unknown
      try {
        parsed = JSON.parse(text)
      } catch {
        toast('概念映射不是合法 JSON', 'error')
        return
      }
      if (typeof parsed !== 'object' || parsed === null ||
        !Object.values(parsed as Record<string, unknown>).every(v => Array.isArray(v))) {
        toast('概念映射格式应为 { "概念名": ["成员", ...] }', 'error')
        return
      }
      saveConcepts.mutate(parsed as Record<string, string[]>)
      return
    }
    const symbols = parseSymbols(text)
    if (dialog === 'spot') {
      if (symbols.length === 0) { toast('现货白名单不能为空', 'error'); return }
      saveUniverse.mutate({ spot: symbols, futures: (universeQ.data as { futures?: string[] })?.futures ?? [] })
    } else if (dialog === 'futures') {
      const spot = (universeQ.data as { spot?: string[] })?.spot ?? []
      if (spot.length === 0 && symbols.length === 0) { toast('现货与合约白名单不能同时为空', 'error'); return }
      saveUniverse.mutate({ spot, futures: symbols })
    } else if (dialog === 'us') {
      if (symbols.length === 0) { toast('美股白名单不能为空', 'error'); return }
      saveUniverse.mutate({ us: symbols })
    }
  }

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-2 mb-2">
        <h3 className="text-sm font-medium text-foreground">
          {isCrypto ? '加密货币同步' : '美股同步'}
        </h3>
        <div className="flex items-center gap-1.5">
          {isCrypto && (
            <button type="button" className={`${BTN} border border-border text-secondary`}
              onClick={() => setDialog('concepts')}>
              <Settings2 className="h-3.5 w-3.5" /> 概念映射
            </button>
          )}
          <button type="button" className={`${BTN} border border-border text-secondary`}
            onClick={() => setDialog(isCrypto ? 'spot' : 'us')}>
            <Settings2 className="h-3.5 w-3.5" /> 白名单
          </button>
        </div>
      </div>
      <p className="text-xs leading-5 text-muted">
        {isCrypto
          ? '从币安同步白名单标的（现货 + U 本位合约，.P 后缀为合约）的日K、指标与可选分钟K；每日 08:05 自动增量同步。'
          : '从东方财富同步美股：全市场维表（供搜索）+ 白名单标的日K、指标与可选分钟K；每交易日美东 16:35 自动同步（节假日跳过）。'}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className={`${BTN} bg-accent text-white hover:bg-accent/90`}
          disabled={running || sync.isPending} onClick={() => sync.mutate(false)}>
          <RefreshCw className={`h-3.5 w-3.5 ${running || sync.isPending ? 'animate-spin' : ''}`} />
          同步日K + 指标
        </button>
        <button type="button" className={`${BTN} border border-border text-secondary hover:text-foreground`}
          disabled={running || sync.isPending} onClick={() => sync.mutate(true)}>
          同步（含分钟K，较慢）
        </button>
      </div>

      {st && (
        <div className="mt-3 rounded-btn border border-border bg-surface/60 px-3 py-2.5 text-xs leading-5 text-secondary">
          {running ? (
            <span className="text-accent">同步进行中…（开始于 {st.started_at}）</span>
          ) : st.skipped === 'us_holiday' ? (
            <span>上次触发为美东节假日，已自动跳过（{st.finished_at}）</span>
          ) : st.ok === true ? (
            <>
              上次同步完成 {st.finished_at} · 耗时 {st.elapsed_s}s · 维表 {st.instruments ?? '-'} 只 ·
              日K {st.daily?.rows ?? 0} 行{st.minute ? ` · 分钟K ${st.minute.rows ?? 0} 行` : ''}
              {(st.daily?.errors?.length ?? 0) > 0 && (
                <div className="text-warning mt-1">部分标的失败: {st.daily?.errors?.slice(0, 3).join('；')}</div>
              )}
            </>
          ) : (
            <span className="text-danger">上次同步失败: {st.error}</span>
          )}
        </div>
      )}

      {dialog && (dialog === 'concepts'
        ? (conceptsQ.isLoading || conceptsQ.isError)
          ? <DialogPending title={dialogTitle} failed={conceptsQ.isError || false} onClose={() => setDialog(null)} />
          : <TextDialog title={dialogTitle} initial={initialText} onClose={() => setDialog(null)} onSave={onDialogSave} />
        : (universeQ.isLoading || universeQ.isError)
          ? <DialogPending title={dialogTitle} failed={universeQ.isError || false} onClose={() => setDialog(null)} />
          : <TextDialog title={dialogTitle} initial={initialText} onClose={() => setDialog(null)} onSave={onDialogSave} />)}
    </div>
  )
}
