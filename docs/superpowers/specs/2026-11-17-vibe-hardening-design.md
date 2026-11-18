# Vibe-Hardening 设计 — 借镜 Vibe-Trading 的 6 项加固

日期：2026-11-17
来源调研：HKUDS/Vibe-Trading（MIT，38k★）源码结合点分析（会话内完成，clone 位于 /tmp/vibe-trading-research/Vibe-Trading）
状态：已与需求方对齐功能清单（#1–#6）；构建层决策采用推荐项

## 背景与动机

对 Vibe-Trading 的源码调研发现 6 项可直接落在 tick-stock-panel 现有机制上的加固/增强，覆盖回测可信度、AI 策略安全、种子测试方法学、数据源契约与挖掘统计严谨性。全部为纯后端 + 测试改动，不引入对方架构（无 fallback 链、无 agent 内核、无 wide-panel 因子体系）。

## 目标 / 非目标 / 反目标

**目标**
1. AI 生成策略的模型 provenance 记录与回测区间重叠警告（evidence manifest）。
2. 正式回测附带 Monte Carlo 置换检验 + Bootstrap Sharpe 置信区间。
3. AI 策略 AST 安全校验堵住命名空间句柄逃逸缺口。
4. 种子/信号层的 lookahead 哨兵测试模板（毒化未来数据不变性）。
5. Provider 日线价格口径声明契约（防双重复权）。
6. 挖掘晋升门禁增加 OOS 置换检验维度。

**非目标**
- 不做前端 UI（evidence/validation 经现有 API JSON 自然透出，展示后补）。
- 不引入 Vibe-Trading 的数据 fallback 链、agent/swarm 内核、Shadow Account、券商连接器。
- 不改动回测撮合语义、复权计算口径本身。

**反目标（任何一条出现即失败）**
- 引入任何静默数据降级。
- 改变现有回测/挖掘结果数值（新增产物必须纯附加）。
- 破坏工作区未提交的 13 个种子文件及其测试（只增量扩展）。

## 已定决策（推荐项，需求方未另行选择）

| 决策 | 取值 | 理由 |
|---|---|---|
| 提交组织 | 单分支 feat/vibe-hardening，每功能一个 commit | 符合「一个提交一个目的」，PR 可整体或按 commit 拆 |
| #5 严格度 | fail-closed：源声明 adjusted 且声明 adj_factor 数据集 → 注册拒绝 | 符合仓库哲学；现有源默认 raw 零影响 |
| #2/#6 开关 | 默认开启，policy/参数可关 | 千次置换毫秒级；优化器试跑保持关闭 |
| #4 覆盖 | 13 个新种子（两个未提交测试文件） | 旧 25 个种子已有 fixture 快照覆盖，不重复 |

## 各功能设计

### F1 模型 provenance（借镜：run_card model_provenance）

- AI 策略保存时在 META 盖可选 `provenance` 戳：`{"source": "ai", "provider": ..., "model": ..., "generated_at": "<UTC ISO>"}`。provider/model 取自 `ai_provider.current_ai_provider()/current_ai_model()`（生成时刻已可用）。落点：`api/strategy.py` AI 保存路径（`_validate_meta_semantics` 通过后、写盘前）；需确认 META 语义校验对未知键的容忍度，若严格白名单则同步扩展。
- `backtest/evidence.py::EvidenceRecorder.finish()` 增加 `model_provenance` 块：读 `strategy.meta.provenance`；`config.end > provenance.generated_at` 时 `status: "warning"` 并说明「回测区间晚于模型生成时刻，策略可能反映生成时已知的行情」。无 provenance 的历史策略不输出该块（兼容）。
- KiT 侧本次不动前端；KiT checkpoint cutoff 已在服务层 meta.calendar 语境中，展示留待后续。

### F2 回测 validation（借镜：backtest/validation.py）

- 新模块 `app/backtest/validation.py`：
  - `permutation_test(trades, *, initial_cash, n_permutations=1000, seed=42)`：打乱交易 pnl 顺序重排净值 → Sharpe/最大回撤的置换分布 → `p_value_sharpe`、`p_value_max_drawdown` + 实际值分位。pnl 取 `TradeRecord.pnl_amount`（字段名以 engine 实际为准）。
  - `bootstrap_sharpe_ci(equity_values, *, n_bootstrap=2000, confidence=0.95, seed=42)`：净值一阶差分日收益 bootstrap → `ci_low/ci_high/prob_positive`。
- 接线：
  - `BacktestResultPolicy.include_validation: bool = True`；`optimizer_trial()` 置 False（轻量试跑不产 validation）。
  - `StrategyBacktestResult` 增 `validation: dict | None = None` 字段（默认 None，`asdict` 序列化自然兼容）。
  - `StrategyBacktestService.run()` 返回前（非 error、policy 允许、trades 非空）计算并填充；异常时 `{"status": "error", ...}` 不影响主结果（附加诊断产物，如实标注）。
  - `evidence.finish()` 增 `validation` 摘要块（存在时）。
- 复权口径注记：置换/Bootstrap 是交易序列统计，与价格复权口径无关。

### F3 AST 安全校验补强（借镜：_scan_runtime_reachable 思想，落地为分量级检查）

已确认缺口（本仓库 `getattr` 已全禁、import 白名单已挡 socket 等，但）：
- `import numpy.ctypeslib`：`_module_allowed` 只看根包 → 放行；随后 `numpy.ctypeslib.load_library(...)` 可加载任意动态库。
- 属性链中间分量不检查：`n = numpy; n.ctypeslib.load_library(...)` → dotted 根 `n` 不在 import 别名表 → 只查末位属性 `load_library`（不在 `_FORBIDDEN_ATTR_NAMES`）→ 放行。

修法（一处改动堵两面）：`_FORBIDDEN_ATTR_NAMES` 命名空间句柄名单应用于
1. `Import`/`ImportFrom` 模块路径的**每个分量**（`numpy.ctypeslib` → 分量 `ctypeslib` 命中 → 拒绝）；
2. `Attribute` dotted 链的**每个中间分量**（`n.ctypeslib.load_library` → `ctypeslib` 命中 → 拒绝）。

测试：现有 AI 安全测试文件补复现用例（点分导入、别名传播、合法白名单用法不误伤——`pl.DataFrame`、`np.random` 等必须继续通过）。

### F4 Lookahead 哨兵模板（借镜：test_lookahead.py 毒化不变性）

- 新共享 helper `backend/tests/backtest/_lookahead_sentinel.py`：
  - `assert_lookahead_free(strategy, paths, params, *, warmup_bars, seed=7)`：构造多 symbol 随机游走 panel → `compute_signals` 基线 → 从 `perturb_from`（默认中段）起对 OHLCV 逐列交替毒化 NaN/1e10 → 重算 → 断言 `[:perturb_from]` 的 entry/exit/score/信号 id 逐位不变。
  - 区分毒化失效与泄漏：毒化导致计算异常或后段全 NaN 属正常（后段本就不比较）；**截断点前行输出变化 = fail**，并列出（日期, symbol, 字段, 基线, 重算）样例。
- 接入 `test_opensource_seeds.py` 与 `test_quantjourney_seeds.py`（未提交文件，增量追加参数化用例，不改动现有用例）；helper 与现有 `_build_panel/_col` 复用。

### F5 价格口径声明契约（借镜：PRICE_CALIBER 教训）

本仓库复权模型 = provider 交 raw 日线 + `adj_factor` 数据集 → enriched 本地乘法前复权（`pipeline._apply_adj_factor`）。真实风险：自定义源交**已复权价**而系统再乘因子 → 双重复权。

- `ProviderCapabilities` 增字段 `daily_price_basis: Literal["raw", "adjusted"] = "raw"`（frozen dataclass 带默认，现有实现零影响）。
- `TickFlowProvider` 显式声明 `"raw"`。
- 自定义源 loader：加载时若 `daily_price_basis == "adjusted"` 且该源同时声明 `adj_factor` 数据集 → **拒绝注册**（fail-closed，错误信息说明双重复权风险）。
- 能力矩阵 API（`build_capability_matrix`）：daily 候选条目透出 basis；当前生效源为 adjusted 时矩阵条目带警示 note（跨源 adj_factor 场景无法注册期拦截，只能提示）。
- 契约测试 + 文档：`docs/plugin-development.md` / `docs/custom-data-source.md` 补 basis 声明语义。

### F6 挖掘置换门禁（借镜：bench_runner_strict 随机对照）

- `validation.permutation_test` 复用：挖掘运行时（walkforward 聚合后）对各折 OOS 曲线合成日收益做分块置换（block=5，保自相关）→ OOS Sharpe 置换 p 值。
- 候选产物：`_ARTIFACT_SCHEMA` 增 `oos_permutation_pvalue: pl.Float64`，`_OPTIONAL_ARTIFACT_COLUMNS` 增同名（历史工件无此列照常读取）。
- `evaluate_candidate_gate` 增可选参 `permutation_pvalue: float | None = None`：None 放行（v2 历史语义，同 `worst_fold_sharpe` 先例）；非 None 且 `> GATE_MAX_PERMUTATION_PVALUE(=0.1)` → 追加 reason。
- `docs/mining.md` 门禁文档同步。

## 约束

- 遵循 CONTRIBUTING.md 全部数据契约与验证矩阵；每功能 TDD（先复现/边界测试）。
- 新增 JSON 字段全部带默认/可选，旧读取方不受影响；evidence `schema_version` 保持 1（纯增量键）。
- 协议：本仓库代码原创 + 思想借鉴（Vibe-Trading MIT，未复制代码逐行移植；如实现中确需复制，文件头标注来源 commit）。
- 不提交、不推送由我自动执行——每 feature commit 前向用户展示（最终按会话权限规则确认）。

## 验证矩阵（最低要求）

| 功能 | 验证 |
|---|---|
| F1 | 单测：META 盖章往返、finish 警告块、历史策略无块；evidence 读取兼容 |
| F2 | 单测：置换 p 值在已知样本上的界（强策略小 p / 随机交易大 p）、bootstrap CI 覆盖、policy 关闭时不产出、异常隔离 |
| F3 | 复现用例：点分导入/别名传播被拒；白名单合法用法不误伤；现有全部安全测试通过 |
| F4 | 13 种子哨兵全过；故意注入未来函数的变异测试（临时改一行 shift 方向）确认哨兵能红 |
| F5 | 契约测试：默认 raw、adjusted+adj_factor 拒绝、矩阵透出；现有 provider 测试不回归 |
| F6 | 门禁单测：None 放行、超阈拒绝、schema 兼容旧工件；挖掘端到端（如现有测试基建允许） |

命令：`cd backend && uv run pytest tests/<affected> -q`、`uv run ruff check <files>`、`git diff --check`。

## 交付顺序

F3（独立、最小）→ F4（测试基建）→ F2 → F1（依赖 evidence 语境）→ F6（复用 F2 的置换实现）→ F5（独立契约改动）。每步对应一个 commit。
