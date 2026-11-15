# 数据页分市场 + 同步日志 + 分钟级自动同步 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 数据页按市场（A股/crypto/us）切换；crypto/us 有常驻同步卡、持久化同步日志（复用 job_store）与分钟级档位自动同步（crypto 7×24，us 仅盘中）；顺带修复定时同步 on_progress 契约 P1。

**Architecture:** 后端把手动/定时/自动三路同步统一到 `_run_tracked` 任务库路径（label=市场族，trigger=来源），自动同步用 APScheduler `IntervalTrigger` + 偏好重注册（review_schedule 先例）；前端 Data.tsx 顶部市场切换器 + 三个新组件（同步卡/自动同步卡/日志卡）。

**Tech Stack:** FastAPI + APScheduler + Pydantic v2 + Polars；React 18 + TanStack Query + vitest。

**Spec:** `docs/superpowers/specs/2026-11-15-data-page-multi-market-sync-design.md`（计划从该文档论证，执行者两份都读）

## Global Constraints

- 不改 A股 既有卡片、管道与调度行为；`_run_tracked` 重构对既有调用方行为等价（仅新增可选参数）。
- 档位集合 `{5, 15, 30, 60}`（分钟）；`job_store.max_jobs` 50→500；job label ∈ `{crypto_sync, us_sync}`；trigger ∈ `{manual, daily, auto}`。
- fail-closed：非法偏好持久化值读取回退 disabled/默认；非法请求值 422；us session 门控显式。
- 单位/日期/复权口径不触碰（provider 层零改动）。
- **本计划任务一律不自动 commit**（工作区含用户未提交改动，混杂提交风险）；全部验证通过后由用户确认统一提交（提交时须 `git commit -s`，DCO 强制）。
- 验证命令：`cd backend && uv run pytest <files> -q`；`uv run ruff check <changed files>`；`cd frontend && pnpm build`；`git diff --check`。新改文件不得引入 Ruff 告警。
- 不提交 `data/`、密钥、本地样本路径。

## Review Focus

1. A股 长任务运行中触发手动市场同步 → 409 文案须指明"已有数据任务在运行（可能为其他市场或 A股 任务）"，不得照旧声称"同市场同步进行中"。→ Task 3 `test_start_market_sync_busy_returns_none` + API 409 映射。
2. us 自动同步非盘中/节假日必须静默跳过且**不产生 job 记录**。→ Task 4 `test_us_auto_sync_gate`。
3. `ok=True` 但 `errors` 非空的部分失败 → 成功记录附错误摘要，不得静默丢失；`ok=False` → failed 记录带原因，不得记成成功。→ Task 3 `test_ok_false_maps_to_failed` / `test_partial_errors_succeeded_with_summary`。
4. `?label=` 过滤不得混入无 label 的 A股 记录；存量无 label/trigger 键的旧 JSON 记录读取与过滤不报错。→ Task 2 `test_list_jobs_label_filter` / `test_legacy_record_without_label_readable`。
5. 前端市场切换后日志/偏好缓存不串市场（日志查询键带 label；crypto/us 组件不复用同一无参键）。→ Task 6 组件测试 + Task 7 `pnpm build`。

---

### Task 1: on_progress 契约修复（P1）+ run_*_sync 阶段回调

**Files:**
- Modify: `backend/app/services/crypto_sync.py`（`run_crypto_sync`）
- Modify: `backend/app/services/us_sync.py`（`run_us_sync`）
- Modify: `backend/app/jobs/daily_pipeline.py:1450-1487`（`_crypto_sync_task` / `_us_sync_task` 签名）
- Test: `backend/tests/test_crypto_pipeline.py`、`backend/tests/test_us_market.py`

**Interfaces:**
- Produces: `run_crypto_sync(repo, *, with_minute: bool = False, on_progress: Callable[[str, int, str], None] | None = None) -> dict`
- Produces: `run_us_sync(repo, *, with_minute: bool = False, skip_non_trading_day: bool = False, on_progress: Callable[[str, int, str], None] | None = None) -> dict`
- stage 约定（后续任务的 job progress 直接复用）：`"instruments"` → pct 10；`"daily"` → pct 50；`"minute"`（仅 with_minute）→ pct 85；完成前最后回调 `"enriched"` → pct 95。on_progress 为 None 时零开销跳过。

- [ ] **Step 1: 写失败测试**（两个文件各一例，用现有 `_FakeCryptoProvider` / `_FakeUSProvider` fixture）

```python
def test_run_crypto_sync_reports_progress(tmp_path, repo, fake_provider):
    events: list[tuple[str, int]] = []
    result = crypto_sync.run_crypto_sync(repo, with_minute=False,
                                         on_progress=lambda s, p, m: events.append((s, p)))
    assert result["ok"] is True
    stages = [s for s, _ in events]
    assert stages == ["instruments", "daily", "enriched"]

def test_run_us_sync_reports_progress(tmp_path, fake_us_provider):
    # repo fixture 同 test_us_sync_full_and_incremental 的构造方式
    events: list[str] = []
    us_sync.run_us_sync(repo, on_progress=lambda s, p, m: events.append(s))
    assert events == ["instruments", "daily", "enriched"]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/test_crypto_pipeline.py::test_run_crypto_sync_reports_progress -q`
Expected: FAIL（`TypeError: run_crypto_sync() got an unexpected keyword argument 'on_progress'`）

- [ ] **Step 3: 实现**：两个 run 函数加 `on_progress` 形参，在 instruments/daily/enriched(=minute 完成后)/各阶段完成点回调（固定 pct 见 Interfaces）；`daily_pipeline.py` 的 `_crypto_sync_task(on_progress=None)`、`_us_sync_task(on_progress=None)` 接受并转发（`_run_tracked` 现有 `fn(on_progress=progress)` 调用即刻生效，定时同步 P1 就此修复）。

- [ ] **Step 4: 运行确认通过 + 既有回归**

Run: `uv run pytest tests/test_crypto_pipeline.py tests/test_us_market.py tests/test_daily_pipeline_minute_gate.py -q`
Expected: PASS（含全部既有用例）

- [ ] **Step 5: ruff 检查改动文件**（不 commit）

Run: `uv run ruff check app/services/crypto_sync.py app/services/us_sync.py app/jobs/daily_pipeline.py`
Expected: 无告警

### Task 2: job_store label/trigger + max_jobs 500 + /api/pipeline/jobs label 过滤

**Files:**
- Modify: `backend/app/services/pipeline_jobs.py`（`JobStore.__init__` 默认 `max_jobs: int = 500`；`create()` 增加可选 `label: str | None = None, trigger: str | None = None` 关键字参，写入 job dict；`_summary()` 增加 `"label": j.get("label"), "trigger": j.get("trigger")`）
- Modify: `backend/app/api/pipeline.py:157`（`list_jobs` 增加 `label: str | None = None`）
- Test: `backend/tests/test_job_record_persistence.py`（追加）

**Interfaces:**
- Produces: `job_store.create(timeout_s=None, *, long_running=False, label=None, trigger=None) -> tuple[str, bool]`（label/trigger 持久化进 `{id}.json` 并出现在 `_summary()` 输出，键缺失时为 None）
- Produces: `GET /api/pipeline/jobs?limit=&label=crypto_sync,us_sync` → `{"active_id", "jobs": [summary...]}`，label 逗号分隔白名单过滤；不传 = 现有全量语义。

- [ ] **Step 1: 写失败测试**

```python
def test_create_with_label_and_trigger_roundtrip(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, is_new = store.create(timeout_s=60, label="crypto_sync", trigger="manual")
    assert is_new
    store.succeed(jid, {"ok": True})
    recent = store.list_recent(limit=5)
    assert recent[0]["label"] == "crypto_sync" and recent[0]["trigger"] == "manual"

def test_legacy_record_without_label_readable(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create(timeout_s=60)          # 不带 label/trigger
    store.succeed(jid, {})
    recent = store.list_recent(limit=5)
    assert recent[0]["label"] is None and recent[0]["trigger"] is None

def test_list_jobs_label_filter(monkeypatch, tmp_path):
    # 仿 test_job_stall_and_cancel.py::test_manual_cancel_endpoint_contract:
    # FastAPI() + include_router(pipeline.router) + monkeypatch app.api.pipeline.job_store
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr("app.api.pipeline.job_store", store)
    a, _ = store.create(timeout_s=60, label="crypto_sync", trigger="auto"); store.fail(a, "x")
    b, _ = store.create(timeout_s=60, label="us_sync", trigger="manual"); store.succeed(b, {})
    c, _ = store.create(timeout_s=60)             # A股 无 label
    store.succeed(c, {})
    client = TestClient(app)
    body = client.get("/api/pipeline/jobs", params={"label": "crypto_sync,us_sync"}).json()
    labels = [j["label"] for j in body["jobs"]]
    assert set(labels) == {"us_sync", "crypto_sync"} and len(labels) == 2  # 不含 c (None)
    assert len(client.get("/api/pipeline/jobs").json()["jobs"]) == 3      # 不传 label → 全量
```

- [ ] **Step 2: 运行确认失败**（`create() got an unexpected keyword argument 'label'` / 断言失败）

- [ ] **Step 3: 实现**（按 Files 描述；`_delete_oldest` 逻辑不动，仅默认容量变 500）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `uv run pytest tests/test_job_record_persistence.py tests/test_job_stall_and_cancel.py tests/test_pipeline_capacity.py -q`
Expected: PASS

- [ ] **Step 5: ruff 检查**

### Task 3: 失败映射 + 统一市场同步任务体 + 手动同步入任务库

**Files:**
- Modify: `backend/app/jobs/daily_pipeline.py`（新增 `MarketSyncFailedError`、模块级 `_run_market_sync(repo, market, *, with_minute, skip_non_trading_day, on_progress)`、`start_market_sync(repo, market, *, with_minute=False) -> str | None`；把 `_run_tracked` 的「acquire 后→release 前」主体提取为 `_tracked_sync_flow(fn, job_label, trigger, job_id) -> None`，`_run_tracked(fn, job_label, *, trigger="daily")` 行为保持；`except MarketSyncFailedError as e: job_store.fail(job_id, str(e))`；重写 crypto/us 两个 `scheduler.add_job` 的 lambda 用 `_run_market_sync`，trigger="daily"）
- Modify: `backend/app/api/us.py`、`backend/app/api/crypto.py`（`post_sync` 改调 `start_market_sync`；`None` → 409 `"已有数据任务在运行（可能是其他市场同步或 A股 任务）"`；删除模块级 `us_sync.is_running()`/`crypto_sync.is_running()` 预检（单飞已覆盖）；响应 `{"started": True, "job_id": ..., "with_minute": ...}`）
- Test: `backend/tests/test_market_sync_jobs.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `run_crypto_sync/run_us_sync(on_progress=...)`；Task 2 的 `create(label=, trigger=)`。
- Produces: `MarketSyncFailedError(Exception)`；`_run_market_sync(repo, market: str, *, with_minute: bool, skip_non_trading_day: bool = False, on_progress=None) -> dict`（`market` ∈ {"crypto","us"}；结果 `ok` falsy 时 `raise MarketSyncFailedError(f"{market} 同步失败: {result['error']}")`；`skipped=us_holiday` 视为 ok）；`start_market_sync(...) -> str | None`（busy → None；否则 create(long_running=True, label=f"{market}_sync", trigger="manual") + acquire slot + daemon 线程跑 `_tracked_sync_flow`，返回 job_id）。
- Produces（自动同步 Task 4 复用）：`_tracked_sync_flow` 对 `MarketSyncFailedError` 记 failed、对 `JobCancelledError` 静默、其余异常记 `f"{job_label} failed"`，finally `release_run_slot(job_id)`。

- [ ] **Step 1: 写失败测试**（fake provider fixture 仿 test_us_market.py；线程完成用轮询 `job_store.get(jid)["status"]` 等待终态，超时 10s fail）

```python
def test_start_market_sync_creates_tracked_job(tmp_path, repo, fake_us_provider):
    jid = start_market_sync(repo, "us", with_minute=False)
    assert jid
    j = _await_terminal(job_store, jid)
    assert j["status"] == "succeeded"
    assert j["label"] == "us_sync" and j["trigger"] == "manual"
    assert "instruments" in (j["result"] or {})

def test_ok_false_maps_to_failed(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(us_sync, "run_us_sync", lambda *a, **k: {"ok": False, "error": "boom"})
    jid = start_market_sync(repo, "us")
    j = _await_terminal(job_store, jid)
    assert j["status"] == "failed" and "boom" in j["error"]

def test_partial_errors_succeeded_with_summary(...):
    # fake provider 返回 ok=True 且 daily.errors 非空 → succeeded，result.daily.errors 可见

def test_start_market_sync_busy_returns_none(tmp_path, repo):
    job_store.create(timeout_s=600)   # 占住单飞 (pending)
    assert start_market_sync(repo, "us") is None

def test_scheduled_daily_task_failure_mapping(tmp_path, repo, monkeypatch):
    # 定时路径: _run_tracked(fn 包装 _run_market_sync, "crypto_sync", trigger="daily")
    # run_crypto_sync 打桩返回 ok False → job failed, trigger=daily

def test_us_sync_api_returns_409_when_busy(monkeypatch, tmp_path):
    # 仿 test_job_stall_and_cancel.py 的 TestClient 模式挂 app.api.us.router
    # monkeypatch app.api.us.start_market_sync → None → POST /api/us/sync → 409, 文案含"已有数据任务"
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**（注意：`_run_tracked` 提取主体时逐行保持既有语义——progress 闭包、`run_with_capacity` 调用、`JobCancelledError` 分支、finally release；既有 A股 调用方不传 trigger 默认 "daily" 不受影响）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `uv run pytest tests/test_market_sync_jobs.py tests/test_job_stall_and_cancel.py tests/test_daily_pipeline_minute_gate.py tests/test_us_market.py tests/test_crypto_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: ruff 检查**

### Task 4: 自动同步偏好 + scheduler 注册 + us 盘中门控 + settings/status API

**Files:**
- Modify: `backend/app/services/preferences.py`（新增 `AUTO_SYNC_INTERVALS: tuple[int, ...] = (5, 15, 30, 60)`；`get_market_auto_sync(market: str) -> dict`（键 `f"{market}_auto_sync"`，缺省 `{"enabled": False, "interval_minutes": 15}`，interval 不在集合内回退 15、enabled 强转 bool）；`set_market_auto_sync(market, enabled, interval_minutes)`（interval 非法 `raise ValueError`）；`get_data_page_market() -> str`（值域 stock/crypto/us，非法回退 "stock"）；`set_data_page_market(market)`（非法 `raise ValueError`））
- Modify: `backend/app/jobs/daily_pipeline.py`（`CRYPTO_AUTO_SYNC_JOB_ID = "crypto_auto_sync"`、`US_AUTO_SYNC_JOB_ID = "us_auto_sync"`；`_us_auto_sync_allowed() -> bool`＝`us_trading_day.is_us_trading_day(us_trading_day.us_today()) and market_time.us_in_session()`；`_register_crypto_auto_sync_job(scheduler, repo)` / `_register_us_auto_sync_job(scheduler, repo)`：读偏好，disabled → `remove_job`（容错不存在），enabled → `add_job(_wrapped, trigger=IntervalTrigger(minutes=interval), id=..., misfire_grace_time=60, replace_existing=True)`；`_wrapped`：auto 静默跳过（`job_store.active_id()` 非 None → info 日志直接 return；us 另加 `_us_auto_sync_allowed()` 门 → debug 日志 return），否则 `_run_tracked(task_fn, f"{market}_sync", trigger="auto")`；`start_scheduler` 末尾各调一次注册）
- Modify: `backend/app/api/settings.py`（preferences payload ~:707 增加 `"crypto_auto_sync"`、`"us_auto_sync"`、`"data_page_market"`；新增 `PUT /preferences/market-auto-sync`（body `{market: Literal["crypto","us"], enabled: bool, interval_minutes: int}`，ValueError→422；改后按 review-schedule 先例重注册/移除 scheduler job）；新增 `PUT /preferences/data-page-market`（body `{market: Literal["stock","crypto","us"]}`））
- Modify: `backend/app/api/data.py`（`status` payload 调度段增加 `"next_crypto_auto_run": _next_cron_run(scheduler, "crypto_auto_sync")`、`"next_us_auto_run": _next_cron_run(scheduler, "us_auto_sync")`）
- Test: `backend/tests/test_market_sync_jobs.py`（追加）、`backend/tests/test_us_market.py`（追加 gate 用例）

**Interfaces:**
- Consumes: Task 3 的 `_run_market_sync` / `_run_tracked(trigger=)`；Task 1 的 on_progress。
- Produces: 偏好键 `crypto_auto_sync` / `us_auto_sync` / `data_page_market`；`GET /api/settings/preferences` 新三键；`_us_auto_sync_allowed()`（Task 4 测试与 us 自动任务共用）。
- scheduler job id：`crypto_auto_sync` / `us_auto_sync`（`/api/data/status` 的 next-run 键依赖这两个 id）。

- [ ] **Step 1: 写失败测试**

```python
def test_market_auto_sync_prefs_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "Preferences_FILE", tmp_path / "prefs.json")  # 仿既有偏好测试的落盘隔离方式
    preferences.set_market_auto_sync("crypto", True, 5)
    assert preferences.get_market_auto_sync("crypto") == {"enabled": True, "interval_minutes": 5}
    with pytest.raises(ValueError):
        preferences.set_market_auto_sync("crypto", True, 7)
    assert preferences.get_market_auto_sync("us") == {"enabled": False, "interval_minutes": 15}  # 缺省

def test_us_auto_sync_gate(monkeypatch):
    from app.jobs import daily_pipeline
    from app import market_time
    monkeypatch.setattr(market_time, "us_in_session", lambda: False)
    assert daily_pipeline._us_auto_sync_allowed() is False
    monkeypatch.setattr(market_time, "us_in_session", lambda: True)
    monkeypatch.setattr(us_trading_day, "is_us_trading_day", lambda d: False)
    assert daily_pipeline._us_auto_sync_allowed() is False
    monkeypatch.setattr(us_trading_day, "is_us_trading_day", lambda d: True)
    assert daily_pipeline._us_auto_sync_allowed() is True

def test_register_auto_jobs_respects_prefs(...):
    # stub scheduler (add_job/remove_job 记录调用): enabled prefs → add_job(trigger.minutes==档位, id 正确)
    # disabled prefs → remove_job(id)；非法持久化 interval → 回退 15

def test_auto_wrapped_silent_skip_creates_no_record(...):
    # 取 captured wrapped: job_store 有活跃任务时调用 → 不新增任何 job 文件
    # (us 再加 gate=False 场景 → 同样零记录); 无占用+gate=True → 恰好 1 条 trigger=auto 记录
```

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**（`_wrapped` 内 `from app.services.pipeline_jobs import job_store` 惰性导入；`add_job` 传**函数对象**而非 lambda 包协程——本处为同步函数，lambda 可用，但与 review job 注记保持一致用具名函数）

- [ ] **Step 4: 运行确认通过 + 回归**

Run: `uv run pytest tests/test_market_sync_jobs.py tests/test_us_market.py tests/test_trading_rules.py tests/test_crypto_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: ruff 检查**

### Task 5: 前端 API 契约（api.ts + queryKeys.ts）

**Files:**
- Modify: `frontend/src/lib/api.ts`（`pipelineJobs(limit = 20, label?: string)` 拼 `&label=`；`PipelineJob`/`PipelineJobSummary` 增加 `label?: string | null; trigger?: 'manual' | 'daily' | 'auto' | null`；`Preferences` 增加 `crypto_auto_sync: MarketAutoSync; us_auto_sync: MarketAutoSync; data_page_market: 'stock' | 'crypto' | 'us'`（`type MarketAutoSync = { enabled: boolean; interval_minutes: number }`）；数据页 status 类型（`===== Data status =====` 段）增加 `next_crypto_auto_run: string | null; next_us_auto_run: string | null`；新增 `cryptoUniverse()` / `updateCryptoUniverse(spot: string[], futures: string[])`（GET/PUT `/api/crypto/universe`）、`usUniverse()` / `updateUsUniverse(symbols: string[])`（GET/PUT `/api/us/universe`）、`cryptoConcepts()` / `updateCryptoConcepts(concepts: Record<string, string[]>)`（GET/PUT `/api/crypto/concepts`）、`updateMarketAutoSync(market, enabled, intervalMinutes)`、`updateDataPageMarket(market)`）
- Modify: `frontend/src/lib/queryKeys.ts`（`marketSyncJobs: (label: string) => ['pipeline-jobs', label] as const`；**不动**现有 `pipelineJobs` 数组键，A股 历史查询零影响）

- [ ] **Step 1: 按上述契约修改两个文件**（纯类型与请求封装，无逻辑分支）
- [ ] **Step 2: 验证**

Run: `cd frontend && pnpm build`
Expected: 构建通过（若 `Preferences` 新字段为必选导致既有 mock/测试对象缺字段，按 vitest 报错逐处补默认值）

### Task 6: 三个新组件 + 组件测试

**Files:**
- Create: `frontend/src/components/data/SyncJobLogCard.tsx`
- Create: `frontend/src/components/data/AutoSyncSettingsCard.tsx`
- Create: `frontend/src/components/data/MarketSyncCard.tsx`
- Test: `frontend/src/components/data/AutoSyncSettingsCard.test.tsx`、`SyncJobLogCard.test.tsx`（仿 `MinuteSyncConfig.test.tsx`：`@vitest-environment jsdom` + `vi.mock('@/lib/api')` + QueryClient 包裹）

**Interfaces:**
- Consumes: Task 5 全部 api 函数与类型；`QK.marketSyncJobs(label)`。
- Produces: `SyncJobLogCard({ market: 'crypto' | 'us' })`；`AutoSyncSettingsCard({ market })`；`MarketSyncCard({ market })`。
- 行为约定：
  - SyncJobLogCard：`useQuery(QK.marketSyncJobs(label), api.pipelineJobs(20, label))`，`refetchInterval`：含 pending/running 行 → 3000，否则 60000；行渲染 = 时间（finished_at ?? started_at）+ trigger 徽章（手动/定时/自动）+ 状态（成功/失败/进行中）+ 耗时 + 摘要（result 的 instruments/daily.rows/minute.rows）+ 失败 `error` 与成功记录里 `daily.errors` 摘要（截 3 条）；`skipped === 'us_holiday'` 显示"节假日跳过"；空态"暂无同步记录"。
  - AutoSyncSettingsCard：偏好读自 `QK.preferences`；开关 + 档位 select（5/15/30/60）；保存 → `updateMarketAutoSync` → invalidate `QK.preferences` + `QK.dataStatus`；"下次运行"读 dataStatus 的 `next_{crypto,us}_auto_run`；文案 crypto「7×24 每 N 分钟同步（含分钟K）」us「仅美股交易日盘中每 N 分钟同步，收盘后由每日任务兜底」。
  - MarketSyncCard：内容 = 现 `CryptoSyncConfig`/`USSyncConfig` 弹窗体（手动同步两按钮 + 上次结果摘要，查询键沿用 `['crypto-sync-status']`/`['us-sync-status']`；同步 mutation onSuccess 额外 invalidate `QK.marketSyncJobs(label)` 与 `QK.dataStatus`）+ 白名单编辑入口（crypto：现货/合约两个 textarea 弹层 + 「概念映射」JSON textarea 弹层；us：代码 textarea 弹层；保存调 Task 5 的 update* API，失败 toast 不假成功）。

- [ ] **Step 1: 写失败测试**

```tsx
// AutoSyncSettingsCard.test.tsx: preferences mock crypto_auto_sync={enabled:true,interval_minutes:15}
it('渲染当前档位并以新档位+开关提交', async () => {
  // 选择 30 → 保存 → expect(api.updateMarketAutoSync).toHaveBeenCalledWith('crypto', true, 30)
})
// SyncJobLogCard.test.tsx:
it('渲染 trigger 徽章与失败原因', ...)   // 一条 succeeded/auto + 一条 failed(error 含 "boom")
it('空列表显示空态', ...)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd frontend && pnpm vitest run src/components/data/AutoSyncSettingsCard.test.tsx src/components/data/SyncJobLogCard.test.tsx`
Expected: FAIL（组件不存在）

- [ ] **Step 3: 实现三个组件**

- [ ] **Step 4: 测试通过**

Run: 同 Step 2 → PASS

- [ ] **Step 5: 旧弹窗组件暂留**（`CryptoSyncConfig.tsx` / `USSyncConfig.tsx` 此时仍被 Data.tsx 引用，留待 Task 7 接线时一并删除；本步只确认 `pnpm build` 仍通过）

### Task 7: Data.tsx 市场切换 + MarketSyncSection 组装

**Files:**
- Create: `frontend/src/components/data/MarketSyncSection.tsx`（`({ market }) => null` 型三卡组装：表统计条（dataStatus 的 `{crypto,us}_enriched` / `_instruments`，字段渲染方式仿页面现有 StatCard 对 `status.daily` 的取值）+ `MarketSyncCard` + `AutoSyncSettingsCard` + `SyncJobLogCard`）
- Modify: `frontend/src/pages/Data.tsx`

**Interfaces:**
- Consumes: Task 6 三组件；`api.updateDataPageMarket`；`QK.preferences`。
- 行为约定：
  - `pageMarket` 读自 `QK.preferences` 的 `data_page_market`；切换 mutation `updateDataPageMarket` → invalidate preferences。
  - 切换器：内容区顶部（`mx-auto px-8 py-6` 容器第一行），三段 segmented（A股 / 加密货币 / 美股）。
  - `pageMarket === 'stock'` → 现有全部内容原样渲染（PageHeader right 全套按钮在内）；否则内容区仅渲染 `<MarketSyncSection market={pageMarket} />`，且 PageHeader `right` 仅保留 数据源链接 + 页面设置 两项（立即同步/停止/数据范围/自检修补/扩展数据/测试端点/清除数据 为 A股/数据源语义，全部隐藏）。

- [ ] **Step 1: 实现组件与页面接线**（现有大块 JSX 包进 `pageMarket === 'stock'` 条件，不改动块内部；接线完成后删除 `CryptoSyncConfig.tsx` / `USSyncConfig.tsx` 及其引用与弹窗开关状态）
- [ ] **Step 2: 验证**

Run: `cd frontend && pnpm vitest run src/components/data/ && pnpm build`
Expected: PASS；构建通过

- [ ] **Step 3: 手工核对**（启动前后端，三市场切换：A股 内容与改动前一致；crypto/us 三卡展示、手动同步触发后日志卡出现 manual 行、自动同步开关保存后 dataStatus 出现 next_*_auto_run；窄屏 900px 无遮挡）

### Task 8: 全量验证

- [ ] **Step 1: 后端受影响模块全量**

Run: `cd backend && uv run pytest tests/test_crypto_pipeline.py tests/test_us_market.py tests/test_market_sync_jobs.py tests/test_job_record_persistence.py tests/test_job_stall_and_cancel.py tests/test_pipeline_capacity.py tests/test_daily_pipeline_minute_gate.py tests/test_trading_rules.py tests/test_crypto_overview.py -q`
Expected: 全部 PASS

- [ ] **Step 2: ruff 全部改动文件 + git diff --check**（无告警、无空白错误）

- [ ] **Step 3: 前端构建 + 既有测试**

Run: `cd frontend && pnpm build && pnpm vitest run`
Expected: PASS

- [ ] **Step 4: 汇报**：`git status`/`git diff --stat` 列出改动；逐条对照 Review Focus 说明验证证据；**不 commit**，等用户确认后统一 `git commit -s`。
