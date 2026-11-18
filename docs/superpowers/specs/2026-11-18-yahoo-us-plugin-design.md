# Yahoo 美股数据源插件 — 设计文档

日期：2026-11-18
状态：方案已选定（A + Phase 2 后置），builder check 已合入，待用户确认

## 背景与意图

新增 `yahoo_us` 数据源插件（雅虎财经美股日线 + 除权事件），解决美股市场两个现存问题：

1. **拆股缺口**：`eastmoney_us` 无 adj_factor 端点，美股拆股日连续性缺口是 docstring 写明的已知限制（`plugins/eastmoney_us/provider.py` 头注释，见 `docs/us-market.md`）。雅虎 v8 chart API 一次请求同时返回原始 OHLCV + Dividends + Stock Splits 事件，事件可换算为本仓库 `ex_factor` 单事件比值契约（`symbol/trade_date/ex_factor`），接入既有前复权管线。
2. **日K备源**：美股日K目前仅东财一路，雅虎作为可路由的第二日K源。

研究对象为 [Heerozh/spectre](https://github.com/Heerozh/spectre) 的 `YahooDownloader`（GPL-3.0，仅借鉴思路，代码全部新写，无 GPL 传染）。

## 目标 / 非目标 / 反目标

**目标**
- `plugins/yahoo_us/`：plugin.yaml（`asset_types: [us]`，`datasets: [daily, adj_factor]`）+ httpx client + provider，实现 `get_daily`（原始价）与 `get_adj_factors`（事件→ex_factor）
- `us_sync.py` 最小集成：路由 provider 声明 adj_factor 数据集时同步美股因子到 `adj_factor_us/all.parquet`；routed provider 不含 `fetch_instruments_page` 时维表回退 eastmoney_us
- 雅虎成为 `us_data_provider` 偏好的可路由选项（注册即可路由，`_allowed_data_providers()` 已动态收录插件名，preferences.py:306-315）
- **前端可选雅虎/东财**（用户确认的需求）：设置页 DataSources 提供美股数据源选择控件。现状核实：`us_data_provider` 只能改偏好文件——`DataProvidersIn`（settings.py:553-560）仅含 A 股 7 能力字段，`GET /preferences` 亦未暴露该字段，DataSources.tsx（1260 行）只渲染 A 股能力矩阵
- 全程 fail-closed：网络失败/结构变化不得伪装成"无数据"

**非目标（本期不做，Phase 2 后置）**
- enriched 管道对美股应用复权因子（本期因子落盘"可用但未被 enriched 消费"；Phase 2 动 `enriched_generation`，对齐 A 股 `raw_*`/复权双列契约）
- 实时行情、分钟K、五档（雅虎免费源无对等能力，不声明）
- 维表（雅虎无全市场目录端点；`instruments_us.parquet` 维表继续由东财承载）
- 幸存者偏差修正（雅虎无退市股数据，属数据源固有缺陷，文档声明即可）

**反目标（失败标准）**
- 拷贝 spectre GPL 代码进仓库
- 因雅虎"失败静默返空"的行为把网络失败当成退市/无数据静默吞掉（spectre 的做法，本项目契约明确禁止：`ProviderRequestError`，base.py:19-20）
- 把雅虎预调整价格当原始价入库（雅虎序列按"至今"回溯调整拆股，见关键技术事实 3）
- 污染 A 股 7 能力路由（`asset_types: [us]` 声明即可隔离，capabilities.py:182-183 自动处理）

## 约束

- 遵循 `docs/secondary-development.md` 插件规范：`plugin.yaml` + `app.plugins.<name>.provider` 入口，`runtime: none`（与 eastmoney_us 同形）
- CONTRIBUTING §3.2/§3.3 数据契约：日K原始价、`date` = 美东交易日 naive 日历日（与 eastmoney_us 口径一致）、`normalize_daily` 白名单列、因子表 `ex_factor` 为单事件 pre/post 比值（非累积，`indicators/pipeline.py:247-311` `_apply_adj_factor` 消费语义）
- 零新依赖：httpx 已有（backend/pyproject.toml:39）；不引入 yfinance（自带限速重试语义与 fail-closed 契约冲突，且重依赖）
- 网络：本机直连境外不通，依赖 `HTTPS_PROXY` 环境变量走 127.0.0.1:7897（httpx `trust_env` 默认信任，与 binance 插件同路径）；NAS 部署环境可达性由 availability 探测如实报告
- 限速：雅虎非官方限制约 2000 请求/小时/IP，client 内置节流 + 429/999 退避
- 当前 `feat/vibe-hardening` 有未提交改动，本功能在独立分支 `feat/yahoo-us-plugin`（自 main 切出）上实施

## 关键技术事实（已核实）

1. **v8 chart 端点**：`GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={unix}&period2={unix}&interval=1d&events=div,splits&includeAdjustedClose=false`。响应 `chart.result[0]`：`meta`（`regularMarketTime`/`exchangeTimezoneName`/`currency`/`instrumentType`）、`timestamp[]`（epoch 秒）、`indicators.quote[0].{open,high,low,close,volume}`、`events.{dividends,splits}`（dividends: `{ts: {amount, date}}`；splits: `{ts: {numerator, denominator, date}}`，to/from 口径待 golden 探针验证方向，见实施决定 2）。带浏览器 UA 即可用，一般无需 crumb；cookie 由 fc.yahoo.com 引导，401/429 时刷新。
2. **splits 事件口径**：`numerator/denominator` 即 to/from（4:1 拆股 → 4/1）。方向必须在实施时用已知拆股（AAPL 2020-08-31 4:1）golden 探针钉死，不得靠猜。**实施记录（2026-11-18）：真机实证受阻**——代理（127.0.0.1:7897 共享出口）对雅虎持续 429 硬限频（cookie 引导 + 50s 间隔 ×4 不恢复），直连命中中国区 geo-block（403, `lang="zh"` 拦截页）。夹具以 `_synthetic: true` 降级提交，方向依据 yfinance/spectre 文档惯例（`numerator=to`，4:1 → numerator 4.0）；捕获测试保留（`--run-network` 以真机响应覆盖合成夹具），Task 8 冒烟复核。
3. **回溯调整陷阱（本设计最大的坑）**：雅虎返回的价格序列按**系列末端（至今）**回溯调整拆股。spectre 下载全史后需 `_to_raw_prices` 逆运算还原；对**窗口化请求**（增量同步的近端窗口），窗口外（period2 之后）若发生拆股，窗口内价格会被静默预调整且窗口内无该事件记录。因此增量同步必须带**重叠连续性校验**（见实施决定 4）。
4. **分红金额同理回溯调整**：雅虎 displayed dividend 已按后续拆股调整；as-traded 分红 = displayed × ∏(事件日之后窗口内拆股 to/from)。
5. **ex_factor 消费语义**（`_apply_adj_factor`）：`adjusted = raw × cum_at_date / total_cum = raw / ∏(该日之后事件 ex_factor)`，即 ex_factor 是**单事件 pre/post 比值**。拆股 ex_factor = to/from；分红 ex_factor = prev_close_raw / (prev_close_raw − d_as_traded)。事件在首次同步时**一次性定稿**（ex_factor 是事件自身属性，未来新事件不影响已存事件的比值，仅分红金额定稿依赖"定稿时其后续拆股已知"这一窗口性质）。
6. **路由现状**：`get_us_data_provider()`（preferences.py:379-388）白名单 = 全部注册插件名，插件注册即可被路由；`us_sync._us_provider()` 校验声明 daily 数据集（fail-closed）。`build_capability_matrix` 自动把 asset_types 源排除出 A 股候选。`sync_us_instruments`（us_sync.py:107-196）调用路由 provider 的**专属方法** `fetch_instruments_page`（139 页东财分页）——雅虎无对等实现，必须回退。
7. **美股因子存储现状**：us_sync 目前无任何因子环节（us_sync.py:268 "美股无复权——enriched 用空 factors 计算"）；A股因子合并逻辑 `kline_sync._merge_adj_factor_store(path, new_data)`（kline_sync.py:660-686 附近）按 `(symbol, trade_date)` merge-upsert 到单文件 parquet，可直接复用其模式（`adj_factor_us/all.parquet`）。symbol 命名空间天然隔离（A股 `600519.SH` / 美股裸 ticker），独立目录避免市场混表。
8. **测试现状**：provider 测试位于 `backend/tests/test_*_provider.py`（baostock/fuyao/tdxquant/custom 等先例），mock 手法沿用既有模式；venv 在 `backend/.venv`。

## 方案取舍（已选定 A）

- **A（选定）：双数据集插件 + 两阶段接入**。插件实现 daily + adj_factor（同一 API 载荷，顺手取全）；本期只落盘因子不接 enriched，Phase 2 单独做。
- B（否决）：仅 adj_factor 插件 —— v8 一次请求同时返回价格与事件，只取事件等于扔掉一半载荷；且东财日K自身质量缺口依旧。
- C（否决）：直接用 yfinance 库 —— 重依赖、自带重试/限速语义与 fail-closed 契约冲突、代理/UA 控制隔一层；仅适合一次性数据质量 spike，不进主干。

## 实施决定（builder check 后确定）

1. **重叠连续性校验放在 us_sync 服务层，provider 保持 repo-free**。本项目 provider 契约只收 symbols/dates（base.py Protocol），loader 零参实例化，无任何插件触碰存储——设计原稿"provider 与 repo 已存 close 对比"违反既有架构。修正：provider `get_daily` 纯净返回请求窗口 + 头部多带 ~10 根重叠 bar；**us_sync** 取重叠段与已存原始 close 对比，比值 ≈ 拆股比 ⇒ 判定窗口外拆股预调整 ⇒ 以全史窗口重取该标的并整段落盘；比值既非 1 也非拆股比 ⇒ 该标的拒绝落盘、上浮 stage error（fail-closed；东财/雅虎两源混存的口径差异由此暴露而非静默混合）。
2. **美股分钟K：路由 provider 未声明 minute 数据集时跳过该阶段并 WARNING**（不回退东财、不整体失败）。符合 capabilities.py「每能力独立声明、不做路由耦合」的既有哲学；`sync_us_minute` 现状只校验 daily 数据集，对任何无 minute 的新源都是潜在 AttributeError，本次一并按数据集声明守卫。此为替用户拍板的推荐项（问询未获答复），如需分钟K不断更，路由保持 eastmoney_us 即可。
3. **首次同步拉全量历史**（不向东财 365d 对齐）。雅虎全史与 1 年同为一次请求；分红事件定稿需要事件前一日原始 close，全史保证所有历史事件的 prev_close 不缺失；白名单规模小，parquet 增量可忽略。此同为替用户拍板的推荐项（问询未获答复）。
4. **限速按小白名单规模走朴素节流**：0.3s ± 抖动 + 429/999 指数退避（5s/15s ×最多3）+ fail-closed，不做小时级配额/断点配额管理（白名单数百只以内一轮同步分钟级完成，不值得引入配额状态机）。雅虎非官方 ~2000 请求/小时/IP 是持续速率口径，突发批量场景实际余量充足。
5. **golden fixture 捕获允许降级**：本机代理抖动（127.0.0.1:7897 已知不稳）可能导致真实响应捕获失败；实施时先尝试真实捕获（AAPL 2020-08-31 4:1 拆股窗口 + 普通分红窗口），失败则按文档化 schema 手工构造 fixture，但 **splits numerator/denominator 方向必须以一次真实响应钉死**（可多试几次/换 query2 主机），不得靠猜。**实施记录：已触发降级**——合成夹具提交（`_synthetic: true`），方向按文档惯例，待真机复核（同决定 2 的实施记录）。

## 设计（Phase 1 范围）

### 新增 `backend/app/plugins/yahoo_us/`

**plugin.yaml**：`name: yahoo_us`，`runtime: none`，`datasets: [daily, adj_factor]`，`asset_types: [us]`，`entry: app.plugins.yahoo_us.provider:YahooUSProvider`，`check: ...availability`。description 必须声明固有缺陷：雅虎无退市股（幸存者偏差）、历史 ticker 更名陷阱（旧代码现属其他公司，映射失败 fail-closed 而非错价）、限速非官方。

**client.py**（httpx，模式对齐 binance/client.py）：
- 浏览器 UA 常量 + `trust_env`（代理走环境变量）；fc.yahoo.com cookie 引导仅在 401/429 时触发刷新，v8 chart 常态无需 crumb
- `chart(symbol, period1, period2)` 单标的请求；节流 0.3s ± 抖动；429/999/超时指数退避（5s/15s，最多 3 次）；结构校验（`chart.result` 缺失/`error` 字段）→ 抛 `ProviderRequestError`，**禁止返空 df 伪装无数据**

**provider.py**（`YahooUSProvider`）：
- `_to_yahoo_symbol`：内部 ticker → 雅虎格式（`.`/`_` → `-`，大写），带 memo；映射后拉空且探测试别码仍空 → 该标的记为失败上浮，不得静默跳过
- `get_daily(symbols, start, end, asset_type="us")`：`_require_us` 守卫（对齐 eastmoney_us）；逐标的拉取 → 丢当日未完成 bar（美东当日且 now < 收盘缓冲）→ `timestamp[]` 经 `exchangeTimezoneName` 转 naive 美东日期 → `normalize_daily`（原始价，volume=股不换算）→ 任一标的失败收集后抛 `ProviderRequestError`（fail-closed，名单上浮给 us_sync stage_errors）。**首次同步（start=None）拉全量历史**（实施决定 3）。provider 保持 repo-free：返回窗口时头部多带 ~10 根重叠 bar，不做存储比对（校验编排见 us_sync 节，实施决定 1）
- `get_adj_factors(symbols, start, end, asset_type="us")`：同一 chart 载荷取 events → 拆股行 `ex_factor = to/from`；分红行 `ex_factor = prev_close_raw / (prev_close_raw − d_as_traded)`，`d_as_traded = displayed × ∏(事件日之后窗口内 to/from)`，`prev_close_raw` 取 **repo 已存该标的原始日线**中事件前一交易日 close（不取载荷内可能被预调整的 bar；缺失则该事件只记拆股分量并 WARNING）→ 同日拆股+分红合并为一行（unique `symbol,trade_date` 契约）。prev_close 查询由 us_sync 以参数传入因子同步步骤（provider 本身无 repo 访问）
- `get_instruments("us")`：显式抛 `ProviderRequestError`（维表由 eastmoney_us 承载，雅虎无目录端点），docstring 写明
- `availability()`：探测 AAPL 小窗口 chart，连通性/结构自检，不抛异常（对齐 eastmoney_us）

### 修改 `backend/app/services/us_sync.py`

- `sync_us_instruments`：路由 provider 无 `fetch_instruments_page` 时回退 `eastmoney_us`（WARNING 留痕）；两者皆不可用则跳过维表刷新（保留既有 parquet），不阻断日K同步
- `sync_us_daily` 增加**重叠连续性校验编排**（实施决定 1）：取 provider 返回的重叠 bar 与已存原始 close 对比；比值 ≈ 拆股比 ⇒ 以全史窗口重取该标的整段落盘；比值异常（既非 1 也非拆股比）⇒ 该标的上浮 stage error 拒绝落盘
- `sync_us_minute` 增加**数据集声明守卫**：路由 provider 未声明 minute 时跳过该阶段并 WARNING（实施决定 2），修复对任何无 minute 新源的潜在 AttributeError
- 新增美股因子同步步骤（日K同步后触发）：路由 provider 声明 `adj_factor` 数据集才执行（eastmoney_us 路由时自然跳过并 DEBUG 留痕）；增量锚点 = `adj_factor_us/all.parquet` 每 symbol 最新 `trade_date`；prev_close 由本步骤查 repo 原始日线后传入 provider；复用 `_merge_adj_factor_store` 合并模式写 `adj_factor_us/all.parquet`（如需跨模块复用，将私有 helper 提升为 kline_sync 公开函数或抽到共享模块，builder 定）

### 修改 `backend/app/api/settings.py` + 前端设置页（美股源选择，用户确认的需求）

- `DataProvidersIn` 增加 `us_data_provider: str | None` 字段；`update_data_providers` 响应回显 `preferences.get_us_data_provider()`；`GET /preferences` 暴露当前值（字段校验由 getter 白名单回退机制承担，setter 不做二次校验，与 A 股字段同模式）
- 前端 `DataSources.tsx` 增加"美股"分区：显示可路由的美股源（`custom_sources.list_plugins()` 中 `asset_types` 含 `us` 且声明 `daily` 的插件，动态枚举，eastmoney_us/yahoo_us 自动出现）+ 当前生效源 + 插件可用性状态（复用既有 plugin status/pending 展示手法）；切换即 `PUT /preferences/data-providers`
- 注意路由切换的后果提示：切到 yahoo_us 时分钟K阶段将跳过（实施决定 2）、拆股因子开始落盘——控件旁以提示文案声明，不弹确认框

### 不改动

- preferences.py（注册即可路由，getter 白名单校验已有）、capabilities.py（asset_types 自动隔离）、A 股管线全部、enriched 管线（Phase 2）

### 文档

- `docs/us-market.md` 已知限制段更新：拆股缺口可通过路由 yahoo_us 缓解（路由后因子落盘；enriched 应用待 Phase 2）
- 插件 docstring 声明幸存者偏差与 ticker 更名陷阱（spectre 的数据缺陷文档化实践）

### 测试（backend/tests/）

- `test_yahoo_us_provider.py`：golden fixture（真实保存的 v8 响应：含 AAPL 2020-08-31 4:1 拆股窗口 + 普通分红窗口）钉死 splits numerator/denominator 方向、日期换算、normalize_daily 列契约、未完成 bar 丢弃、事件→ex_factor 换算（拆股 only / 分红 only / 同日合并 / prev_close 缺失降级）、fail-closed（HTTP 错误/结构损坏不得返空）
- 插件加载/路由：plugin.yaml 经 custom.loader 注册后进入 `us_data_provider` 白名单、不进 A 股能力候选
- us_sync 集成：维表回退 eastmoney_us、分钟阶段未声明即跳过、**重叠连续性校验**（正常通过 / 检出拆股触发全史重取 / 异常比值上浮 stage error）、因子步骤在 eastmoney_us 路由下跳过、合并写入 `adj_factor_us/`
- 设置 API：`PUT /preferences/data-providers` 接受 `us_data_provider`、非法值经 getter 白名单回退 eastmoney_us 并告警、`GET /preferences` 回显

## 留给 builder 的决定

1. 节流/退避常量与 client 结构细节（对齐 binance/client.py 风格）
2. `_merge_adj_factor_store` 复用方式（提升公开 vs 抽共享模块 vs 复制模式）
3. `_to_yahoo_symbol` 特例映射表的完备性（从 `instruments_us.parquet` 抽样验证类别股代码形态，如 `BRK.B`/`BRK_A` 在东财目录中的真实写法）
4. 测试 mock 手法沿用哪个既有先例（respx vs httpx MockTransport）
5. 未完成 bar 判定阈值（收盘缓冲秒数）与 `meta.regularMarketTime` 的使用方式
6. 前端"美股"分区的具体布局与文案（复用 DataSources.tsx 既有组件风格，builder 定）

## Phase 2 轮廓（本期不实施）

enriched 管道对 `us` 资产读取 `adj_factor_us/` 应用 `_apply_adj_factor`，`open/high/low/close` 转前复权并保留 `raw_close/raw_high/raw_low`，对齐 A 股 §3.2 契约；`us_sync.py:268` 的"空 factors"注释与 `docs/us-market.md` 同步更新；enriched 增量重算范围复用因子同步返回的 affected symbols。
