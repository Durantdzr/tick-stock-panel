# 加密货币市场（crypto）

本项目已支持加密货币作为第二个市场（asset_type `crypto`），数据源为币安公开行情
（现货 + U 本位合约），复用现有 enriched 指标管道与策略筛选引擎。本文档说明
配置方式、数据口径与已知限制。

## 快速开始

1. **同步数据**：面板启动后调用 `POST /api/crypto/sync`（或等待每日 08:05 定时任务）。
   首次同步会拉取白名单标的的全部历史日K（现货 2017 年起）并重算 enriched。
2. **筛选**：策略页资产类型切换到「加密货币」，内置指标（MA/MACD/RSI/动量等）可直接使用。
3. **查看**：主界面看板头部切换到「加密货币」市场（独立布局与组件集：主流币卡、
   涨跌榜、成交额榜、恐惧贪婪/资金费率、市场资讯）；自选页搜索交易对（如 `BTCUSDT`、
   `ETHUSDT.P`）加入自选；个股详情页查看 K 线。

## 白名单

`data/user_data/crypto_universe.json`（默认主流币 ~10 个现货 + 对应合约）：

```json
{"spot": ["BTCUSDT", "ETHUSDT"], "futures": ["BTCUSDT"]}
```

- API：`GET/PUT /api/crypto/universe`；显式空数组表示清空该市场（不允许双空）。
- 同步时与交易所 `exchangeInfo`（TRADING 状态 + `quoteAsset=USDT`）取交集；
  非 USDT 计价或已下架条目自动跳过。

## 数据口径（与股票契约的差异，全部显式声明）

| 项 | 口径 | 说明 |
| --- | --- | --- |
| volume | **基础币数量**（BTC 等） | 非股票「手」口径；策略中请按相对量使用 |
| amount | **USDT 计价额** | 非股票「元」口径 |
| change_pct | 小数制 | 与全站一致（0.0052 = 0.52%） |
| 日线 date | UTC 开盘时间的北京日期 | 币安日线以 UTC 00:00 开盘（= 北京 08:00 同日），与 UTC 日期一致 |
| 分钟 datetime | 北京墙钟 naive | UTC+8h，与全站分钟口径一致（CONTRIBUTING §3.3） |
| 复权/股本/涨跌停 | 不存在 | enriched 的 `turnover_rate`、`consecutive_limit_*` 列恒缺失（fail-soft） |
| 合约 symbol | `BTCUSDT.P` | 与现货 `BTCUSDT` 命名空间隔离（交易所 symbol 同名） |

## 数据源与路由

- 币安插件（`backend/app/plugins/binance/`）声明 `asset_types: [crypto]`，
  **不进入 A 股 7 能力路由候选**；数据源经偏好 `crypto_data_provider`（默认 `binance`）路由，
  可在 `data/user_data/preferences.json` 修改。
- 公共行情免 Key；可选在 `.env` 配 `BINANCE_API_KEY` 提升限额。
- 部分地区访问币安需代理；插件 `availability()` 探针失败时在设置页灰显并给出原因。

## 同步

- `POST /api/crypto/sync`（可选 `{"with_minute": true}` 同步近 3 天 1 分钟K），后台执行；
  `GET /api/crypto/status` 查询进度与最近一次结果。
- 定时任务：每日 08:05（北京时间，UTC 日线收盘后）增量同步。
- 增量语义：按每标的本地最新日K日期续拉；enriched 全量重算但只落盘受影响日期之后的分区。

## 交易规则注册表与主界面市场切换

交易模式差异的**单一权威**是 `backend/app/services/trading_rules.py`：每个资产类型
声明交易规则（`t_plus_1` / `price_limit` / `lot_size` / `sessions` / `day_boundary` /
`volume_unit` / `change_pct_window`）与功能可用性（`features`）。消费方式：

- **后端强制限制（fail-closed）**：回测全部入口（`/api/backtest/run`、
  `/api/backtest/factor/*`、`/api/backtest/strategy/*`）与监控规则创建
  （`/api/monitor_rules`）对 crypto 返回 400 并给出原因；分钟策略沿用 screener 既有限制。
- **前端门控唯一来源**：`GET /api/markets/rules` 返回全部市场规则，看板组件按
  registry `markets` 声明 + `feature` 键叠加过滤，不在组件里硬编码能力清单。
- 未来若撮合引擎把 T+0/无涨跌停/任意精度参数化并补足时间对齐测试，只需翻转
  注册表里 `crypto.backtest` 的开关。

**主界面（看板）市场切换**：头部「A股 / 加密货币」切换器持久化到偏好
`dashboard_market`；布局按市场独立存储（`dashboard_layout` / `dashboard_layout_crypto`）。
crypto 总览数据来自 `/api/overview/market?asset_type=crypto`（`services/crypto_overview.py`）：

- 涨跌/成交额/榜单/主流币卡：币安 24hr 快照（全 USDT 现货对，**24 小时滚动窗口口径**，
  非 A 股日历日口径）；快照不可达时 fail-soft 降级本地 enriched 最新日（白名单标的）。
- 趋势（站上均线/新高新低）：只来自本地 enriched，反映已同步白名单范围。
- 涨跌停/换手率/板块/概念/情绪雷达对 crypto 恒为零或空；KPI 与趋势组件按
  `market === 'crypto'` 分流渲染有口径的指标。

## 外部资讯与情绪（决策辅助）

`backend/app/services/external_info.py` + `/api/info/news`、`/api/info/sentiment`
（可用性由注册表 `news` / `market_sentiment` feature 声明）：

- 新闻快讯（`GET /api/info/news?asset_type=`）：A 股＝新浪财经 7×24、东方财富快讯；
  crypto＝CoinDesk、Cointelegraph RSS。多源聚合 fail-soft（单源失败在 `errors` 说明），
  进程内 TTL 缓存 5 分钟，不进入实时热路径。看板「市场资讯」组件展示。
- 恐惧贪婪指数（alternative.me，免 Key）+ U 本位合约资金费率（经币安插件
  `get_funding_rates`，仅返回白名单合约、按 |费率| 降序）。看板「外部市场情绪」
  组件展示；外部源不可达时显示可读错误，不伪造数值。

## 已知限制（本版范围外）

- **无实时行情/监控**：quote_service 相位机与监控规则为 A 股专用；crypto 的 24hr 快照
  仅用于总览看板与数据源试拉，不接入实时轮询、语音、飞书推送。
- **无回测/纸面交易**：回测引擎 T+1/涨跌停拒单/整手取整等撮合约束为 A 股语义且未参数化，
  后端对 crypto fail-closed（日线口径下 T+0 当日收盘撮合即未来函数，见 trading_rules 模块注记）。
- **分钟策略不可用**：分钟策略仅支持 A 股（screener 显式限制 + 注册表声明）。
- **无财务/盘口数据集**：`depth5`/`financial` 未声明，相关页面（连板梯队封单、财务搜索）对 crypto 无数据。
- 币安为单一数据源，无能力矩阵多源路由（A 股多源不受影响）。
- 快讯源（新浪直播/东财 np-listapi）与情绪源为非官方公开端点，结构变更时对应源
  fail-soft 降级并在 `errors` 中说明；未对 crypto 提供历史复盘（快照只有"当前"）。
