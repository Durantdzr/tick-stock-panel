/**
 * 市场同步日志卡 (数据页 crypto/us)。
 *
 * 数据来自任务库 /api/pipeline/jobs?label=<market>_sync — 手动/定时/自动三路
 * 同一份持久化历史 (重启不丢, 上限 500 条)。有 pending/running 行时 3s 轮询,
 * 否则 60s。失败原因与部分失败摘要如实展示, 不把空值伪装成功。
 */
import { useQuery } from '@tanstack/react-query'

import { api, type PipelineJobSummary } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

const TRIGGER_LABEL: Record<string, string> = { manual: '手动', daily: '定时', auto: '自动' }

const BADGE_OK = 'px-1.5 rounded bg-bull/10 text-bull text-[10px]'
const BADGE_FAIL = 'px-1.5 rounded bg-danger/10 text-danger text-[10px]'
const BADGE_RUN = 'px-1.5 rounded bg-accent/10 text-accent text-[10px]'
const BADGE_MUTED = 'px-1.5 rounded bg-surface text-secondary text-[10px]'

function TriggerBadge({ trigger }: { trigger?: string | null }) {
  const text = TRIGGER_LABEL[trigger ?? ''] ?? '未知'
  return <span className={trigger === 'auto' ? BADGE_MUTED : BADGE_MUTED}>{text}</span>
}

function StatusBadge({ status }: { status: string }) {
  if (status === 'succeeded') return <span className={BADGE_OK}>成功</span>
  if (status === 'failed') return <span className={BADGE_FAIL}>失败</span>
  return <span className={BADGE_RUN}>{status === 'running' ? '进行中' : '排队中'}</span>
}

function stepSummary(j: PipelineJobSummary): string {
  const result = (j.result ?? {}) as Record<string, unknown>
  const parts: string[] = []
  if (typeof result.instruments === 'number') parts.push(`维表 ${result.instruments}`)
  const daily = result.daily as Record<string, unknown> | undefined
  if (daily && typeof daily.rows === 'number') parts.push(`日K ${daily.rows}`)
  const minute = result.minute as Record<string, unknown> | null | undefined
  if (minute && typeof minute.rows === 'number') parts.push(`分钟K ${minute.rows}`)
  return parts.join(' · ')
}

function JobRow({ j }: { j: PipelineJobSummary }) {
  const time = j.finished_at ?? j.started_at ?? ''
  const result = (j.result ?? {}) as Record<string, unknown>
  const daily = result.daily as Record<string, unknown> | undefined
  const dailyErrors = Array.isArray(daily?.errors) ? (daily!.errors as string[]) : []
  const skippedHoliday = result.skipped === 'us_holiday'
  return (
    <div className="border-t border-border/60 py-1.5 first:border-t-0 text-xs leading-5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-secondary tabular-nums">{time}</span>
        <TriggerBadge trigger={j.trigger} />
        <StatusBadge status={j.status} />
        {skippedHoliday && <span className={BADGE_MUTED}>节假日跳过</span>}
        {j.duration_s != null && <span className="text-muted text-[10px]">{j.duration_s}s</span>}
        <span className="text-muted text-[10px]">{stepSummary(j)}</span>
      </div>
      {j.status === 'failed' && j.error && (
        <div className="text-danger mt-0.5 break-all">{j.error}</div>
      )}
      {j.status === 'succeeded' && dailyErrors.length > 0 && (
        <div className="text-warning mt-0.5 break-all">
          部分失败: {dailyErrors.slice(0, 3).join('；')}
          {dailyErrors.length > 3 && ` 等 ${dailyErrors.length} 条`}
        </div>
      )}
    </div>
  )
}

export function SyncJobLogCard({ market }: { market: 'crypto' | 'us' }) {
  const label = `${market}_sync`
  const jobsQ = useQuery({
    queryKey: QK.marketSyncJobs(label),
    queryFn: () => api.pipelineJobs(20, label),
    refetchInterval: (q) =>
      q.state.data?.jobs?.some((x) => x.status === 'pending' || x.status === 'running') ? 3000 : 60_000,
  })
  const jobs = jobsQ.data?.jobs ?? []

  return (
    <div className="rounded-card border border-border bg-surface p-4">
      <div className="flex items-center gap-2 mb-2">
        <h3 className="text-sm font-medium text-foreground">同步日志</h3>
        <span className="text-muted text-[10px]">最近 20 条 · 手动/定时/自动</span>
      </div>
      {jobsQ.isLoading ? (
        <div className="text-xs text-muted py-3">加载中…</div>
      ) : jobs.length === 0 ? (
        <div className="text-xs text-muted py-3">暂无同步记录</div>
      ) : (
        <div className="max-h-72 overflow-y-auto">
          {jobs.map((j) => <JobRow key={j.id} j={j} />)}
        </div>
      )}
    </div>
  )
}
