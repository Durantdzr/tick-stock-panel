/**
 * 数据页 crypto/us 市场区块 — 分市场切换后的整页内容。
 *
 * 组装: 表统计条 (dataStatus 的 {crypto,us}_enriched/_instruments) +
 * 同步卡 (手动同步/白名单/概念) + 自动同步设置 + 同步日志。
 * A股 区块 (pages/Data.tsx 既有内容) 与本组件互斥渲染。
 */
import { useDataStatus } from '@/lib/useSharedQueries'

import { AutoSyncSettingsCard } from './AutoSyncSettingsCard'
import { MarketSyncCard } from './MarketSyncCard'
import { SyncJobLogCard } from './SyncJobLogCard'

type Market = 'crypto' | 'us'

type TableStats = {
  rows?: number
  symbols_covered?: number
  earliest_date?: string | null
  latest_date?: string | null
} | null

function StatStrip({ stats, instruments, market }: {
  stats: TableStats
  instruments: TableStats
  market: Market
}) {
  const label = market === 'crypto' ? '加密货币' : '美股'
  const cells: Array<[string, string]> = [
    ['维表标的', instruments?.symbols_covered != null ? `${instruments.symbols_covered}` : '—'],
    ['指标行数', stats?.rows != null ? `${stats.rows}` : '—'],
    ['最新交易日', stats?.latest_date ?? '—'],
    ['数据起点', stats?.earliest_date ?? '—'],
  ]
  return (
    <div className="rounded-card border border-border bg-surface px-4 py-3 grid grid-cols-2 md:grid-cols-4 gap-3">
      {cells.map(([k, v]) => (
        <div key={k}>
          <div className="text-[10px] text-muted">{label} · {k}</div>
          <div className="text-sm font-medium text-foreground tabular-nums">{v}</div>
        </div>
      ))}
    </div>
  )
}

export function MarketSyncSection({ market }: { market: Market }) {
  const status = useDataStatus({ refetchInterval: 60_000 })
  const s = status.data
  const stats = market === 'crypto' ? s?.crypto_enriched : s?.us_enriched
  const instruments = market === 'crypto' ? s?.crypto_instruments : s?.us_instruments

  return (
    <div className="space-y-4">
      <StatStrip stats={stats as TableStats} instruments={instruments as TableStats} market={market} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <MarketSyncCard market={market} />
        <AutoSyncSettingsCard market={market} />
      </div>
      <SyncJobLogCard market={market} />
    </div>
  )
}
