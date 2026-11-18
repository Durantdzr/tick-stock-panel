# RD-Agent 桥接实现计划（Phase A：借 RD-Agent 循环验证，评估接自研回测）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 RD-Agent 的 fin_factor 进化循环驱动本仓库回测做因子挖掘端到端验证：换 Runner 摆脱 Qlib/Docker，因子值经评估适配器进 FactorBacktestService，产出"LLM 挖因子在本框架上有没有 alpha"的实测结论与 Phase B 资产清单。

**Architecture:** 本仓库侧仅新增两个独立脚本（导出/评估适配器，均"纯函数内核 + 薄 CLI"）与测试；RD-Agent 侧在仓库外桥接目录以可编辑安装 + 环境变量换 Runner + 3 处最小 clone 补丁接线。评估链：因子值 parquet → 鸭子引擎 → FactorBacktestService.run → JSON（含 RD-Agent 三指标别名）。

**Tech Stack:** 本仓库 Python 3.12 + Polars + pytest（venv 复用主检出 backend/.venv）；桥接侧 Python 3.10/3.11 + rdagent（pip install -e）+ pandas + LiteLLM。本仓库零新依赖。

**Spec:** `docs/superpowers/specs/2026-11-18-rdagent-bridge-design.md`（与本计划同读）

## Global Constraints

- 本仓库命令：`cd ~/PycharmProjects/tick-stock-panel-rdagent/backend && /Users/durant/PycharmProjects/tick-stock-panel/backend/.venv/bin/python -m pytest <target> -q`；`uv run ruff check <files>`；提交前 `git diff --check`。
- 每任务一个 commit，消息 `feat/test/docs(scope): 描述`（中文，仓库现有风格）。不推送、不合并。
- 只新增文件：scripts/export_daily_pv.py、scripts/factor_panel_eval.py、tests/test_export_daily_pv.py、tests/backtest/test_factor_panel_eval.py、docs 两篇。不触碰 app/ 与任何 WIP 文件。
- 适配器 fail-closed：symbol 映射失败、日期越界、因子全 NaN → 结构化报错，禁止伪造指标；JSON 输出 NaN/Inf → null。
- 桥接目录：~/PycharmProjects/rdagent-bridge/；LLM key 从 secrets_store 直写 .env 不回显；RD-Agent 生成代码不进本仓库执行边界。
- 每轮 LLM 调用记录成本；循环数/因子数硬上限后再跑。

## Review Focus

1. symbol 双向映射必须可逆且经 instruments 实测确认（Task 2 先打印验证再写死映射表）。
2. 评估适配器鸭子引擎不得绕过 `BacktestEngine.load_panel` 的 enriched 正规路径（禁直读 parquet）。
3. 三指标别名语义：annualized_return 是 Q1 分组**绝对**年化，不是对基准超额——JSON 里必须带 `metric_semantics` 说明字段，防下游误读。
4. clone 补丁逐条独立 commit 且不改变 RD-Agent 循环骨架（补丁仅提示词文本/文件格式/日期窗口/校验置空）。
5. 端到端指标必须交叉验证：手跑 factor_panel_eval.py 与 RD-Agent trace 内指标一致才算通。

---

### Task 1: 设计文档入库（F0）

**Files:**
- New: `docs/superpowers/specs/2026-11-18-rdagent-bridge-design.md`
- New: `docs/superpowers/plans/2026-11-18-rdagent-bridge.md`

**Interfaces:** 无代码接口；后续任务的需求依据。

- [x] **Step 1:** 按 vibe-hardening 文档惯例撰写 spec 与 plan（已完成，即本文件与同目录 design）
- [ ] **Step 2:** `git diff --check` 后 `docs(rdagent): Phase A 桥接设计与实现计划` 一个 commit

### Task 2: 数据导出适配器（F1）

**Files:**
- New: `backend/scripts/export_daily_pv.py`
- Test: `backend/tests/test_export_daily_pv.py`

**Interfaces:**
- Consumes: `BacktestEngine.load_panel`（engine.py:475）；`normalize_symbol`（services/ext_data.py:602，实现时先实测仓库 symbol 格式）。
- Produces: `panel_to_daily_pv(panel, symbol_map) -> pl.DataFrame`（datetime/instrument/$open/$close/$high/$low/$volume/$factor）；CLI `--start --end --output [--asset-type stock]`。

- [ ] **Step 1:** 实测 symbol 格式：用 venv python 打印 instruments.parquet 头部与 normalize_symbol 输出，确定映射规则（记录到脚本 docstring）
- [ ] **Step 2: 写失败测试**（合成 8 天 × 3 symbol panel：MultiIndex 兼容 long 表结构、$ 列名、$factor=1.0、qfq 值透传、双向映射可逆、空 panel raise）
- [ ] **Step 3:** 运行确认失败 → 实现纯函数 + CLI → 测试通过
- [ ] **Step 4:** 真实数据冒烟：`--start 2026-09-01 --end 2026-11-16` 导出，检查行数与日期范围；ruff 零告警；commit `feat(scripts): RD-Agent 数据导出适配器`

### Task 3: 评估适配器（F2）

**Files:**
- New: `backend/scripts/factor_panel_eval.py`
- Test: `backend/tests/backtest/test_factor_panel_eval.py`

**Interfaces:**
- Consumes: `FactorBacktestService.run` / `FactorConfig`（factor.py:46-59,181）；独立构建模板 worker.py:212-230；进程契约 kit_predict_runner.py:1-17。
- Produces: `evaluate_factor_values(factor_values: pl.DataFrame, repo, *, start, end, n_groups=5) -> dict`（含 IC / 1day.excess_return_with_cost.annualized_return / max_drawdown 三别名 + metric_semantics 说明 + 全量诊断）；CLI `--factor-values <parquet> --start --end [--asset-type] [--n-groups]`，stdout 末行 JSON。

- [ ] **Step 1: 写失败测试**（模仿 test_factor_batch.py 鸭子引擎 + 合成 panel + 合成因子值：① 有效因子出全套指标与别名 ② 因子全 NaN → exit 语义/结构化错误 ③ 日期越界 → 结构化错误；NaN→null 断言）
- [ ] **Step 2:** 运行确认失败 → 实现纯函数（Q1 年化/回撤从 group_nav 计算）+ CLI → 测试通过
- [ ] **Step 3:** 用 Task 2 导出的真实数据 + 一个已知因子（如 momentum_20d 列）冒烟，人工核对 IC 合理性；ruff 零告警；commit `feat(scripts): RD-Agent 因子评估适配器`

### Task 4: 桥接环境 + macOS 冒烟（F3，GO/NO-GO 闸门）

**Files:**（仓库外 `~/PycharmProjects/rdagent-bridge/`）
- RD-Agent clone + Python 3.10/3.11 venv（pip install -e .）
- `.env`（secrets_store 读取直写：ai_base_url → OPENAI_API_BASE、key → OPENAI_API_KEY、ai_model → CHAT_MODEL）

- [ ] **Step 1:** clone + venv + 可编辑安装；失败任一步即停回报
- [ ] **Step 2:** embedding 实测：curl runninghub /v1/embeddings；不支持则向需求方要 SiliconFlow key 配 litellm_proxy/BAAI/bge-m3
- [ ] **Step 3:** 冒烟四项留证据：import 循环模块 / FactorFBWorkspace 执行 trivial pandas 因子（python_bin 指向桥接 venv）/ LiteLLM chat roundtrip / rdagent health_check
- [ ] **Step 4:** GO/NO-GO 结论汇报；NO-GO 则回报回退方案（Docker Linux / NAS）待决策

### Task 5: 自定义 Runner + clone 补丁（F4）

**Files:**（桥接目录）
- New: `custom_runner.py::TickPanelFactorRunner`（复用 scenarios/qlib/developer/utils.py:131 process_factor_data；subprocess 调 backend 评估适配器；exp.result ← pd.Series 三指标）
- Modify（clone 内逐条独立 commit）: `prompts.yaml`（simulator 语义改写 + parquet 合同 + 数据 schema）；`components/coder/factor_coder/factor.py`（result 读取 h5→parquet）；实验日期窗口（对齐 2025-11..2026-11 含预热）；`components/workflow/rd_loop.py`（base-features Qlib 表达式校验置空）

- [ ] **Step 1:** 先读 QlibFactorRunner（factor_runner.py）确认 develop 契约与 exp.result 消费方（feedback.py process_results）
- [ ] **Step 2:** 四条 clone 补丁逐条 commit，每条附一行动机说明
- [ ] **Step 3:** custom_runner.py + `.env` 装配（QLIB_FACTOR_RUNNER 等）；单跑一个手工因子工作区过 TickPanelFactorRunner 验证指标通路
- [ ] **Step 4:** 成本护栏：循环数/因子数上限写入启动脚本/文档

### Task 6: 端到端验证跑（F5）

- [ ] **Step 1:** `rdagent fin_factor` 小预算跑（≤3 循环 × ≤2 因子）
- [ ] **Step 2:** 成功标准核验：≥1 循环四步全通；手跑适配器 vs trace 指标交叉一致；失败因子得到结构化反馈并进入下轮
- [ ] **Step 3:** 记录每轮成本与 LLM 失败率；若代码生成成功率过低，停下回报重新选型（不擅自换端点）

### Task 7: 结论回填 + Phase B 输入（F5）

**Files:**
- New: `docs/superpowers/specs/2026-11-18-rdagent-bridge-findings.md`（结论 + B 资产清单）

- [ ] **Step 1:** 回填：alpha 有无、反馈质量、成本、macOS 可行性、数据窗口局限
- [ ] **Step 2:** B 资产清单：可复用 prompts、factor_panel_eval 内核、CoSTEER 进化策略要点、需自研部分（Trace/知识库/UI）
- [ ] **Step 3:** commit `docs(rdagent): Phase A 端到端结论与 Phase B 输入`

## 交付顺序与分支

分支 `feat/rdagent-bridge`（worktree：~/PycharmProjects/tick-stock-panel-rdagent，从 main 45813238 切出；基线 backtest 471 passed / 1 skipped）。仓库内交付顺序 Task 1 → 2 → 3 →（桥接 4/5）→ 6 → 7。Task 4 为闸门：不通过则停在 Task 3 后汇报。全程不推送；Phase B 独立计划另起。
