// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type Preferences } from '@/lib/api'
import { AutoSyncSettingsCard } from './AutoSyncSettingsCard'

vi.mock('@/lib/api', () => ({ api: {
  preferences: vi.fn(), dataStatus: vi.fn(), updateMarketAutoSync: vi.fn(),
} }))

let host: HTMLDivElement
let root: Root
let client: QueryClient

const prefs = {
  crypto_auto_sync: { enabled: true, interval_minutes: 15 },
  us_auto_sync: { enabled: false, interval_minutes: 15 },
} as Preferences

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.preferences).mockResolvedValue(prefs)
  vi.mocked(api.dataStatus).mockResolvedValue({
    next_crypto_auto_run: '2026-11-15T12:00:00', next_us_auto_run: null,
  } as never)
  vi.mocked(api.updateMarketAutoSync).mockResolvedValue({ enabled: true, interval_minutes: 30 })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(market: 'crypto' | 'us' = 'crypto') {
  await act(async () => root.render(
    <QueryClientProvider client={client}><AutoSyncSettingsCard market={market} /></QueryClientProvider>,
  ))
  await settle()
}
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
}
async function setSelect(value: string) {
  const select = host.querySelector('select')!
  expect(select).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, value)
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
}

it('渲染当前档位并以新档位立即提交', async () => {
  await render('crypto')
  expect(host.textContent).toContain('15 分钟')
  await setSelect('30')
  expect(api.updateMarketAutoSync).toHaveBeenCalledWith('crypto', true, 30)
})

it('开关切换立即提交 enabled 取反', async () => {
  await render('crypto')
  const checkbox = host.querySelector('input[type="checkbox"]') as HTMLInputElement
  expect(checkbox).toBeTruthy()
  expect(checkbox.checked).toBe(true) // crypto_auto_sync 默认桩为 enabled
  await act(async () => checkbox.click())
  await settle()
  expect(api.updateMarketAutoSync).toHaveBeenCalledWith('crypto', false, 15)
})

it('crypto 文案为 7×24 语义', async () => {
  await render('crypto')
  expect(host.textContent).toContain('7×24')
  expect(host.textContent).not.toContain('盘中')
})

it('us 文案为盘中语义并说明盘后兜底', async () => {
  await render('us')
  expect(host.textContent).toContain('盘中')
  expect(host.textContent).toContain('兜底')
})

it('展示下次运行时间 (仅启用时)', async () => {
  await render('crypto')
  expect(host.textContent).toContain('下次同步')
  expect(host.textContent).toContain('2026-11-15T12:00:00')
  await render('us') // us_auto_sync disabled → 不显示下次运行
  expect(host.textContent).not.toContain('下次同步')
})
