/**
 * 市场资讯 widget — 外部新闻快讯流 (后端 /api/info/news 多源聚合)。
 *
 * 自取数据 (与 MonitorWidget 同模式): 不依赖 OverviewMarket 聚合,
 * 按 assetType 切换来源; 单源失败在后端 errors 中说明, 前端展示可读错误。
 */
import { useQuery } from '@tanstack/react-query'
import { Loader2, Newspaper, RefreshCw } from 'lucide-react'
import { api, type DashboardMarket } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { SectionTitle } from './shared'

function timeOf(iso?: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

export function NewsCard({ assetType }: { assetType: DashboardMarket }) {
  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: QK.infoNews(assetType),
    queryFn: () => api.infoNews(assetType),
    staleTime: 120_000,
    refetchInterval: 5 * 60_000,
  })

  return (
    <section className="flex h-full flex-col overflow-hidden rounded-card border border-border bg-surface/80 p-1.5 shadow-[0_1px_2px_hsl(var(--border)/0.4)] backdrop-blur-sm transition-shadow hover:shadow-[0_2px_8px_hsl(var(--border)/0.5)]">
      <div className="flex items-center justify-between gap-2">
        <SectionTitle icon={Newspaper} title="市场资讯" hint={data?.sources?.join(' / ') || undefined} />
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-muted hover:bg-accent/10 hover:text-accent"
          title="刷新资讯"
        >
          <RefreshCw className={`h-3 w-3 ${isFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>
      <div className="mt-1 min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />加载资讯…
          </div>
        ) : isError ? (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 text-xs text-muted">
            <span>资讯加载失败</span>
            <button onClick={() => refetch()} className="rounded-btn border border-border px-2 py-0.5 text-[11px] hover:text-foreground">重试</button>
          </div>
        ) : !data?.items?.length ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 px-2 text-center text-[11px] text-muted">
            <span>暂无资讯</span>
            {data?.errors?.length ? <span className="text-warning/80">{data.errors[0]}</span> : null}
          </div>
        ) : (
          <ul className="divide-y divide-border/60">
            {data.items.map((item, idx) => {
              const title = (
                <span className="line-clamp-2 flex-1 text-[11px] leading-snug text-secondary group-hover:text-foreground">
                  {item.title}
                </span>
              )
              return (
                <li key={`${item.source}-${idx}`} className="group flex items-start gap-1.5 px-0.5 py-1">
                  <span className="mt-0.5 shrink-0 rounded bg-elevated px-1 py-px font-mono text-[9px] text-muted">{item.source}</span>
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer noopener" className="flex min-w-0 flex-1 gap-1.5">
                      {title}
                    </a>
                  ) : title}
                  <span className="mt-0.5 shrink-0 font-mono text-[9px] text-muted">{timeOf(item.published_at)}</span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </section>
  )
}
