# 个股分析页适配 crypto/us 设计（短设计）

日期：2026-11-16
状态：已批准（会话内确认）
规模：小改动（4 文件 + 测试）

## 意图

个股分析页（`/stock-analysis`，ext 页）当前只接受 A股 标的。适配 crypto/us：
技术分析链路（日K/关键价位/AI 的 K 线部分）后端已按 `resolve_asset_type` 分流，
打通搜索入口、AI 提示词分支与价格提醒门控即可。

## 目标 / 非目标

**目标**
- 搜索框扩为全市场（`stock,index,crypto,us`），结果行加市场徽标，选中携带 asset_type。
- AI 四维分析对 crypto/us：财务文件跳过扫描（现只跳 etf/index）+ 提示词分支明确
  「无财务/基本面维度是常态，勿编造 ROE/营收」（仿既有 ETF 分支）。
- 价格提醒按钮对 crypto/us 置灰 + 原因提示（后端 `realtime_monitor` 已 fail-closed 400）。
- A股 全部行为零改动。

**非目标**
- 不做市场切换器（单标的分析页，全市场单框搜索）。
- 不为 crypto/us 开实时监控/通知（trading_rules 未开放，保持 fail-closed）。
- 不改 AI 报告存储（本地 localStorage，市场无关）。

**反目标**
- 不复制 K 线/价位/AI 流水线做平行页面。

## 方案取舍

- 全市场单框（选定）vs 按市场分 tab：单标的分析页无市场级上下文，单框更直接。
- 价格提醒置灰给原因（选定）vs 无声隐藏：比无声隐藏更友好；原因复用
  `trading_rules.deny_reason` 语义（「实时监控依赖 A 股交易时段相位机…」）。
- 不为 crypto/us 建独立技术分析页：违反「不平行实现」。

## 改动清单

1. `backend/app/services/stock_analyzer.py`：
   - `analyze_stock_stream` 财务跳过集合 `{etf,index}` → `{etf,index,crypto,us}`；
   - `_build_user_prompt` 加 `crypto` / `us` 提示词分支（无财务是常态，勿编造数字）。
2. `frontend/src/components/financials/StockFinancialSearch.tsx`：
   - `onSelect` 第三参透传 `asset_type`（向后兼容）；crypto/us 结果行加市场徽标。
3. `frontend/src/custom/stock-analysis/extension.tsx`：
   - `assetTypes="stock,index,crypto,us"`；记录选中 asset_type；
   - 副标题/提示按市场变体（crypto/us：「技术面为主 · 无财务维度」）；
   - 价格提醒按钮 crypto/us 置灰 + title/toast 原因。
4. 测试：后端 `_build_user_prompt` 分支断言（crypto/us 不出现「接入中」、含常态文案）；
   前端搜索徽标用例；浏览器实测 crypto 标的全流程。

## 验证

`uv run pytest tests/test_stock_analyzer_prompt.py（新增）+ 既有相关`；`pnpm vitest run` + `pnpm build`；浏览器实测 ETHUSDT 搜索→分析→价位图。
