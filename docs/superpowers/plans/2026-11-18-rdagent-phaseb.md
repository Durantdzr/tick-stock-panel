# RD-Agent Phase B（M1）实现计划 — LLM 因子进化循环内嵌后端

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把五步 LLM 因子进化循环（假设→编码→评估→反馈→记录）内嵌为本仓库后端域模块 `app/ai_mining/`，复用 MiningJobManager/worker 编排与 ai_provider，SOTA 因子经 ext_data 落地面板；纯后端 + REST/SSE。

**Architecture:** 新域模块 `app/ai_mining/`（proposal/sandbox/coder/evaluator/memory/loop/runtime/prompts）；评估内核下沉 `app/backtest/factor_panel.py`；worker.py 注册 `llm_evo` kind；lifespan 挂参数化的 MiningJobManager；生成因子代码走 AST+子进程沙箱；进化记忆用结构化 JSON（零 embedding）。

**Tech Stack:** Python 3.11+（仓库既有）、ai_provider（chat_completions）、Polars/pandas（因子合同）、pytest。零新依赖。

**Spec:** `docs/superpowers/specs/2026-11-18-rdagent-phaseb-design.md`（与本计划同读）

## Global Constraints

- 命令：`cd ~/PycharmProjects/tick-stock-panel-rdagent/backend && /Users/durant/PycharmProjects/tick-stock-panel/backend/.venv/bin/python -m pytest <target> -q`；`uv run ruff check <files>`；提交前 `git diff --check`。
- 每任务一个 commit（`-s` DCO，`feat/test/docs(scope): 中文描述`）。不 push、不合并。
- 生成代码永不进策略目录；沙箱边界（AST+子进程，无网络强制断开）文档明示。
- `max_llm_calls` 预算硬上限；失败 fail-closed，不伪造指标。
- 主检出 WIP 不碰；ext_data 写入用既有 `write_ext_parquet`（自动失效缓存）。

## Review Focus

1. `_validate_safety` 扩展不得放松既有拦截（pandas 白名单只加根包，危险属性继续全拦；read_pickle 必须显式拒）。
2. worker `llm_evo` 分支不得在 import 期引入 ai_provider/循环模块（保持 worker 启动轻量，运行体内延迟 import）。
3. evaluator 的 dedup 相关性计算必须与指标同窗口，且 NaN 容错（全 NaN 候选直接拒绝）。
4. API running 防重入与预算校验要在 manager.start 之前（400/409 语义正确）。
5. 端到端前必须先跑通"假 LLM 进程内 1 轮"（Task 7），禁止直接烧真 LLM 调试逻辑。

---

### Task 1: spec + plan 文档入库（F0）

**Files:**
- New: `docs/superpowers/specs/2026-11-18-rdagent-phaseb-design.md`
- New: `docs/superpowers/plans/2026-11-18-rdagent-phaseb.md`

- [x] **Step 1:** 按惯例撰写（即本文件与同目录 design）
- [ ] **Step 2:** `git diff --check` 后 commit `docs(rdagent): Phase B M1 设计与实现计划`

### Task 2: 评估内核下沉（F1）

**Files:**
- New: `backend/app/backtest/factor_panel.py`（核心自 `scripts/factor_panel_eval.py` 迁入：`EvalError`/`_normalize_factor_values`/`evaluate_factor_values`/`_DuckPanelEngine`/`sanitize_json`/`_make_price_loader`）
- Modify: `backend/scripts/factor_panel_eval.py`（薄壳：argparse + import 服务函数，CLI/JSON 契约不变）
- Test: `backend/tests/backtest/test_factor_panel_eval.py`（11 用例改 import `app.backtest.factor_panel`，增 1 个 CLI 薄壳冒烟）

**Interfaces:**
- Produces: `evaluate_factor_values(...) -> dict`（签名不变）、`sanitize_json`、`EvalError`、`FACTOR_COL` 常量。

- [ ] **Step 1:** 迁移核心到 `app/backtest/factor_panel.py`（去 sys.path 注入与 argparse，保留纯函数；`_make_price_loader` 保留）
- [ ] **Step 2:** scripts 薄壳改写；测试 import 路径更新
- [ ] **Step 3:** 全部用例绿 + ruff 零告警；commit `refactor(backtest): 因子面板评估内核下沉 app 层, scripts 变薄壳`

### Task 3: 沙箱执行器（F2）

**Files:**
- New: `backend/app/ai_mining/__init__.py`、`backend/app/ai_mining/sandbox.py`
- Test: `backend/tests/ai_mining/test_sandbox.py`

**Interfaces:**
- Consumes: `AIStrategyGenerator._validate_safety(code, extra_allowed_import_modules=...)`（ai_generator.py:486）。
- Produces: `validate_factor_code(code)`、`run_factor_script(workdir, *, timeout=120) -> SandboxResult(stdout, stderr, returncode)`、`load_factor_values(path) -> pl.DataFrame`（fail-closed schema 校验）。

- [ ] **Step 1: 写失败测试**（合法 pandas 因子过检；`import subprocess`/`open(`/`eval`/`__import__`/`pandas.read_pickle` 拒；执行超时；输出缺列/空值全拒）
- [ ] **Step 2:** 实现：AST 校验（含 read_pickle 显式禁）→ subprocess（cwd=workdir、env 白名单清理、capture、timeout kill）→ `load_factor_values`
- [ ] **Step 3:** 测试绿 + ruff；commit `feat(ai_mining): 因子代码沙箱执行器`

### Task 4: 提示词资产（F3）

**Files:**
- New: `backend/app/ai_mining/prompts/factor-evolution.md`、`backend/app/ai_mining/prompts.py`（加载器 + proposal/coder/feedback 三个模板常量，仿 custom_signals_ai 风格）

- [ ] **Step 1:** 指南 md（面板 schema 摘要/评估语义/方向约定/纪律）+ 三模板（JSON 硬约束 + 固定结构示例；coder 模板含修复回喂段）
- [ ] **Step 2:** 冒烟：模板可 format 无 KeyError；commit `feat(ai_mining): 进化循环提示词资产`

### Task 5: 进化记忆（F4）

**Files:**
- New: `backend/app/ai_mining/memory.py`
- Test: `backend/tests/ai_mining/test_memory.py`

**Interfaces:**
- Produces: `EvolutionMemory`（dataclass: hypotheses/sota_factors/lessons/llm_calls）、`load_memory(run_dir)`、`save_memory(run_dir, mem)`、`is_duplicate_vs_sota(candidate_values: pl.DataFrame, sota_values: dict[str, pl.DataFrame], threshold=0.9) -> str | None`（返回冲突因子名）。

- [ ] **Step 1: 写失败测试**（round-trip、dedup 正/反例、全 NaN 候选拒、损坏 JSON 容错重建）
- [ ] **Step 2:** 实现（atomic_write_text；dedup 用 pandas 截面秩相关，逐日秩相关均值）
- [ ] **Step 3:** 绿 + ruff；commit `feat(ai_mining): 结构化进化记忆与 SOTA 去重`

### Task 6: proposal / coder / evaluator（F5）

**Files:**
- New: `backend/app/ai_mining/proposal.py`、`coder.py`、`evaluator.py`
- Test: `backend/tests/ai_mining/test_components.py`（monkeypatch `generate_ai_text`，不触网）

**Interfaces:**
- proposal: `async propose(memory, scen_ctx, *, factors_limit) -> Proposal(hypothesis, reason, factors[])`（白名单校验身份字段）
- coder: `async implement_factor(task, workdir, panel_path, *, max_repairs=3) -> FactorImpl(code, values, repair_history)`
- evaluator: `async evaluate_and_feedback(impls, eval_cfg, memory, scen_ctx) -> RoundResult(metrics_by_factor, decision, observations, lessons)`

- [ ] **Step 1:** 三组件实现（全部走 `generate_ai_text`，预算计数在 loop 层）
- [ ] **Step 2:** mock 测试：假设解析容错（围栏/尾逗号/多字段）、代码修复回喂路径（首次坏代码→错误回喂→修复成功）、evaluator 指标映射与反馈 JSON 解析
- [ ] **Step 3:** 绿 + ruff；commit `feat(ai_mining): 假设/编码/评估组件`

### Task 7: 循环 + runtime + worker + lifespan（F6）

**Files:**
- New: `backend/app/ai_mining/loop.py`、`backend/app/ai_mining/runtime.py`
- Modify: `backend/app/backtest/worker.py`（make_worker_task elif `llm_evo`；_worker_entry 分发 elif，延迟 import runtime）
- Modify: `backend/app/main.py`（lifespan：store+manager+recover_interrupted 挂 app.state.ai_mining_manager）
- Test: `backend/tests/ai_mining/test_loop.py`（假 LLM + 合成面板进程内 1 轮）；worker kind 注册单测（make_worker_task 返回 dict 断言）

- [ ] **Step 1:** loop 五步编排（进度事件 mining 同形、取消检查、预算计数、异常→run 失败不吞）
- [ ] **Step 2:** runtime 入口（spawn 子进程内 asyncio.run；面板供给：`BacktestEngine.load_panel` 写 workdir/panel.parquet）
- [ ] **Step 3:** worker 两处 elif；main.py lifespan 接线
- [ ] **Step 4:** 假 LLM 1 轮跑通 + worker 注册断言；commit `feat(ai_mining): 五步循环运行时与 worker/lifespan 接线`

### Task 8: API（F7）

**Files:**
- New: `backend/app/api/ai_mining.py`
- Modify: `backend/app/main.py`（include_router）
- Test: `backend/tests/ai_mining/test_api.py`（httpx AsyncClient + 假 manager）

- [ ] **Step 1:** 六端点（照 api/mining.py 模式：pydantic 请求约束、manager 503、409 防重入、预算校验 400、EventSourceResponse SSE）
- [ ] **Step 2:** API 测试 + ruff；commit `feat(api): AI 因子进化 runs API（启动/进度/取消/结果）`

### Task 9: 端到端验证（真 LLM）

- [ ] **Step 1:** 启动后端（worktree backend，DATA_DIR 指主检出），`POST /api/ai-mining/runs` 2 轮预算
- [ ] **Step 2:** SSE 观察；核验 run 目录（manifest/events/summary/memory.json/因子代码/values/ext parquet）
- [ ] **Step 3:** 指标与手跑 `factor_panel_eval.py` 交叉验证；取消路径演练；成本记录

### Task 10: SOTA 落地 ext_data（F8）

**Files:**
- Modify: `backend/app/ai_mining/evaluator.py`（或 loop record 步）+ `runtime.py`
- Test: `backend/tests/ai_mining/test_ext_landing.py`（tmp_path 全链，照 test_ext_factors.py 模式：upsert→write→ensure_synced→all_factors 可见→evaluate 按 ext id 可跑）

- [ ] **Step 1:** SOTA 因子逐日分区落盘 + 注册
- [ ] **Step 2:** 测试 + ruff；commit `feat(ai_mining): SOTA 因子 ext_data 落地面板`

### Task 11: 文档回填

**Files:**
- Modify: `docs/superpowers/specs/2026-11-18-rdagent-bridge-findings.md`（B-M1 结论增补）
- New: `docs/ai-mining.md`（五段式：边界/口径/沙箱边界/门禁与预算/资源）

- [ ] **Step 1:** 回填结论与成本；运维文档
- [ ] **Step 2:** commit `docs(rdagent): Phase B M1 结论与运维文档`

## 交付顺序与分支

分支 `feat/rdagent-bridge`（worktree `~/PycharmProjects/tick-stock-panel-rdagent`）顺序 T1→T11。T9 端到端前先向用户同步成本预估；全部完成后不自动 push/merge，合并策略由用户决定。
