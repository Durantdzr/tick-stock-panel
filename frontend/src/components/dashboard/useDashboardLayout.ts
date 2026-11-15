/**
 * 看板布局持久化 hook — 后端偏好加载 + 本地改动防抖落盘 (按市场分键)。
 *
 * 返回 { items, setItems }:
 * - items: 本地有改动取本地, 否则取后端该市场保存布局(规范化), 再退内置默认。
 * - setItems: 本地改动入口; 800ms 防抖保存到 PUT /preferences/dashboard-layout
 *   (带 market), 与该市场内置默认布局完全一致时存 null(清除)。
 * - 切换市场时本地未保存改动作废 (市场间布局相互独立)。
 */
import { useEffect, useMemo, useState } from 'react'
import { api, type DashboardMarket } from '@/lib/api'
import { usePreferences } from '@/lib/useSharedQueries'
import { DEFAULT_LAYOUT, DEFAULT_LAYOUT_CRYPTO, DEFAULT_LAYOUT_US, isKnownWidgetType, minSizeOf } from './registry'
import { normalizeLayout, toBlob, type DashboardItem } from './layout'

const SAVE_DEBOUNCE_MS = 800

export function useDashboardLayout(market: DashboardMarket = 'stock'): {
  items: DashboardItem[]
  setItems: (next: DashboardItem[]) => void
} {
  const { data: prefs } = usePreferences()
  const defaults = market === 'crypto' ? DEFAULT_LAYOUT_CRYPTO : market === 'us' ? DEFAULT_LAYOUT_US : DEFAULT_LAYOUT
  const savedBlob = market === 'crypto' ? prefs?.dashboard_layout_crypto : market === 'us' ? prefs?.dashboard_layout_us : prefs?.dashboard_layout
  const saved = useMemo(
    // normalizeLayout: blob → clamp/去重/消毒; 未知组件剔除 (后端是 blob 权威副本)
    () => normalizeLayout(savedBlob ?? undefined, defaults, isKnownWidgetType, minSizeOf),
    [savedBlob, market],
  )
  const [local, setLocal] = useState<DashboardItem[] | null>(null)

  // 切换市场时丢弃上个市场的本地未保存改动
  useEffect(() => {
    setLocal(null)
  }, [market])

  useEffect(() => {
    if (local === null) return
    const blob = toBlob(local)
    const isDefault = JSON.stringify(blob.items) === JSON.stringify(defaults)
    const timer = window.setTimeout(() => {
      api.saveDashboardLayout(isDefault ? null : blob, market)
        .catch(() => { /* 保存失败静默: 布局仍在本会话生效, 下次刷新回退上次保存 */ })
    }, SAVE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [local, market])

  return { items: local ?? saved, setItems: setLocal }
}
