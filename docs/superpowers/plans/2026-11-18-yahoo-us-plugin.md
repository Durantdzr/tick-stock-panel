# Yahoo 美股数据源插件 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `yahoo_us` 插件（雅虎美股日线原始价 + 拆股/分红事件→ex_factor），us_sync 最小集成（连续性校验、分钟守卫、因子落盘），设置页可切换雅虎/东财。

**Architecture:** provider 保持 repo-free（对齐既有插件：零参实例化、`_require_us` 守卫、`normalize_daily` 归一）；连续性校验与全史重取编排放 us_sync 服务层；雅虎 v8 chart 载荷自含拆股事件，provider 用载荷新闻事件做 spectre 式反向还原（period2 恒为 now ⇒ 事件日之后的所有调整拆股必在载荷内）；因子经 `_merge_adj_factor_store` 落 `adj_factor_us/all.parquet`，本期不接 enriched（Phase 2）。

**Tech Stack:** Python 3.12 / httpx / polars / pytest（venv: `backend/.venv`）；前端 React + vitest。

**Spec:** `docs/superpowers/specs/2026-11-18-yahoo-us-plugin-design.md`（实施决定 1-5 以 spec 为准）

## Global Constraints

- GPL 零拷贝：不复制 spectre 代码，只借鉴数学；零新依赖（httpx 已有）
- 网络：httpx `trust_env` 默认（走 `HTTPS_PROXY`，本机 127.0.0.1:7897，代理抖动属常态）；429/401/999 → cookie 引导 + 5s/15s 退避 ×3 → `YahooError`；网络异常 → 1s/2s ×3；其余非 200 立即失败
- **period2 恒为 now**（epoch 秒）：保证"事件日之后所有调整拆股必在载荷 events 内"，反向还原自洽
- 契约值（CONTRIBUTING §3.2/§3.3 + normalizer.py）：`date` = `pl.Date` naive 美东交易日；`DAILY_COLS` 9 列白名单；`ADJ_FACTOR_COLS = symbol/trade_date/ex_factor`；`ex_factor` = 单事件 pre/post 比值（拆股 = to/from；分红 = prev_close_raw/(prev_close_raw − d_as_traded)；同日合并 = 乘积）
- volume=股不换算、amount=美元（trading_rules volume_unit=share）；美股 amount 雅虎无对应字段 → null 不伪造
- 节流：标的名 0.3s + `random.uniform(0, 0.15)` 抖动；当日未完成 bar = 美东当日且 now < 16:00 ET（ZoneInfo America/New_York）
- fail-closed：任何网络/结构失败抛 `ProviderRequestError` 收集上浮，禁止返空 df 伪装无数据
- 所有后端命令：`cd backend && .venv/bin/python -m pytest ...`；lint `.venv/bin/python -m ruff check <files>`
- 提交信息遵循仓库 conventional 风格（`feat(plugin):` / `test(us):` 等）；分支 `feat/yahoo-us-plugin` 自 `main` 切出

## Review Focus

1. **splits numerator/denominator 方向**：v8 事件字段方向未知，猜错整条复权历史全错 → Task 0 夹具实证 + Task 2 断言 `ex_factor == 4.0`（AAPL 2020-08-31 4:1）
2. **雅虎 displayed dividend 是否已按后续拆股调整**：直接决定 d_as_traded 换算 → Task 0 夹具 + Task 2 断言 as-traded = $0.1925（2020-02-07 事件，且夹具载荷含 2020-08-31 拆股事件可验证 ×4）
3. **增量窗口的回溯预调整（混价静默入库）**：雅虎按"至今"回溯调整，增量续传与存量混合会静默错价 → Task 4 连续性校验测试（比值≈拆股比 → 全史重取；异常比值 → stage error 拒绝落盘）
4. **盘中未完成 bar 混入**：盘中拉取当日 bar 未收盘 → Task 2 未完成 bar 丢弃测试（monkeypatch now_et）
5. **429 限频/结构变化的 fail-closed**：雅虎失败静默返空的行为绝不能穿透 → Task 1 客户端重试/失败测试 + Task 3 provider 逐标的失败收集测试

---

### Task 0: 分支切出 + golden 夹具捕获（网络，一次性）

**Files:**
- Create: `backend/tests/fixtures/yahoo/chart_aapl_split_window.json`（period1=2020-08-24, period2=now）
- Create: `backend/tests/fixtures/yahoo/chart_aapl_div_window.json`（period1=2020-02-03, period2=2021-06-01，载荷含 2020-08-31 拆股事件）
- Create: `backend/tests/test_yahoo_fixture_capture.py`（`@pytest.mark.network`，兼作夹具 schema 校验）

**Interfaces:**
- Produces: 两份真实 v8 响应 JSON，Task 2/3 的 golden 断言数据源；实证 splits numerator/denominator 方向与 dividend 调整惯例

- [ ] **Step 1: 自 main 切分支**

```bash
git stash list  # 确认工作区 in-flight 改动不混入; 若有未提交改动保持原分支不动, 用 git worktree 或确认改动属其他特性
git fetch origin main && git checkout -b feat/yahoo-us-plugin origin/main
```

若 `feat/vibe-hardening` 的未提交改动使 checkout 不可行，用 `git worktree add` 隔离。

- [ ] **Step 2: 写夹具捕获测试** `backend/tests/test_yahoo_fixture_capture.py`

`pytest.mark.network` 标记（pyproject 已定义该 marker，conftest 提供 `--run-network` 开关——先读 `backend/tests/conftest.py` 确认开关名，若 marker 机制不同则随实际调整）。测试内直接用 httpx（浏览器 UA `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36`，trust_env 默认走代理）请求两个窗口，把完整响应 JSON 写入 fixtures 目录。断言夹具内容：
- split 夹具：`chart.result[0].events.splits` 含映射到 date=2020-08-31 的条目，记录其 `numerator/denominator` 原始值（方向实证）
- dividend 夹具：`events.dividends` 含 date=2020-02-07 条目，记录 displayed `amount` 原始值（调整惯例实证：≈0.1925 → 未按后续拆股调整；≈0.048125 → 已调整）
- 两夹具 `indicators.quote[0]` 的 `close` 数组与 `timestamp` 等长且非空

- [ ] **Step 3: 运行捕获（需代理，失败重试）**

```bash
cd backend && HTTPS_PROXY=http://127.0.0.1:7897 .venv/bin/python -m pytest tests/test_yahoo_fixture_capture.py -v --run-network
```

429 时等 60s 重试；query1 持续失败换 `query2.finance.yahoo.com`。代理不通且多次重试失败 → 按 v8 文档化 schema 手工构造两份夹具（拆股事件字段 `{date, numerator, denominator}`、分红 `{date, amount}`、quote 六数组），并在夹具文件头部加 `_synthetic: true` 标记——此情况下 Task 2 的方向断言必须保守：实现时用捕获优先，synthetic 夹具只保结构测试。

- [ ] **Step 4: 把实证结论记入设计文档实施决定 2/5**（splits 方向、dividend 惯例各一行）

- [ ] **Step 5: Commit**

```bash
git add backend/tests/fixtures/yahoo backend/tests/test_yahoo_fixture_capture.py docs/superpowers/specs/2026-11-18-yahoo-us-plugin-design.md
git commit -m "test(yahoo): v8 chart golden fixtures capture (split+dividend windows)"
```

### Task 1: YahooClient（HTTP 层）

**Files:**
- Create: `backend/app/plugins/yahoo_us/__init__.py`（空）
- Create: `backend/app/plugins/yahoo_us/client.py`
- Test: `backend/tests/test_yahoo_us_client.py`

**Interfaces:**
- Produces: `YahooError(Exception)`；`YahooClient(timeout: float = 15.0, transport: httpx.BaseTransport | None = None)`，方法 `close() -> None`、`chart(symbol: str, period1: int, period2: int) -> dict`（`transport` 仅供测试注入 MockTransport）

- [ ] **Step 1: 写失败测试** `test_yahoo_us_client.py`

httpx.MockTransport 注入，断言：
1. `chart("AAPL", p1, p2)` 成功 → 返回原始 dict；请求 URL 含 `/v8/finance/chart/AAPL`，query 含 `interval=1d`、`events=div,splits`、`includeAdjustedClose=false`、`period1/period2` 原样
2. 首次 429 → 200：请求前先命中 `https://fc.yahoo.com`（cookie 引导被触发），最终成功
3. 429×3 → `pytest.raises(YahooError)`（用 monkeypatch 把 `time.sleep` 置零并计数：退避序列 5s/15s）
4. 非 200 且非 429/401/999（如 404）→ 立即 `YahooError`，无重试
5. 200 但 `chart.error` 非空 / `chart.result` 缺失 → `YahooError`
6. `httpx.ConnectError`×3 → `YahooError`（monkeypatch sleep 计数：1s/2s）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_yahoo_us_client.py -v`
Expected: FAIL（ModuleNotFoundError: app.plugins.yahoo_us）

- [ ] **Step 3: 实现 `client.py`**

结构对齐 `plugins/binance/client.py`（模块 docstring 说明端点/口径/失败语义；`httpx.Client(headers={"User-Agent": UA}, timeout=timeout, transport=transport)`；`_bootstrap_cookie()` = GET `https://fc.yahoo.com` 忽略状态码存 cookies；`chart()` 内：网络异常 1s/2s ×3；响应 429/401/999 → bootstrap + 5s/15s 退避 ×3；结构校验 `chart.error`/`chart.result`）。不含节流（节流在 provider 标的间，对齐 eastmoney `_DAILY_INTERVAL_S` 模式）。

- [ ] **Step 4: 运行通过 + ruff**

```bash
cd backend && .venv/bin/python -m pytest tests/test_yahoo_us_client.py -v && .venv/bin/python -m ruff check app/plugins/yahoo_us
```

- [ ] **Step 5: Commit** `feat(plugin): yahoo_us v8 chart client (cookie bootstrap + 429 backoff, fail-closed)`

### Task 2: parse_chart 反向还原纯函数（golden 断言）

**Files:**
- Create: `backend/app/plugins/yahoo_us/provider.py`（本任务只含模块级纯函数 + 常量）
- Test: `backend/tests/test_yahoo_us_provider.py`（新建，后续任务续用）

**Interfaces:**
- Consumes: Task 0 夹具两份
- Produces:
  `parse_chart(payload: dict, symbol: str, now_et: datetime | None = None) -> tuple[list[dict], list[tuple[date, float]], list[tuple[date, float]]]`
  返回 `(daily_rows_as_traded, split_events, dividend_events)`；rows 元素含 `symbol/date(open cared as date 对象)/open/high/low/close/volume`；split_events 元素 `(ex_date, to_from)`；dividend_events 元素 `(ex_date, d_as_traded)`。`now_et=None` 时取真实 `datetime.now(ZoneInfo("America/New_York"))`

- [ ] **Step 1: 写失败测试**（追加到 `test_yahoo_us_provider.py`）

读 Task 0 夹具 JSON：
1. split 夹具：`parse_chart` 产出的 split_events 含 `(date(2020,8,31), 4.0)`（钉死 to/from 方向）；对 2020-08-28 行：`close == 夹具内 displayed_close(08-28) × 4.0`（±1e-6，直接从夹具 JSON 取 displayed 值计算，不依赖记忆价位）
2. dividend 夹具：dividend_events 含 `(date(2020,2,7), 0.1925)`（±1e-9；as-traded 金额为公开事实 $0.1925/股，夹具载荷含 2020-08-31 拆股事件支撑 ×4 还原）
3. 未完成 bar 丢弃：取 split 夹具 payload，手工把最后一个 (timestamp, quote 数组尾元素) 替换为 now 当日美东时间戳，`now_et=datetime(2026,11,18,10,0,tzinfo=NY)` → 该日不出现在 rows；`now_et` 收盘后（16:05）→ 保留
4. timestamp→美东交易日换算：断言夹具内已知时间戳对应的 date 正确（含跨夏令时边界任取一个即可）
5. volume 反向还原：08-28 行 `volume == displayed_volume × ?` 按 `displayed / later` 断言（夹具值自算）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && .venv/bin/python -m pytest tests/test_yahoo_us_provider.py -v`
Expected: FAIL（ImportError: parse_chart）

- [ ] **Step 3: 实现 `parse_chart`**

算法（载荷新闻事件自洽，period2=now 不变量见 Global Constraints）：

```python
split_f[t] = to/from（事件日；无事件日 = 1.0）
later[t]   = ∏(split_f[u] for u > t)   # 严格晚于该 bar 的拆股累积
ohlc_raw   = ohlc_displayed × later[t]  # 逐 bar
volume_raw = volume_displayed / later[t]
d_as_traded(t) = displayed_amount(t) × later[t] × split_f[t]  # 分红按除权日前股数计
row 丢弃: bar_date == now_et.date() and now_et.time() < time(16, 0)
timestamp → date: datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).date()
```

（若 Task 0 实证 displayed dividend **未**被后续拆股调整，则 `d_as_traded = displayed × split_f[t]` 仅同日项，去掉 `later` 项——以夹具断言 0.1925 为准绳实现其一。）

- [ ] **Step 4: 运行通过 + ruff**

- [ ] **Step 5: Commit** `feat(plugin): yahoo chart 反向拆股还原与事件解析 (golden fixtures pinned)`

### Task 3: YahooUSProvider.get_daily + 插件注册

**Files:**
- Modify: `backend/app/plugins/yahoo_us/provider.py`
- Create: `backend/app/plugins/yahoo_us/plugin.yaml`
- Test: `backend/tests/test_yahoo_us_provider.py`

**Interfaces:**
- Consumes: `YahooClient.chart`（Task 1）、`parse_chart`（Task 2）、`normalize_daily(rows, source=name)`（normalizer.py:34）
- Produces:
  `class YahooUSProvider`: 属性 `name = "yahoo_us"`、`builtin = True`、实例 `config = _YahooUSConfig()`（shim 对齐 `eastmoney_us._USConfig`：`datasets = dict.fromkeys(("daily", "adj_factor"))`）；构造 `__init__(self, client: YahooClient | None = None)`；
  `get_daily(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None, asset_type: str = "us", on_chunk_done=None) -> pl.DataFrame`（start=None → period1=0 全史；end 恒用真实 now）；
  `availability() -> tuple[bool, str]`（模块级）

- [ ] **Step 1: 写失败测试**

Fake client（`_FakeYahooClient.chart` 按 symbol 返回预置 payload 或抛异常）注入：
1. 两个字段映射：`"BRK.B"→"BRK-B"`、`"brk_b"→"BRK-B"`（断言 client 收到的 symbol）
2. `get_daily(["AAPL"], start, None, "us")` 返回 normalize_daily 口径：列集 ⊆ `DAILY_COLS ∪ {source}`、`date` 为 `pl.Date`、close 为原始价（Task 2 夹具值）、`amount` 全 null、`source == "yahoo_us"`（对齐 eastmoney_us 在 normalize 后补 source 列的写法）
3. 逐标的节流：monkeypatch `time.sleep` 计数，2 标的 ≥1 次调用
4. fail-closed：fake 对第二只抛 `YahooError` → `pytest.raises(ProviderRequestError)` 且异常信息含失败 symbol 名；成功那只**不落盘**（整个调用无返回）
5. `_require_us`：`asset_type="stock"` → `ProviderRequestError`
6. `get_instruments("us")` → `ProviderRequestError`（维表由东财承载，显式拒绝，docstring 写明）
7. plugin.yaml 注册：`custom_sources.list_plugins()` 含 `yahoo_us`，`asset_types == ["us"]`，datasets 含 `daily`+`adj_factor`；`capabilities.build_capability_matrix(...)` 的每个能力 candidates 不含 yahoo_us
7. availability：fake 返回合法 payload → `(True, "ok")`；fake 抛错 → `(False, 非空原因)`，不抛异常

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

provider 骨架对齐 `eastmoney_us/provider.py`（`_require_us` 模块函数、`_to_yahoo_symbol` memo、逐标的循环 + `time.sleep(_DAILY_INTERVAL_S + uniform(0, 0.15))`、`on_chunk_done` 透传、失败收集后 `raise ProviderRequestError(f"雅虎美股日K拉取失败: {failed}")`）。`plugin.yaml` 按 binance.yaml 字段（`runtime: none`、`check: app.plugins.yahoo_us.provider:availability`），description 必须含：雅虎无退市股（幸存者偏差）、历史 ticker 更名可能取错公司、非官方限速。

- [ ] **Step 4: 运行通过 + ruff + 插件加载验证**

```bash
cd backend && .venv/bin/python -m pytest tests/test_yahoo_us_provider.py tests/test_yahoo_us_client.py -v && .venv/bin/python -m ruff check app/plugins/yahoo_us
.venv/bin/python -c "from app.data_providers import custom as c; c.load_all(); print([p['name'] for p in c.list_plugins()])"
```
Expected: 输出含 `yahoo_us`（本机有代理时 check 探测会成功；无代理时 status 不可用但清单仍在）

- [ ] **Step 5: Commit** `feat(plugin): yahoo_us provider get_daily (raw prices, fail-closed) + manifest`

### Task 4: YahooUSProvider.get_adj_factors

**Files:**
- Modify: `backend/app/plugins/yahoo_us/provider.py`
- Test: `backend/tests/test_yahoo_us_provider.py`

**Interfaces:**
- Consumes: `parse_chart` 的 `split_events/dividend_events`；`normalize_adj_factors`（normalizer.py:70）
- Produces:
  `get_adj_factors(self, symbols: list[str], start_time: datetime | None, end_time: datetime | None, asset_type: str = "us", prev_closes: dict[str, dict[date, float]] | None = None, on_chunk_done=None) -> pl.DataFrame`
  返回列 `symbol/trade_date(pl.Date)/ex_factor/dividend/prev_close`（明细列分红行才非空）；start_time 语义 = 只返回 trade_date > start_time.date() 的事件（增量锚点契约，与 kline_sync.sync_adj_factor 一致）

- [ ] **Step 1: 写失败测试**

1. 拆股 only：Task 2 split 夹具 payload → 行 `(AAPL, 2020-08-31, 4.0)`，明细列 null
2. 分红 only：dividend 夹具 + `prev_closes={"AAPL": {date(2020,2,6): 318.0}}` → 行 `ex_factor ≈ 318.0/(318.0−0.1925)`，`dividend≈0.1925`，`prev_close=318.0`（注意 prev_close 用**传入值**而非载荷价，docstring 写明原因：载荷 bar 可能被窗口外拆股预调整）
3. prev_close 缺失：prev_closes 无该日期 → 该事件**不产出行**，`caplog` 有 WARNING（拆股分量不受影响仍产出）
4. 同日拆股+分红合并：构造合成 payload（一个时间戳既有 split 4:1 又有 div）→ 单行 `ex_factor == 4.0 × prev/(prev−d)`
5. 增量过滤：`start_time=date(2020,9,1)` → 2020-08-31 事件不返回
6. 返回帧可被 `normalize_adj_factors` 直接消费（列名/类型契约）

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

逐标的 `chart(symbol, period1, period2)` → `parse_chart` → events 过滤增量 → 分红行查 `prev_closes[symbol]` 中 < ex_date 的最大 date 的 close（缺失 → WARNING + 跳过）→ 同日事件合并为一行 → `normalize_adj_factors` + 明细列附加 → concat 返回。节流/失败收集同 get_daily。

- [ ] **Step 4: 运行通过 + ruff**

- [ ] **Step 5: Commit** `feat(plugin): yahoo_us get_adj_factors (splits+dividends → ex_factor 事件)`

### Task 5: us_sync 集成（连续性校验 / 分钟守卫 / 因子落盘）

**Files:**
- Modify: `backend/app/services/us_sync.py`
- Test: `backend/tests/test_us_market.py`（扩展现有 `_FakeUSProvider` 模式）

**Interfaces:**
- Consumes: Task 3/4 provider 方法；`kline_sync._merge_adj_factor_store(out: Path, new_data) -> tuple[int, list[str]]`（us_sync 已有私有导入先例 `_write_minute_partition`）；`_us_daily_dir(repo)`、`_latest_daily_dates(repo)`、`us_universe.load_universe`
- Produces:
  - `sync_us_daily` 内新增重叠连续性校验（无新公开函数）
  - `sync_us_adj_factors(repo, on_progress=None) -> dict`（新公开函数）
  - `run_us_sync` 状态 dict 新增 `"factors"` 键

- [ ] **Step 1: 写失败测试**（追加到 `test_us_market.py`）

扩展 `_FakeUSProvider`：加 `get_adj_factors`（返回预置帧）与可切换的 daily 行为。新增测试（monkeypatch `us_sync._us_provider` 同既有 fixture 模式）：
0. **维表回退**：路由 provider（fake 无 `fetch_instruments_page` 属性）→ `sync_us_instruments` 自动改用 eastmoney_us 拉维表（monkeypatch `EastmoneyUSProvider.fetch_instruments_page` 或注入 fake），WARNING 留痕；eastmoney_us 也不可用 → 跳过维表刷新且不抛（保留存量 parquet）
1. **分钟守卫**：monkeypatch `app.data_providers.custom.provider_has_dataset` 使 `(name,"minute")→False`（us_sync 经 `custom_sources.provider_has_dataset` 调用，先确认 custom/__init__ 的再导出位置按实际调整 patch 目标）→ `sync_us_minute` 返回 `{"ok": True, "skipped": "minute_not_declared", ...}` 且不调 provider.get_minute；默认（声明 minute）行为不变（既有测试不回归）
2. **连续性-通过**：fake get_daily 返回与存量重叠区 close 一致 + 新 bar → 正常 append，无 stage error
3. **连续性-拆股预调整**：fake 返回重叠区 close = 存量 ×4 → 同一 symbol 自动以 `start_time=None` 二次拉取（fake 记录调用参数断言），落盘后存量行被覆盖为全史值
4. **连续性-异常比值**：重叠区 close = 存量 ×1.37 → 该 symbol 进 errors 列表，其数据不落盘，`sync_us_daily` 其余标的照常，返回 `ok=True` + errors 非空
5. **因子步骤-声明守卫**：`provider_has_dataset(name,"adj_factor")→False` → `sync_us_adj_factors` 返回 skipped 且不调 provider
6. **因子步骤-happy path**：fake 返回含 1 行事件帧 → `data_dir/adj_factor_us/all.parquet` 存在且含该行；二次调用锚点增量（fake 记录 start_time）
7. **run_us_sync**：状态 dict 含 `factors` 键；进度回调出现 `("factors", ...)` 阶段

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现 us_sync.py**

```python
_OVERLAP_CALENDAR_DAYS = 14  # 连续性校验重叠窗口

def sync_us_instruments(repo) -> int:
    # 路由 provider 无 fetch_instruments_page (getattr 检查) → 改用
    # custom_sources.get_provider("eastmoney_us") (WARNING 留痕);
    # 两者皆不可用 → WARNING + 返回存量行数 (跳过刷新, 不阻断后续阶段)

def sync_us_daily(repo, *, incremental=True, on_progress=None) -> dict:
    # 每标的增量起点回退 _OVERLAP_CALENDAR_DAYS 取重叠;
    # _check_overlap(new_df, stored_closes) -> ("ok"|"split"|"mismatch", ratio):
    #   重叠 = new.date ≤ 存量 max(date) 的公共日期; 全部 |r-1|≤1e-6 → ok;
    #   全部比值一致(极差≤0.001)且 ≠1 → split; 否则 mismatch
    #   split → provider.get_daily([symbol], None, None, "us") 全史重取再 append;
    #   mismatch → errors.append + continue (fail-closed)
    # stored_closes: pl.scan_parquet(_us_daily_dir) filter symbol/date≥重叠起点 → {date: close}

def sync_us_minute(...):  # 头部加数据集守卫:
    # if not provider_has_dataset(preferences.get_us_data_provider(), "minute"):
    #     WARNING + return {"ok": True, "skipped": "minute_not_declared", "symbols": len(symbols)}

def sync_us_adj_factors(repo, on_progress=None) -> dict:
    # 守卫同上 (adj_factor); 锚点 = adj_factor_us/all.parquet 每 symbol max(trade_date)+1d
    # prev_closes = 全白名单 {symbol: {date: close}} 扫 _us_daily_dir
    # df = provider.get_adj_factors(symbols, anchor, None, "us", prev_closes=prev_closes)
    # _merge_adj_factor_store(repo.store.data_dir / "adj_factor_us" / "all.parquet", df)
    # 空 df → {"ok": True, "rows": 0}

# run_us_sync: daily 之后插入 factors = sync_us_adj_factors(repo); _report("factors", 85, ...)
# 状态 dict 加 "factors": factors
```

模块 docstring 头部"无复权"注释更新为"复权因子经 yahoo_us 路由落 adj_factor_us（enriched 应用见 Phase 2）"。

- [ ] **Step 4: 全量 us 测试回归 + ruff**

```bash
cd backend && .venv/bin/python -m pytest tests/test_us_market.py tests/test_yahoo_us_provider.py -v && .venv/bin/python -m ruff check app/services/us_sync.py
```

- [ ] **Step 5: Commit** `feat(us): yahoo_us 路由集成 — 连续性校验/分钟守卫/因子落盘 adj_factor_us`

### Task 6: 设置 API 暴露 us_data_provider

**Files:**
- Modify: `backend/app/api/settings.py`（`DataProvidersIn`:553、`update_data_providers`:970、`get_preferences`:636）
- Test: `backend/tests/test_settings_us_provider.py`（新建；API 测试 harness 沿用 `tests/test_capability_augment.py` 的既有模式——先读该文件取 TestClient/认证处理方式）

**Interfaces:**
- Produces: `PUT /api/settings/preferences/data-providers` 请求体新增可选字段 `us_data_provider: str`；`GET /api/settings/preferences` 与 PUT 响应均含 `us_data_provider` 当前值（经 `preferences.get_us_data_provider()` 白名单回退校验）

- [ ] **Step 1: 写失败测试**

1. PUT `{"us_data_provider": "yahoo_us"}` → 响应含 `"us_data_provider": "yahoo_us"`；GET 回显一致
2. PUT `{"us_data_provider": "not_a_source"}` → 响应回退 `"eastmoney_us"`（getter 白名单回退，告警日志出现）
3. PUT 不含该字段 → 存量偏好不被覆盖（A 股字段回归不受影响）
4. 测试结束恢复偏好原值（teardown 或 tmp preferences 路径，随 harness 惯例）

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**（三处各一行/一字段，与 A 股字段同模式）

- [ ] **Step 4: 运行通过 + ruff**

- [ ] **Step 5: Commit** `feat(api): us_data_provider 偏好出入口 (设置页美股源切换)`

### Task 7: 前端美股源选择控件

**Files:**
- Modify: `frontend/src/lib/api.ts`（`Preferences` 接口:2492、`updateDataProviders`:3026）
- Modify: `frontend/src/pages/settings/DataSources.tsx`（`SettingsDataSourcesPanel`:602 内新增分区）
- Test: `frontend/src/pages/settings/UsRouting.test.tsx`（mock 手法沿用 `ImportCenter.test.tsx` 先例；若其 harness 过重则省略测试，以 `npm run build` 类型检查为验收并在此注明）

**Interfaces:**
- Consumes: `api.updateDataProviders`（Task 6 扩展）、数据源清单查询（DataSources.tsx 既有 plugins 查询，含 `asset_types`/`datasets`/`available` 字段）、`usePreferences()`（QK.preferences）
- Produces: `Preferences.us_data_provider?: string`；面板新分区 `UsRoutingSection`（源列表 + 当前源高亮 + 可用性标记 + 提示文案）

- [ ] **Step 1: api.ts 类型扩展**

`Preferences` 接口加 `us_data_provider?: string`；`updateDataProviders` 参数与返回类型改为 `Partial<Pick<Preferences, ProviderField> & { us_data_provider?: string }>`。**不改** `ProviderField` 联合类型与 `provider-routing.ts` 的 `DEFAULT_ROUTING`（us 路由不属于 A 股能力矩阵，加入会导致"一键套用"误覆盖美股路由）。

- [ ] **Step 2: UsRoutingSection 组件**

数据：既有插件清单过滤 `asset_types.includes('us') && datasets.includes('daily')`；当前值 `preferences.us_data_provider ?? 'eastmoney_us'`；交互对齐 `CapabilityRoutingSection` 的 chip + pending 态模式（mutation `api.updateDataProviders({ us_data_provider: name })`，乐观更新 QK.preferences、失败回滚）；每源显示 display_name + 可用性（available=false 置灰 + status 提示）；固定提示文案："切至雅虎后：分钟K同步跳过（雅虎不提供）；拆股/分红因子将落盘（enriched 复权应用待后续版本）"。

- [ ] **Step 3: 验证**

```bash
cd frontend && npx vitest run src/pages/settings && npm run build
```
Expected: 测试通过 / tsc 无类型错误

- [ ] **Step 4: Commit** `feat(frontend): 设置页美股数据源切换 (eastmoney_us / yahoo_us)`

### Task 8: 文档 + 全量验证

**Files:**
- Modify: `docs/us-market.md`（:39 复权行、:67-68 已知限制段）
- Modify: `docs/superpowers/specs/2026-11-18-yahoo-us-plugin-design.md`（状态行改为已实施）

- [ ] **Step 1: 更新 docs/us-market.md**

复权行改为"路由 yahoo_us 时拆股/分红事件落 `adj_factor_us/`（原始价入库不变；enriched 复权应用待 Phase 2）"；已知限制段补雅虎源固有缺陷（幸存者偏差、ticker 更名、非官方限速、分钟K缺失）。

- [ ] **Step 2: 全量后端测试 + 前端构建**

```bash
cd backend && .venv/bin/python -m pytest tests/ -x -q
cd frontend && npm run build
```
Expected: 全部通过（若有既存无关失败，逐条核对是否本分支引入——不得跳过未解释的失败）

- [ ] **Step 3: 真机冒烟（需代理，可选但强烈建议）**

```bash
cd backend && HTTPS_PROXY=http://127.0.0.1:7897 .venv/bin/python -c "
from app.plugins.yahoo_us.client import YahooClient
from app.plugins.yahoo_us.provider import parse_chart
from datetime import datetime
c = YahooClient()
p = c.chart('AAPL', 0, int(datetime.now().timestamp()))
rows, splits, divs = parse_chart(p, 'AAPL')
print(len(rows), rows[-1], splits[-1])"
```
Expected: 数千行、末行为近期交易日、末次拆股 4.0（2020-08-31）。代理不通则记录跳过原因，不阻塞。

- [ ] **Step 4: Commit + 分支汇总** `docs(us): yahoo_us 路由与固有缺陷文档`；输出分支 commit 清单与测试结果摘要给用户。
