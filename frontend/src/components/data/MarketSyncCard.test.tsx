// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { MarketSyncCard } from './MarketSyncCard'

vi.mock('@/lib/api', () => ({ api: {
  cryptoStatus: vi.fn(), usStatus: vi.fn(), cryptoSync: vi.fn(), usSync: vi.fn(),
  cryptoUniverse: vi.fn(), usUniverse: vi.fn(),
  updateCryptoUniverse: vi.fn(), updateUsUniverse: vi.fn(),
  cryptoConcepts: vi.fn(), updateCryptoConcepts: vi.fn(),
  preferences: vi.fn(), dataStatus: vi.fn(), updateMarketAutoSync: vi.fn(),
} }))
vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

import { toast } from '@/components/Toast'

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.resetAllMocks()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  vi.mocked(api.cryptoStatus).mockResolvedValue(null as never)
  vi.mocked(api.usStatus).mockResolvedValue(null as never)
  vi.mocked(api.updateCryptoUniverse).mockResolvedValue({ spot: [], futures: [] })
  vi.mocked(api.cryptoConcepts).mockResolvedValue({ DeFi: ['ETHUSDT'] })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

async function render(market: 'crypto' | 'us' = 'crypto') {
  await act(async () => root.render(
    <QueryClientProvider client={client}><MarketSyncCard market={market} /></QueryClientProvider>,
  ))
  await settle()
}
async function settle() {
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
}
async function click(text: string) {
  const button = [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === text)!
  expect(button).toBeTruthy()
  await act(async () => button.click())
  await settle()
}
async function fillTextarea(value: string) {
  const ta = host.querySelector('textarea')!
  expect(ta).toBeTruthy()
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(ta, value)
    ta.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
}

it('白名单加载完成前禁用保存; 加载后预填, 保存保留未编辑一侧', async () => {
  let resolveUni!: (v: { spot: string[]; futures: string[] }) => void
  vi.mocked(api.cryptoUniverse).mockImplementation(() =>
    new Promise(r => { resolveUni = r }))
  await render('crypto')
  await click('白名单')

  // 竞态防护: 数据未返回时保存必须禁用 (否则空初值保存会静默清空另一侧白名单)
  const saveBtn = () => [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存')!
  expect(saveBtn()?.disabled).toBe(true)

  await act(async () => resolveUni({ spot: ['BTCUSDT'], futures: ['ETHUSDT'] }))
  await settle()
  expect(saveBtn()?.disabled).toBe(false)
  expect(host.querySelector('textarea')!.value).toBe('BTCUSDT')

  await fillTextarea('BTCUSDT\nSOLUSDT')
  await click('保存')
  expect(api.updateCryptoUniverse).toHaveBeenCalledWith(['BTCUSDT', 'SOLUSDT'], ['ETHUSDT'])
})

it('白名单查询失败时保存保持禁用并给出可读提示', async () => {
  vi.mocked(api.cryptoUniverse).mockRejectedValue(new Error('网络失败'))
  await render('crypto')
  await click('白名单')
  await settle()
  const saveBtn = () => [...host.querySelectorAll('button')].find(b => b.textContent?.trim() === '保存')!
  expect(saveBtn()?.disabled).toBe(true)
  expect(host.textContent).toContain('加载失败')
  expect(api.updateCryptoUniverse).not.toHaveBeenCalled()
})

it('自动同步保存失败时给出错误提示 (不静默)', async () => {
  const { AutoSyncSettingsCard } = await import('./AutoSyncSettingsCard')
  vi.mocked(api.preferences).mockResolvedValue({
    crypto_auto_sync: { enabled: true, interval_minutes: 15 },
  } as never)
  vi.mocked(api.dataStatus).mockResolvedValue({ next_crypto_auto_run: null } as never)
  vi.mocked(api.updateMarketAutoSync).mockRejectedValue(new Error('网络失败'))

  await act(async () => root.render(
    <QueryClientProvider client={client}><AutoSyncSettingsCard market="crypto" /></QueryClientProvider>,
  ))
  await settle()
  const select = host.querySelector('select')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')!.set!.call(select, '30')
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  await settle()
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('保存失败'), 'error')
})
