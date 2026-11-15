// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { IndexTicker } from './widgets'

let host: HTMLDivElement
let root: Root

const item = { symbol: 'ETHUSDT', name: 'ETH', last_price: 1811.48, change_pct: -0.0002, change_amount: null }

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

async function render(node: React.ReactNode) {
  await act(async () => root.render(<MemoryRouter>{node}</MemoryRouter>))
}

it('默认 (A股 指数条) 渲染为 /indices 跳转链接', async () => {
  await render(<IndexTicker item={{ ...item, symbol: '000001.SH', name: '上证指数' }} />)
  const link = host.querySelector('a')
  expect(link).toBeTruthy()
  expect(link!.getAttribute('href')).toBe('/indices?symbol=000001.SH')
})

it('提供 onOpen 时渲染为按钮并回调 (crypto 主流币卡走预览弹窗, 指数页不认 crypto 代码)', async () => {
  const onOpen = vi.fn()
  await render(<IndexTicker item={item} onOpen={onOpen} />)
  expect(host.querySelector('a')).toBeNull()
  const btn = host.querySelector('button')
  expect(btn).toBeTruthy()
  expect(btn!.textContent).toContain('ETHUSDT')
  await act(async () => btn!.click())
  expect(onOpen).toHaveBeenCalledWith(item)
})
