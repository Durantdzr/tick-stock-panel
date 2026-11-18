# RD-Agent Phase B（M1）设计 — LLM 因子进化循环内嵌后端

日期：2026-11-18
来源调研：Phase A 端到端实测（`2026-11-18-rdagent-bridge-findings.md`）+ 本仓库编排/LLM/ext_data 惯例摸底
状态：已批准（范围采用推荐默认：纯后端；SOTA 因子纳入本期）

## 背景与动机

Phase A 实证了"RD-Agent 循环 × 自研回测"可行，但依赖 RD-Agent 本体运行时（LiteLLM/conda/Docker 残留/其知识库栈）。北极星 B = 循环内嵌本仓库后端：复用项目自有设施（ai_provider、worker、MiningJobManager、ext_data）与 A 的资产（评估内核、评估语义提示词），让"假设 → 因子实现 → 回测评估 → 反馈进化"成为产品的一等能力。

## 目标 / 非目标 / 反目标

**目标**
1. 五步进化循环（假设/编码/评估/反馈/记录）作为后端域模块 `app/ai_mining/`。
2. 可取消、有进度、持久化、跨重启可恢复的后台任务（复用 MiningJobManager/worker）。
3. REST + SSE API（启动/列表/详情/事件流/取消/结果）。
4. SOTA 因子经 ext_data 通道落地，面板因子库自动可见 `ext_llmevo_*`。
5. LLM 调用预算硬护栏；结构化 JSON 进化记忆（零 embedding 依赖）。

**非目标**
- 不做前端页面（引擎跑稳后另出 UI 计划）。
- 不做向量知识库/RAG（M2 候选）。
- 不做 fin_model/模型进化、不做分钟级数据。
- 不改既有回测撮合语义与 factor registry 四处同步模式。

**反目标（任何一条出现即失败）**
- 生成因子代码进入策略目录或绕过沙箱直接在主进程执行。
- LLM 调用无预算上限（可无限烧钱）。
- 静默吞掉因子执行/评估失败并伪造指标。
- 破坏主检出 vibe-hardening WIP 或既有 API/测试行为。

## 已定决策

| 决策 | 取值 | 理由 |
|---|---|---|
| LLM 客户端 | `generate_ai_text`（services/ai_provider.py:357），max_tokens=None 惯例 | 项目自有栈；无 response_format（智谱兼容） |
| JSON 解析 | 复用 `custom_signals_ai._extract_json_object` | 三级降级+轻量修复，现成 |
| 任务编排 | `MiningJobManager(kind="llm_evo", store=MiningRunStore(namespace="llm_evo"))` | 参数化已验证（mining_manager.py:38），免费获得状态机/取消/恢复/SSE |
| worker 注册 | worker.py 两处 elif 加 `llm_evo`（make_worker_task + _worker_entry） | 既有惯例 |
| 评估内核 | 下沉 `app/backtest/factor_panel.py`，scripts CLI 变薄壳 | 核心进 app、脚本薄 CLI（CONTRIBUTING 2.3） |
| 因子合同 | pandas 脚本：读 `panel.parquet` → 写 `factor_values.parquet`（symbol,date,value） | A 实证 glm-5.2 pandas 成功率高 |
| 沙箱 | `_validate_safety` 扩展（+pandas 白名单，显式禁 read_pickle 等）+ 子进程隔离（cwd jail/timeout/env 清理） | 与 ai_generator 同级信任、同款哲学 |
| 进化记忆 | 结构化 JSON（hypotheses/sota_factors/lessons）注入 prompt | 零 embedding 依赖 |
| SOTA 落地 | ext_data `write_ext_parquet` 逐日分区 → `ext_llmevo_*` | 面板/挖掘/回测零改动可用 |
| 分支 | 继续 feat/rdagent-bridge（A 产物在此） | B 依赖 A scripts |
| 循环默认 | 3 轮 × ≤2 因子/轮；修复重试 ≤3；max_llm_calls 硬上限 | 成本护栏 |

## 各功能设计

### F1 评估内核下沉（`app/backtest/factor_panel.py`）
- `evaluate_factor_values(factor_values, *, start, end, price_loader, asset_type, n_groups, rebalance, qlib_instruments) -> dict`：A 版全量迁移（鸭子引擎/三指标别名/metric_semantics/sanitize_json/EvalError）。
- `scripts/factor_panel_eval.py` 保留 CLI 薄壳（桥接外部进程契约不变），import 服务函数。
- 测试随迁（11 用例改 import 路径）。

### F2 沙箱执行器（`app/ai_mining/sandbox.py`）
- `validate_factor_code(code) -> None`：`AIStrategyGenerator._validate_safety(code, extra_allowed_import_modules={"pandas"})` + 追加禁用面：`pandas.read_pickle`/`pickle`/`marshal`（反序列化 RCE 面）、`socket` 系（AST 层无网保证）。
- `run_factor_script(workdir, *, timeout) -> subprocess 结果`：`sys.executable` 执行 `factor.py`，cwd=workdir，env 清理（只留 PATH/PYTHONPATH 必要项），stdout/stderr 截断回传。
- `load_factor_values(path) -> pl.DataFrame`：输出校验（列 symbol/date/value、dtype、行数>0），fail-closed。
- 边界如实声明：AST+子进程，无网络强制断开（AST 拦 socket），信任级别同 ai_generator。

### F3 提示词资产（`app/ai_mining/prompts/`）
- `factor-evolution.md`：面板 schema 摘要、评估语义（截面分层/Qn 绝对口径/T+1/涨跌停）、方向约定（反向因子取负）、进化纪律。
- proposal/coder/feedback 内联模板（`_SYSTEM_TEMPLATE` 风格，JSON 硬约束 + 固定结构示例）。

### F4 进化记忆（`app/ai_mining/memory.py`）
- `EvolutionMemory`：`hypotheses[]`（round/hypothesis/reason/metrics_summary/decision/lessons）、`sota_factors[]`（id/name/description/formulation/ic/ir/values_path）、`lessons[]`、`llm_calls` 计数。
- 读写用 `atomic_write_text`；dedup：候选因子值与 SOTA 截面相关 > 0.9 拒绝（fail 时回喂 LLM）。

### F5 循环组件（proposal/coder/evaluator）
- proposal：记忆摘要 + SOTA 列表 → JSON `{hypothesis, reason, factors[{name,description,formulation,direction}]}`；身份字段白名单校验。
- coder：代码生成（`_extract_code_block` + validate + repair ≤3，错误回喂）→ sandbox 执行 → 因子值。
- evaluator：`factor_panel.evaluate_factor_values` → dedup → 反馈 JSON（decision/observations/lessons）。

### F6 循环与运行时（loop.py / runtime.py / worker / lifespan）
- 五步编排：propose → implement（含修复循环）→ evaluate（含 dedup）→ feedback → record；进度事件 `{"phase","label","done","total"}`（mining 同形）；每步取消检查。
- `run_ai_mining_runtime(config, data_dir, engine, progress_cb, cancel_check, rss_sampler)`：worker 入口（spawn 子进程内 asyncio.run）。
- worker.py 两处 elif `llm_evo`（config 自由 dict）。
- main.py lifespan：`MiningRunStore(namespace="llm_evo")` + `MiningJobManager(kind="llm_evo")` + `recover_interrupted()` → `app.state.ai_mining_manager`。

### F7 API（`app/api/ai_mining.py`）
- `POST /api/ai-mining/runs`（参数校验/预算上限/running 防重入 409）、`GET /runs`、`GET /runs/{id}`、`GET /runs/{id}/events`（EventSourceResponse + Last-Event-ID）、`POST /runs/{id}/cancel`、`GET /runs/{id}/result`
- manager 缺失 503；run 存储于 `data/research/llm_evo/runs/`。

### F8 SOTA 落地（ext_data）
- 每轮 SOTA 因子：`ExtConfig(id=f"llmevo_{fid}", mode="timeseries", fields=[value:float])` + `ExtConfigStore.upsert` + `write_ext_parquet` 逐交易日分区 → `GET /api/factors` 自动出现 `ext_llmevo_{fid}_value`。

## 约束

- 全程 feat/rdagent-bridge worktree；主检出 WIP 不碰。
- 每任务一 commit（DCO `-s`）；不 push。
- LLM 预算：`max_llm_calls`（默认 120）硬上限，超限显式失败。
- 数据窗口 ~1 年：结论文档如实标注。
- 沙箱边界文档明示（无网络强制断开，AST 拦 socket；子进程隔离）。

## 验证矩阵（最低要求）

| 功能 | 验证 |
|---|---|
| F1 | 原 11 用例新路径全绿 + CLI 薄壳回归 |
| F2 | 正/危 import/read_pickle/超时/输出 schema 五类用例 |
| F5 | mock LLM 全链路 + 解析容错（围栏/尾逗号/脏字段） |
| F6 | 假 LLM 合成面板 1 轮进程内跑通 + worker kind 单测 |
| F7 | start/events/cancel/result + 400/404/409 |
| F8 | tmp_path 全链（照 test_ext_factors.py）+ `GET /api/factors` 可见 |
| 端到端 | 真 LLM 2 轮：manifest/events/summary/memory/ext_data 产物核验 + ruff 零告警 |

## 交付顺序

T1 文档 → T2 评估下沉 → T3 沙箱 → T4 提示词 → T5 记忆 → T6 组件 → T7 循环/worker/lifespan → T8 API → T9 端到端（真 LLM）→ T10 ext_data → T11 文档回填。
