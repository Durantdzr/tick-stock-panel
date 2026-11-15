/**
 * 市场自动同步设置卡 (数据页 crypto/us)。
 *
 * 开关与档位 (5/15/30/60 分钟) 变更立即提交 PUT /api/settings/preferences/market-auto-sync,
 * 后端即时重注册 APScheduler interval job (crypto 7×24; us 仅交易日盘中门控,
 * 收盘后由每日 16:35 定时任务兜底)。"下次同步"来自 /api/data/status 的 next_*_auto_run。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { toast } from '@/components/Toast'

const INTERVALS = [5, 15, 30, 60]
const SELECT = 'h-7 rounded-btn border border-border bg-surface px-1.5 text-xs text-foreground'

export function AutoSyncSettingsCard({ market }: { market: 'crypto' | 'us' }) {
  const qc = useQueryClient()
  const prefsQ = useQuery({ queryKey: QK.preferences, queryFn: api.preferences })
  const statusQ = useQuery({ queryKey: QK.dataStatus, queryFn: api.dataStatus })
  const update = useMutation({
    mutationFn: ({ enabled, interval }: { enabled: boolean; interval: number }) =>
      api.updateMarketAutoSync(market, enabled, interval),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: QK.preferences })
      qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
    onError: (e: unknown) => toast(`自动同步保存失败: ${(e as Error)?.message ?? e}`, 'error'),
  })

  const prefs = prefsQ.data
  const current = market === 'crypto' ? prefs?.crypto_auto_sync : prefs?.us_auto_sync
  const enabled = current?.enabled ?? false
  const interval = current?.interval_minutes ?? 15
  const nextRun = market === 'crypto'
    ? statusQ.data?.next_crypto_auto_run ?? null
    : statusQ.data?.next_us_auto_run ?? null

  const hint = market === 'crypto'
    ? '7×24 每 N 分钟同步一次（日K增量 + 分钟K近窗 + 指标重算）'
    : '仅美股交易日盘中每 N 分钟同步，收盘后由每日任务兜底'

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium text-foreground">自动同步</h3>
          <span className={`px-1.5 rounded text-[10px] ${enabled ? 'bg-accent/10 text-accent' : 'bg-surface text-muted'}`}>
            {enabled ? '已开启' : '已关闭'}
          </span>
        </div>
        <label className="inline-flex items-center cursor-pointer">
          <input
            type="checkbox"
            className="sr-only peer"
            checked={enabled}
            disabled={update.isPending}
            onChange={(e) => update.mutate({ enabled: e.target.checked, interval })}
          />
          <span className="w-8 h-4.5 rounded-full bg-border peer-checked:bg-accent relative transition-colors after:content-[''] after:absolute after:top-0.5 after:left-0.5 after:w-3.5 after:h-3.5 after:rounded-full after:bg-white after:transition-transform peer-checked:after:translate-x-3.5" />
        </label>
      </div>
      <p className="mt-1.5 text-xs leading-5 text-muted">
        {hint.replace('N 分钟', `${interval} 分钟`)}
      </p>
      <div className="mt-2 flex items-center gap-2 text-xs text-secondary">
        <span>同步间隔</span>
        <select
          className={SELECT}
          value={interval}
          disabled={update.isPending}
          onChange={(e) => update.mutate({ enabled, interval: Number(e.target.value) })}
        >
          {INTERVALS.map((m) => <option key={m} value={m}>{m} 分钟</option>)}
        </select>
        {enabled && nextRun && (
          <span className="text-muted ml-auto text-[10px]">下次同步 {nextRun}</span>
        )}
      </div>
    </div>
  )
}
