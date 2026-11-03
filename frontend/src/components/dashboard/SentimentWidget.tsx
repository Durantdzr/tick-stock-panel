/**
 * 外部市场情绪 widget — crypto: 恐惧贪婪指数 + 合约资金费率 (/api/info/sentiment)。
 *
 * 决策辅助口径: 恐惧贪婪 0-100 (alternative.me, 免 Key); 资金费率正值=多头付
 * 资金费 (多头拥挤), 负值反之。外部源不可达时展示可读错误, 不伪造数值。
 */
import { useQuery } from '@tanstack/react-query'
import { Loader2, RefreshCw, Waves } from 'lucide-react'
import { api, type DashboardMarket, type FundingRateRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { fmtPrice, pctClass, SectionTitle } from './shared'

const FNG_COLORS: Record<string, string> = {
  极度恐惧: '#ef4444', 恐惧: '#f97316', 中性: '#eab308', 贪婪: '#22c55e', 极度贪婪: '#16a34a',
}

function fngColor(label?: string): string {
  return (label && FNG_COLORS[label]) || '#eab308'
}

function FundingRow({ row }: { row: FundingRateRow }) {
  const rate = row.funding_rate
  // 费率为 8h 结算的小数 (0.0001 = 0.01%)
  const pct = rate != null ? rate * 100 : null
  return (
    <div className="flex items-center justify-between gap-2 px-0.5 py-0.5">
      <span className="min-w-0 flex-1 truncate text-[11px] text-secondary">{row.name || row.symbol}</span>
      <span className={`font-mono text-[11px] ${pctClass(rate ?? 0)}`}>
        {pct != null ? `${pct >= 0 ? '+' : ''}${fmtPrice(pct, 4)}%` : '—'}
      </span>
    </div>
  )
}

export function SentimentCard({ assetType }: { assetType: DashboardMarket }) {
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: QK.infoSentiment(assetType),
    queryFn: () => api.infoSentiment(assetType),
    staleTime: 5 * 60_000,
    refetchInterval: 15 * 60_000,
  })

  const fng = data?.fng?.current
  const history = [...(data?.fng?.history ?? [])].slice(0, 14).reverse() // 旧→新, 便于迷你条形
  const funding = (data?.funding ?? []).slice(0, 8)

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="flex items-center justify-between gap-2">
        <SectionTitle icon={Waves} title="市场情绪" hint="恐惧贪婪 / 资金费率" />
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted hover:bg-accent/10 hover:text-accent"
          title="刷新"
        >
          <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>
      <div className="mt-1 min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />加载情绪指标…
          </div>
        ) : isError ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">情绪数据加载失败</div>
        ) : !fng ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 px-2 text-center text-[11px] text-muted">
            <span>外部情绪源暂不可达</span>
            {data?.fng?.error ? <span className="text-warning/80">稍后自动重试</span> : null}
          </div>
        ) : (
          <>
            <div className="flex items-center gap-3 rounded-lg bg-elevated/45 px-2 py-1.5">
              <div
                className="grid h-11 w-11 shrink-0 place-items-center rounded-full border-2 text-sm font-bold"
                style={{ color: fngColor(fng.label), borderColor: `${fngColor(fng.label)}66` }}
              >
                {fng.value}
              </div>
              <div className="min-w-0 flex-1">
                <div className="text-xs font-medium" style={{ color: fngColor(fng.label) }}>{fng.label}</div>
                <div className="mt-1 flex h-4 items-end gap-px" title="近 14 天恐惧贪婪指数">
                  {history.map((p, i) => (
                    <div
                      key={`${p.date}-${i}`}
                      className="w-full min-w-[3px] rounded-sm"
                      style={{ height: `${Math.max(8, p.value)}%`, background: `${fngColor(p.label)}55` }}
                    />
                  ))}
                </div>
              </div>
            </div>
            <div className="mt-1.5 border-t border-border pt-1.5">
              <div className="mb-0.5 px-0.5 text-[10px] font-medium text-muted">合约资金费率 (8h, |费率| 高在前)</div>
              {funding.length === 0 ? (
                <div className="px-0.5 py-1 text-[11px] text-muted">资金费率暂不可用</div>
              ) : (
                funding.map(row => <FundingRow key={row.symbol} row={row} />)
              )}
            </div>
          </>
        )}
      </div>
    </section>
  )
}
