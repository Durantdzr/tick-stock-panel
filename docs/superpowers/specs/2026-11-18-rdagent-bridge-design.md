# RD-Agent 桥接设计 — 借微软 RD-Agent 的因子挖掘循环（Phase A）

日期：2026-11-18
来源调研：microsoft/RD-Agent（MIT，15.4k★）源码结合点分析（会话内完成，clone 位于 /tmp/rdagent-research 与 ~/PycharmProjects/rdagent-bridge/RD-Agent）；技术报告 arXiv:2505.14738、RD-Agent(Q) arXiv:2505.15155
状态：已与需求方对齐（先 A 后 B；LLM 复用项目配置）

## 背景与动机

RD-Agent 用"提出假设 → LLM 实现因子代码 → 真实回测 → 反馈进化"的闭环自动化量化因子研发（RD-Agent(Q) NeurIPS 2025：<$10 成本、少 70% 因子数、约 2× ARR）。本仓库已有完整的回测证据链（AST 白名单 → 子进程隔离 → 前视审计 → IC/分层因子评估），唯独缺"R"侧（假设进化）。

调研确认 RD-Agent 的 Qlib 耦合**浅而集中**：全仓仅 1 个 `qrun` 调用点（`QlibFBWorkspace.execute`），官方以 `QLIB_FACTOR_RUNNER`/`QLIB_FACTOR_SCEN` 等环境变量暴露扩展缝；因子代码合同是"读数据文件 → 写结果文件"的 pandas 面板，与本仓库 enriched 日线面板天然兼容。因此可以用"换 Runner"的最小切口把它的进化循环接到自研回测上（Phase A），验证后再把循环内嵌进本仓库后端（Phase B，独立计划）。

## 目标 / 非目标 / 反目标

**目标**
1. 数据导出适配器：enriched stock 面板 → RD-Agent 因子代码可消费的 parquet 数据文件。
2. 评估适配器：RD-Agent 因子值面板 → 本仓库 `FactorBacktestService`（鸭子类型，零注册）→ JSON 指标（含 RD-Agent 三指标别名）。
3. 桥接环境：RD-Agent 源码 clone + 独立 venv，macOS 冒烟通过（GO/NO-GO 闸门）。
4. 自定义 Runner：替换 Qlib 回测为本地适配器调用，全程不触碰 qrun/Docker。
5. 端到端验证：≥1 个完整 `fin_factor` 循环（假设→实现→面板评估→反馈），指标交叉验证一致。

**非目标**
- 不做 fin_model / fin_quant（模型进化、因子-模型联合）。
- 不做 Phase B（进化循环内嵌 backend、Trace/知识库/UI 自研）——A 结束后出独立计划。
- 不改动本仓库回测撮合语义、复权口径、factor registry/scoring/matrix 四处同步模式。
- 不把 RD-Agent 生成的因子代码注入本仓库策略目录（只消费其因子**值**面板；AST 白名单与种子黄金测试不受影响）。

**反目标（任何一条出现即失败）**
- RD-Agent 生成代码进入本仓库执行边界（data/strategies、AST 白名单管辖域）。
- 评估适配器静默降级（symbol 映射失败、日期越界必须显式报错）。
- 把绝对收益伪装成"对基准超额收益"（Qlib 指标语义差异必须如实标注）。
- 破坏主检出（feat/vibe-hardening）的未提交改动。

## 已定决策

| 决策 | 取值 | 理由 |
|---|---|---|
| Phase A 范围 | 仅 `fin_factor` | 因子值评估是 B 的内核，先验证最薄闭环 |
| 分支策略 | worktree `feat/rdagent-bridge` 从 main 切出（~/PycharmProjects/tick-stock-panel-rdagent） | 主检出有 vibe-hardening WIP，不得混入 |
| 桥接目录 | ~/PycharmProjects/rdagent-bridge/（仓库外） | RD-Agent clone + venv + custom_runner 不污染本仓库 |
| 评估接入 | 鸭子引擎 + `FactorBacktestService.run`（backtest/factor.py:181） | 服务只要求 panel 有因子列，registry 白名单仅在 HTTP API 层 |
| 因子数据合同 | parquet（data.parquet / result.parquet），不用 h5 | 避免 backend venv 引入 pytables；clone 内补丁 2 处 |
| 适配器市场 | stock（A 股，2025-11-03..2026-11-16） | 数据最新最全；us 滞后、crypto 无换手率 |
| LLM 后端 | 复用项目 ai_base_url + secrets_store key + ai_model | 需求方已确认；embedding 端点支持性待验证，降级路径 SiliconFlow bge-m3 |
| 指标映射 | IC=ic_mean；annualized_return=Q1 分组年化（绝对）；max_drawdown=Q1 nav 回撤 | 对齐 feedback.py:17-21 契约；语义差异写进结论文档 |

## 各功能设计

### F1 数据导出适配器（借镜：RD-Agent factor_data_template/generate.py 的 D.features 导出）

- 新增 `backend/scripts/export_daily_pv.py`：核心纯函数 `panel_to_daily_pv(panel: pl.DataFrame) -> pl.DataFrame` + 薄 CLI（argparse + sys.path 注入，对齐 bench_*.py 惯例）。
- 输出 parquet：pandas MultiIndex 兼容的 long 表（datetime, instrument, $open/$close/$high/$low/$volume/$factor），qfq 已复权故 $factor=1.0。
- symbol 映射：仓库格式 ↔ Qlib 风格（SH600000/SZ000001），实现时先以 `instruments/instruments.parquet` + `normalize_symbol`（services/ext_data.py:602）实测确认，映射函数双向可逆并测试覆盖。
- 数据经 `BacktestEngine.load_panel`（backtest/engine.py:475）走正规 enriched 路径，不直读 parquet（CONTRIBUTING 禁绕过仓库层）。

### F2 评估适配器（借镜：RD-Agent read_exp_res.py 的指标回读）

- 新增 `backend/scripts/factor_panel_eval.py`：核心纯函数 `evaluate_factor_values(factor_values, repo, *, start, end, n_groups, ...) -> dict`；CLI 进程契约对齐 kit_predict_runner.py:1-17（stdin/args 输入、stdout 末行 JSON、stderr 进度、exit 1 报错）。
- 评估链：因子值 join enriched panel → 鸭子引擎（只实现 `load_panel`，模仿 tests/backtest/test_factor_batch.py:46-59）→ `FactorBacktestService.run(FactorConfig)` → asdict(FactorResult) 全量诊断。
- 三指标别名 + 诊断（IC 序列/IR/胜率/IC 衰减/分层净值/多空/覆盖率/换手）全部 JSON 可序列化；NaN/Inf → null（JSON 合规）。
- 失败语义 fail-closed：因子全 NaN、日期越界、symbol 无法映射 → exit 1 + 结构化错误，不伪造指标。

### F3 桥接环境与 macOS 冒烟（GO/NO-GO 闸门）

- ~/PycharmProjects/rdagent-bridge/RD-Agent（源码 clone，`pip install -e .`，Python 3.10/3.11 venv）。
- `.env` 由项目 secrets_store 读取生成（key 不回显）；embedding 用 curl 实测 runninghub 端点，不支持则降级 litellm_proxy + SiliconFlow bge-m3（届时向需求方要 key）。
- 冒烟四项逐项留证据：import 循环模块 / FactorFBWorkspace 执行 trivial 因子 / LiteLLM chat roundtrip / rdagent health_check。
- 任何硬失败（Linux-only 依赖）→ 停止并回报，回退 Docker Linux 容器或 NAS，不硬扛。

### F4 自定义 Runner 与 clone 补丁（借镜：官方 QLIB_FACTOR_RUNNER 扩展缝）

- `~/PycharmProjects/rdagent-bridge/custom_runner.py::TickPanelFactorRunner`：对齐 QlibFactorRunner 接口；复用 `process_factor_data`（scenarios/qlib/developer/utils.py:131）收集因子面板 → 转 parquet → subprocess 调 backend venv 的 factor_panel_eval.py → 解析 JSON → `pd.Series([IC, annualized, max_drawdown])` 赋 exp.result。
- clone 内补丁（各自单独 commit，便于迁移 B）：prompts.yaml 的 `qlib_factor_simulator` 改写为本面板真实语义（截面打分→top-N、T+1、费用、涨跌停）、输出格式与数据文件改 parquet；factor.py result 读取 h5→parquet；实验日期窗口对齐本仓库数据范围；rd_loop.py Qlib 表达式 base-features 校验置空。
- 装配：`QLIB_FACTOR_RUNNER=custom_runner.TickPanelFactorRunner` 等环境变量，零 RD-Agent 源码结构改动（补丁仅契约文本与文件格式）。

### F5 端到端验证与结论

- 小预算跑（循环数/因子数硬上限，每轮成本记录）。
- 成功标准：≥1 个完整循环四步全通且指标来自真实回测；同一因子手跑适配器与 RD-Agent trace 指标一致。
- 结论文档回填：alpha 有无、反馈质量、成本、macOS 可行性；Phase B 资产清单（可复用 prompts / factor_panel_eval 内核 / CoSTEER 进化策略要点 / 需重造部分）。

## 约束

- 本仓库侧只新增 scripts/ + tests/ + docs/，不触碰 app/ 现有模块、factor registry 四处同步模式、工作区 WIP 文件。
- RD-Agent 生成代码永远只在其工作区/桥接 venv 执行；评估输入只有因子**值**（数据，非代码）。
- LLM key 经 secrets_store → bridge .env 直写，不进日志、不进 git、不回显。
- 数据窗口仅 ~1 年，结论文档必须如实标注统计功效局限。
- 后端验证统一用主检出 venv：`backend/.venv/bin/python -m pytest`（worktree 无独立 venv）。

## 验证矩阵（最低要求）

| 功能 | 验证 |
|---|---|
| F1 导出 | `backend/.venv/bin/python -m pytest tests/test_export_daily_pv.py -q`（合成 panel：MultiIndex/列名/$factor=1/symbol 双向映射/空 panel 报错） |
| F2 评估 | `backend/.venv/bin/python -m pytest tests/backtest/test_factor_panel_eval.py -q`（正常/因子全 NaN/日期越界三路径） |
| 脚本质量 | `uv run ruff check scripts/factor_panel_eval.py scripts/export_daily_pv.py`（+新测试文件）零告警 |
| F3 冒烟 | 四项证据逐条记录；失败即停回报 |
| F5 端到端 | 循环四步全通 + 适配器/trace 指标交叉验证一致 |
| 提交 | `git diff --check`；每任务一个 commit（`feat/test/docs(scope): 描述`），不推送 |

## 交付顺序

1. 文档入库（spec + plan，本提交）
2. F1 导出适配器 + 测试（一个 commit）
3. F2 评估适配器 + 测试（一个 commit）
4. F3 桥接环境冒烟（仓库外，无 commit；GO/NO-GO 汇报）
5. F4 Runner + clone 补丁（rdagent-bridge 内 commit）
6. F5 端到端 + 结论文档（仓库内 docs commit）
