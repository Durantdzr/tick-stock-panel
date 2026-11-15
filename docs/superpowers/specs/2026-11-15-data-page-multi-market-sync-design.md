# 数据页分市场 + 同步日志 + 分钟级自动同步 设计

日期：2026-11-15
状态：待批准
范围：backend（us_sync / crypto_sync / daily_pipeline / preferences / api）+ frontend（Data 页重构）

## 意图与背景

数据页（`/api/data` 前端对应 `frontend/src/pages/Data.tsx`）目前是 A股专用：管道调度、复权、财务、分钟K等卡片全为 A股语义；crypto/us 只有两个手动同步弹窗（`CryptoSyncConfig` / `USSyncConfig`），仅有一行"最近一次结果"。

用户需求（本分支 feat/crypto-market-and-multi-source 上迭代）：

1. **数据页分市场**：与看板一致按市场（A股 / 加密货币 / 美股）切换。
2. **同步日志可见**：crypto/us 的每次同步（手动 + 定时 + 自动）有持久化历史，前端可查。
3. **自动同步**：按最低时间周期（分钟级档位）可开启，保持分钟级数据新鲜。

附带发现的**现存 P1 缺陷**（本次必须修复）：`_run_tracked` 以 `fn(on_progress=progress)` 调用任务函数（`jobs/daily_pipeline.py:1062`），但 `_crypto_sync_task()`（:1450）与 `_us_sync_task()`（:1469）零参定义——定时触发必然抛 `TypeError`，被通用 `except` 捕获后 job 记为 failed，**同步本体从未执行**。crypto 08:05 与美股 16:35 定时同步在当前分支上从未成功过。

## 目标 / 非目标 / 反目标

**目标**

- 数据页顶部市场切换（A股 / 加密货币 / 美股），偏好持久化。
- crypto/us 各自：常驻同步状态卡（手动同步 + 上次摘要 + 白名单/概念配置入口）、同步日志卡（最近 N 条，持久化）、自动同步设置卡（开关 + 档位）。
- 自动同步每轮 = 既有"含分钟K"同步语义（instruments → 日K增量 → enriched → 分钟K近窗）。
- 修复定时同步 P1（on_progress 契约）。

**非目标**

- 不开放 crypto 1 分钟档（本轮选定 5/15/30/60 分钟档位）。
- 不做分钟K增量游标优化（每轮按现有近窗语义重拉：crypto 3 天 / us 5 天窗口）。
- 不开放实时行情/监控/分钟策略（trading_rules 对 crypto/us 保持 fail-closed）。
- 不改 A股 既有卡片、管道与调度行为。
- 不动财务/复权/五档等 A股 数据集。

**反目标**

- 不引入第二套任务存储/日志文件/清理逻辑。
- 不绕过 provider 抽象或前端硬编码能力清单。

## 方案取舍（已与用户确认）

| 决策点 | 备选 | 选定 | 理由 |
| --- | --- | --- | --- |
| 分市场形态 | 单页三分区 / 顶部切换器 | **顶部切换器** | A股区块已很长；与看板 `dashboard_market` 模式一致 |
| 自动同步语义 | 仅日K提频 / 分钟级增量 / crypto 加 1 分钟档 | **分钟级增量（5/15/30/60 分钟档）** | 对齐"按最低时间周期保持新鲜"；1 分钟档在 us 侧限频不可行、crypto 侧写放大明显 |
| 日志存储 | 独立 JSONL / 复用 job_store | **复用 job_store** | 定时路径已在 job_store；手动路径纳入后零新增存储与清理，重启不丢 |
| us 自动同步时段 | 全天跑 / 仅盘中 | **仅盘中**（XNYS 交易日 + 09:30–16:00 ET） | 盘后由既有 16:35 定时任务兜底，避免夜间空跑 |

## 后端设计

### 1. P1 修复：on_progress 契约（前置）

- `crypto_sync.run_crypto_sync(repo, *, with_minute=False, on_progress=None)` 与 `us_sync.run_us_sync(repo, *, with_minute=False, skip_non_trading_day=False, on_progress=None)` 增加 `on_progress` 形参，分阶段回调（instruments / daily / enriched / minute），透传给既有 `sync_us_daily(on_progress=...)` 等。
- `_crypto_sync_task(on_progress=None)` / `_us_sync_task(on_progress=None)` 接受参数（`_run_tracked` 现有调用不变）。

### 2. job_store 扩展（label / trigger / 容量 / 失败映射）

- **schema 扩展**：`job_store.create()` 增加可选 `label`（`crypto_sync` / `us_sync`，手动与定时共用）与 `trigger`（`manual` / `daily` / `auto`）键，`_summary()` 透传。评审已核实原兜底方案（并入 result）不可行——失败/运行中行没有 result，会被 label 过滤漏掉，而失败记录正是日志卡的主要内容。
- **容量**：`max_jobs` 50 → 500（仍最旧驱逐）。5 分钟档自动同步 ≈ 288 条/天，历史滚动约 1-2 天；日志卡默认只展示最近 15-20 条，足够。
- **失败映射**：`run_crypto_sync` / `run_us_sync` 内部吞异常返回 `ok:False`，`_run_tracked` 现在会无条件 `succeed()`。任务包装器必须翻译：`ok=False` → 抛专用 `SyncFailedError`（`_run_tracked` 记 failed，错误信息入记录）；`ok=True` 且 `errors` 非空 → succeed，错误摘要随 result 展示；`skipped=us_holiday` → succeed 带跳过标记。

### 3. 手动同步纳入任务库

- `POST /api/us/sync`、`POST /api/crypto/sync` 从裸 `threading.Thread` 改为与定时同路径：`job_store.create()` → 后台线程执行 `_run_tracked` 同款流程（单飞 + 执行槽语义）。
- 409 语义从"同市场同步进行中"变为"已有数据任务在运行（可能为其他市场或 A股 任务）"，提示文案相应调整——单飞是全局的，这是纳入统一任务库的直接后果，如实呈现。
- 响应带 `job_id`，前端可跳转/轮询。

### 4. 自动同步任务与注册

- `jobs/daily_pipeline.py` 新增 `_register_crypto_auto_sync_job(scheduler, repo)` / `_register_us_auto_sync_job(scheduler, repo)`（仿 `_register_review_job` 模式）：
  - `IntervalTrigger(minutes=档位)`，id `crypto_auto_sync` / `us_auto_sync`；偏好 disabled 时 `remove_job`（存在才删）。
  - **start_scheduler 注册一次 + settings API 改偏好时重注册**（review_schedule 先例）。
  - 任务体 = `run_crypto_sync(repo, with_minute=True, on_progress=...)` / `run_us_sync(repo, with_minute=True, skip_non_trading_day=True, on_progress=...)`，经 §2 失败映射包装。
- **忙时静默跳过**：自动同步触发时若全局单飞或执行槽被占（A股 管道/其他市场同步运行中），**跳过且不创建 job 记录**（info 日志即可），与 session 门控同语义，避免 5 分钟档产生失败记录刷屏。`_run_tracked` 加 `quiet_skip` 形参供 auto 路径使用（手动/定时路径行为不变）。
- **与 A股 管道的碰撞评估**：us 自动同步被 session 门控隔离在北京时间约 21:30–05:00（含 DST），与 A股 09:10/15:35 等日间任务零重叠；crypto 白名单小、单轮耗时短，与 15:35 管道重叠概率低；若真发生，A股 管道被跳过属既有全局单飞语义（今日手动同步同样如此），日志可见。
- **us 盘中门控**：复用现有 `market_time.us_in_session()`（美东 09:30–16:00 + weekday）与 `us_trading_day.is_us_trading_day(us_today())` 组合成门控，不新增重复函数。非盘中时跳过且**不创建 job 记录**（仅 debug 日志）。
- crypto 7×24 无门控；数据源不可达时 job 记 failed（下轮自愈，符合现状语义）。

### 5. 偏好（services/preferences.py）

- `get/set_crypto_auto_sync`、`get/set_us_auto_sync`：`{"enabled": bool, "interval_minutes": 5|15|30|60}`，非法档位 fail-closed 拒绝（422 语义），非法持久化值读取时回退 disabled。
- `get/set_data_page_market`：值域 `stock|crypto|us`，默认 `stock`。
- 暴露入口跟随 review_schedule / minute_sync 的现有 settings API 模式（builder 核对具体路由）。

### 6. 任务查询

- `GET /api/pipeline/jobs` 增加 `label` 过滤参数（逗号分隔；label 为市场族 `crypto_sync` / `us_sync`，手动/定时/自动共用 label、以 trigger 字段区分来源）；不传为现有全量语义。
- `next_run_time`：interval job 与现有 `_next_cron_run` 同样可从 scheduler `get_job()` 读到，自动同步卡展示"下次运行"。

## 前端设计

- `pages/Data.tsx`：顶部市场 segmented（A股 / 加密货币 / 美股），值持久化到 `data_page_market`。按市场渲染：
  - **A股**：现有内容原样。
  - **crypto / us**：三个新卡 + 该市场的表统计（`/api/data/status` 已有 `crypto_enriched` / `crypto_instruments` / `us_enriched` / `us_instruments` 键，按市场分流显示）。
- `components/data/` 新组件：
  - `MarketSyncStatusCard`：手动同步按钮（含分钟K选项）+ 上次结果摘要 + 白名单（crypto 另加概念映射）配置入口。内容从现有 `CryptoSyncConfig` / `USSyncConfig` 弹窗升级为常驻卡（API 不变；弹窗去留由 builder 按页面信息架构定，倾向移除弹窗改为卡片）。
  - `SyncJobLogCard`：`pipelineJobs({label: 该市场 labels})` 列表——时间、触发方式、状态、耗时、行数摘要、错误摘要；有 running 行时 3s 轮询，否则 60s。
  - `AutoSyncSettingsCard`：开关 + 档位选择 + 说明文案（crypto：7×24 每档位同步一次；us：仅美股交易日盘中）+ 下次运行时间。
- `lib/api.ts` / `lib/queryKeys.ts`：pipelineJobs 加 label 参数；新增偏好读写；新查询键集中注册。

## 测试与验证

后端（`uv run pytest`，venv 在 `backend/.venv`）：

- on_progress 契约回归：任务函数缺参场景修复前必失败、修复后通过（模拟 `_run_tracked` 调用）。
- `run_us_sync` / `run_crypto_sync` 透传 on_progress 的阶段回调断言。
- 失败映射：`ok=False` → job failed 带错误；`ok=True`+`errors` 非空 → succeeded 带摘要；`skipped=us_holiday` → succeeded 带标记。
- 忙时静默跳过：auto 路径占位时不建 job 记录；手动/定时路径行为不变。
- us 盘中门控：盘中跑 / 盘后跳过不建 job / 节假日跳过 / 日历库降级路径。
- 偏好档位校验：非法值拒绝、存量非法值读取回退 disabled。
- 手动同步入 job_store：创建记录（label/trigger 键）、忙时 409。
- `/api/pipeline/jobs?label=` 过滤（含不传 label 的兼容、失败行可被过滤命中）。
- job_store 容量：500 驱逐顺序与最旧淘汰。
- 自动同步重注册：改档位/开关后 scheduler job 触发参数生效。
- `uv run ruff check` 改动文件；`git diff --check`。

前端：

- `pnpm build`。
- 组件测试参照 `MinuteSyncConfig.test.tsx` 模式：日志卡渲染/轮询、自动同步档位更新、市场切换门控与默认值。

## 约束（CONTRIBUTING 摘要）

- 单位/日期口径不动（provider 层已声明，本次不触碰）。
- 缓存一致性：同步完成后的 `invalidate_enriched_asset` / data-status 失效路径保持现状，新增 UI 轮询不改变后端缓存语义。
- fail-closed：偏好非法值、session 门控、单飞冲突都有明确行为。
- 最小范围：不改无关卡片；A股 区块零改动。

## 留给 builder 核对的点

1. 偏好更新走哪个既有 settings 路由（review_schedule / minute_sync 先例）。
2. interval job 的 `next_run_time` 读取与 `_next_cron_run` 的适配。
3. 现有同步弹窗组件升级为卡片后的去留与页面信息架构。

## 风险

- 东财美股盘中 5 分钟档限频：已有页级重试 + 断点续传兜底；观察日志，必要时建议用户提档到 15 分钟。
- 币安代理抖动（本机已知）：单轮失败记 failed，下轮自愈；不加重试风暴。
- 自动同步与手动同步并发：单飞 + 执行槽已有，前端 409 提示保持。
