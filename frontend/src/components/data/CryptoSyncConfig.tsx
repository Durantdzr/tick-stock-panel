/**
 * 加密货币数据同步面板 (数据页 crypto 卡片的设置弹窗)。
 *
 * 独立于 A 股盘后管道: 触发 POST /api/crypto/sync (后台执行, 同步锁单飞),
 * GET /api/crypto/status 查最近一次结果。默认白名单见 docs/crypto-market.md。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'

import { api } from '@/lib/api'

const BTN = 'inline-flex items-center gap-1.5 h-8 px-3 rounded-btn text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'

export function CryptoSyncConfig({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient()
  const status = useQuery({ queryKey: ['crypto-sync-status'], queryFn: api.cryptoStatus, refetchInterval: (q) => (q.state.data?.running ? 3000 : false) })
  const sync = useMutation({
    mutationFn: (withMinute: boolean) => api.cryptoSync(withMinute),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['crypto-sync-status'] })
      qc.invalidateQueries({ queryKey: ['data-status'] })
    },
  })
  const st = status.data
  const running = st?.running === true

  return (
    <div className="flex flex-col gap-4 p-1 text-sm">
      <div className="text-xs leading-5 text-muted">
        从币安同步白名单标的（现货 + U 本位合约，BTCUSDT.P 后缀为合约）的日K、指标与可选分钟K；
        每日 08:05 自动增量同步，服务未开机错过时在此手动补。
        白名单经 <code className="text-accent">PUT /api/crypto/universe</code> 修改。
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={`${BTN} bg-accent text-white hover:bg-accent/90`} disabled={running || sync.isPending} onClick={() => sync.mutate(false)}>
          <RefreshCw className={`h-3.5 w-3.5 ${running ? 'animate-spin' : ''}`} /> 同步日K + 指标
        </button>
        <button type="button" className={`${BTN} border border-border text-secondary hover:text-foreground`} disabled={running || sync.isPending} onClick={() => sync.mutate(true)}>
          同步（含分钟K，较慢）
        </button>
      </div>

      {st && (
        <div className="rounded-btn border border-border bg-surface/60 px-3 py-2.5 text-xs leading-5 text-secondary">
          {running ? (
            <span className="text-accent">同步进行中…（开始于 {st.started_at}）</span>
          ) : st.ok === true ? (
            <>
              上次同步完成 {st.finished_at} · 耗时 {st.elapsed_s}s · 标的 {st.instruments} 个 ·
              日K {st.daily?.rows ?? 0} 行{st.minute ? ` · 分钟K ${st.minute.rows ?? 0} 行` : ''}
              {(st.daily?.errors?.length ?? 0) > 0 && <div className="text-warning mt-1">部分标的失败: {st.daily?.errors?.slice(0, 3).join('；')}</div>}
            </>
          ) : (
            <span className="text-danger">上次同步失败: {st.error}</span>
          )}
        </div>
      )}

      <div className="flex justify-end">
        <button type="button" className={`${BTN} border border-border text-secondary hover:text-foreground`} onClick={onClose}>关闭</button>
      </div>
    </div>
  )
}
