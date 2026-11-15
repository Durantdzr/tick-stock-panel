// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type PipelineJobSummary } from '@/lib/api'
import { SyncJobLogCard } from './SyncJobLogCard'

vi.mock('@/lib/api', () => ({ api: { pipelineJobs: vi.fn() } }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

const jobs = [
  {
    id: 'j1', status: 'succeeded', stage: 'done', progress: 100, stage_pct: 0,
    started_at: '2026-11-15T10:00:00', finished_at: '2026-11-15T10:01:00', duration_s: 60.5,
    result: { instruments: 2, daily: { ok: true, rows: 3, symbols: 1, errors: [] } },
    error: null, label: 'us_sync', trigger: 'auto',
  },
  {
    id: 'j2', status: 'failed', stage: 'daily', progress: 50, stage_pct: 0,
    started_at: '2026-11-15T09:00:00', finished_at: '2026-11-15T09:02:00', duration_s: 120,
    result: null,
    error: 'us 同步失败: boom 网络失败', label: 'us_sync', trigger: 'manual',
  },
] as unknown as PipelineJobSummary[]

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.pipelineJobs).mockResolvedValue({ active_id: null, jobs })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(market: 'crypto' | 'us' = 'us') {
  await act(async () => root.render(
    <QueryClientProvider client={client}><SyncJobLogCard market={market} /></QueryClientProvider>,
  ))
  await settle()
}
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
}

it('按市场 label 拉取并渲染触发徽章与失败原因', async () => {
  await render('us')
  expect(api.pipelineJobs).toHaveBeenCalledWith(20, 'us_sync')
  expect(host.textContent).toContain('自动')
  expect(host.textContent).toContain('手动')
  expect(host.textContent).toContain('成功')
  expect(host.textContent).toContain('失败')
  expect(host.textContent).toContain('boom 网络失败')
})

it('渲染分步摘要 (维表/日K行数)', async () => {
  await render('us')
  expect(host.textContent).toContain('维表 2')
  expect(host.textContent).toContain('日K 3')
})

it('空列表显示空态', async () => {
  vi.mocked(api.pipelineJobs).mockResolvedValue({ active_id: null, jobs: [] })
  await render('crypto')
  expect(api.pipelineJobs).toHaveBeenCalledWith(20, 'crypto_sync')
  expect(host.textContent).toContain('暂无同步记录')
})

it('部分失败 (errors 非空) 在成功记录上显示摘要', async () => {
  vi.mocked(api.pipelineJobs).mockResolvedValue({ active_id: null, jobs: [{
    ...jobs[0],
    result: { instruments: 2, daily: { ok: true, rows: 1, symbols: 2, errors: ['MSFT: 网络失败'] } },
  }] as unknown as PipelineJobSummary[] })
  await render('us')
  expect(host.textContent).toContain('MSFT: 网络失败')
})

it('节假日跳过记录显示跳过标记', async () => {
  vi.mocked(api.pipelineJobs).mockResolvedValue({ active_id: null, jobs: [{
    ...jobs[0], trigger: 'daily',
    result: { ok: true, skipped: 'us_holiday' },
  }] as unknown as PipelineJobSummary[] })
  await render('us')
  expect(host.textContent).toContain('节假日跳过')
})
