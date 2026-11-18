# Vibe-Hardening 实现计划（借镜 Vibe-Trading 的 6 项加固）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 tick-stock-panel 落地 6 项加固：AI 策略模型 provenance 警告、回测置换检验/Bootstrap CI、AST 安全校验补强、种子 lookahead 哨兵测试、provider 价格口径契约、挖掘 OOS 置换门禁。

**Architecture:** 全部为纯后端增量改动：validation 作为新纯计算模块被回测服务与挖掘运行时复用；AST 补强是现有校验器的分量级收紧；provenance 走 META 文本改写 + evidence 增量键；basis 契约走 ProviderCapabilities 默认值扩展。不改撮合/复权计算语义。

**Tech Stack:** Python 3.11+、Polars、NumPy（置换/Bootstrap 用 `np.random.default_rng`）、pytest。零新依赖。

**Spec:** `docs/superpowers/specs/2026-11-17-vibe-hardening-design.md`（与本计划同读）

## Global Constraints

- 后端命令：`cd backend && uv run pytest <target> -q`；`uv run ruff check <files>`；提交前 `git diff --check`。
- 每功能一个 commit，消息格式 `feat/fix/test(scope): 描述`（中文正文遵循仓库现有风格）。不推送。
- 新增 JSON 字段全部可选/带默认；evidence `schema_version` 保持 `1`；旧 manifest/旧候选工件必须可读取。
- 工作区有未提交文件（13 个种子 + 2 个种子测试 + KiT 文档）：绝不回滚/覆盖；F4 只增量追加。
- 置换/Bootstrap 一律 `np.random.default_rng(seed)`，返回值非有限数写 `None`（evidence `allow_nan=False`）。
- 不引入 fallback、不改变任何现有回测结果数值。

## Review Focus

1. AST 收紧误伤合法代码：`np.linalg.norm` 等白名单深属性链必须继续通过（Task 1 正向用例钉死）。
2. trades < 20 或净值点 < 30 时 validation 返回 `{"status": "insufficient"}`，不得抛错或伪造 p 值（Task 3）。
3. 无 `provenance` 的历史策略，evidence 不输出 `model_provenance` 键（Task 4）。
4. 旧挖掘候选工件缺 `oos_permutation_pvalue` 列时门禁行为与今天完全一致（Task 5）。
5. 哨兵必须能红：一个故意读未来数据的探针策略必须被 helper 判失败（Task 2 变异用例）。

---

### Task 1: AST 安全校验补强（F3）

**Files:**
- Modify: `backend/app/strategy/ai_generator.py`（`_validate_safety`，约 474-591 行）
- Test: `backend/tests/test_strategy_safety_validation.py`（追加用例；先读该文件现有的合法策略代码构造模式并复用）

**Interfaces:**
- Consumes: 现有 `_FORBIDDEN_ATTR_NAMES`、`_module_allowed`、`_dotted_name`、`module_aliases`。
- Produces: 行为契约——`Import`/`ImportFrom` 模块路径与 `Attribute` dotted 链的**每个分量**命中 `_FORBIDDEN_ATTR_NAMES` 即拒绝。无新公共 API。

- [ ] **Step 1: 写失败测试**（追加到 `test_strategy_safety_validation.py`，代码构造沿用该文件现有 helper）

```python
def test_dotted_import_of_forbidden_component_rejected():
    # import numpy.ctypeslib 后可 load_library 加载任意动态库
    code = VALID_STRATEGY_TEMPLATE.format(extra_import="import numpy.ctypeslib")
    with pytest.raises(ValueError, match="ctypeslib"):
        AIStrategyGenerator._validate_safety(code)

def test_attribute_chain_middle_component_rejected():
    # 普通赋值别名传播: n = numpy; n.ctypeslib.load_library(...)
    code = VALID_STRATEGY_TEMPLATE.format(extra_import="import numpy")
    code = code.replace("import numpy", "import numpy\nn = numpy")
    code = code.replace(
        "def compute_signals",
        "def _probe():\n    return n.ctypeslib.load_library('x')\n\n\ndef compute_signals",
    )
    with pytest.raises(ValueError, match="ctypeslib"):
        AIStrategyGenerator._validate_safety(code)

def test_whitelist_deep_attribute_chain_still_allowed():
    # 正向: 白名单模块的合法深属性不得误伤
    code = VALID_STRATEGY_TEMPLATE.format(extra_import="import numpy")
    code = code.replace("import numpy", "import numpy.linalg")
    code = code.replace(
        "def compute_signals",
        "def _probe():\n    return float(numpy.linalg.norm(numpy.ones(3)))\n\n\ndef compute_signals",
    )
    AIStrategyGenerator._validate_safety(code)  # 不抛
```

（`VALID_STRATEGY_TEMPLATE` 为该文件现有的最小合法策略代码构造；若无现成 helper 则按文件内其他用例的方式内联最小 META + 策略类。）

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/test_strategy_safety_validation.py -q -k "dotted_import or attribute_chain or whitelist_deep"`
Expected: 前两个用例 FAIL（未拒绝），第三个 PASS。

- [ ] **Step 3: 实现**（`_validate_safety` 内）

- 新增模块级 helper `_forbidden_path_component(path: str) -> str | None`：按 `.` 分量检查，返回第一个命中 `_FORBIDDEN_ATTR_NAMES` 的分量。
- `ast.Import`：`_module_allowed(alias.name)` 通过后，再查 `_forbidden_path_component(alias.name)`，命中即 `raise ValueError(f"禁止 import {alias.name} (命名空间句柄 {comp} 可绕道执行任意命令)")`。
- `ast.ImportFrom`：对 `node.module` 同样检查。
- `ast.Attribute` 分支：`dotted is not None` 时，在现有逻辑之前先查整条链分量（含中间分量），命中即拒绝（同样的中文错误格式）。

- [ ] **Step 4: 运行验证通过 + 全量回归**

Run: `uv run pytest tests/test_strategy_safety_validation.py tests/test_ai_strategy_meta_normalize.py tests/test_strategy_build_stream.py -q`
Expected: 全部 PASS（确认无误伤）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/strategy/ai_generator.py backend/tests/test_strategy_safety_validation.py
git commit -m "fix(strategy): AST 安全校验拦截命名空间句柄的导入与属性链分量"
```

---

### Task 2: Lookahead 哨兵模板 + 13 种子接入（F4）

**Files:**
- Create: `backend/tests/backtest/_lookahead_sentinel.py`
- Test: `backend/tests/backtest/test_lookahead_sentinel.py`（helper 自身测试）
- Modify（仅追加）: `backend/tests/backtest/test_opensource_seeds.py`、`backend/tests/backtest/test_quantjourney_seeds.py`

**Interfaces:**
- Consumes: `app.backtest.matrix.build_market_data_matrix`；两种子测试文件已有的 `MATRIX_STRATEGY` 导入与 params 字典（参数取自各文件现有用例）。
- Produces: `assert_lookahead_free(strategy, *, params: dict, warmup_bars: int, n_bars: int = 64, n_symbols: int = 3, seed: int = 7) -> None`——内部自建随机游走 panel，毒化未来段后断言截断点前信号不变。

- [ ] **Step 1: 写 helper 与其元测试（含变异探针）**

`_lookahead_sentinel.py` 行为契约：
- 构造：`seed` 驱动的多 symbol 随机游走 close（`np.cumsum(rng.normal(0, 0.5, (n_bars, n_symbols)) + 10.0)`，open=high=low=close，volume=1000），`build_market_data_matrix` 成矩阵。
- 基线：`strategy.compute_signals(market, params)`；截断点 `cut = max(warmup_bars + 2, n_bars // 2)`。
- 毒化：`cut + 1` 起按列奇偶交替——偶数列置 `NaN`、奇数列置 `1e10`（float 列）。
- 断言：重算后 `[0, cut+1)` 行的 `entry/exit`（bool 逐位相等）与 `score`（`np.array_equal(..., equal_nan=True)`）与基线一致；不一致 → `AssertionError` 列出至多 5 个 `(行号, symbol, 字段, 基线, 重算)` 样例。
- 毒化重算抛异常 → `pytest.skip(f"毒化导致计算失败: {type(exc).__name__}")`（与泄漏区分，显式可见）；基线计算异常照常抛出。
- probe 行选择：`cut` 行（截断前最后一根）必须参与比较。

`test_lookahead_sentinel.py` 三个用例：
1. 干净探针（最小 MA 交叉 MatrixStrategy，仅用 `valid_shift` 类回看）→ 通过；
2. 变异探针 `_LeakyProbe`（`compute_signals` 把 `close[-1]` 广播进每行 entry）→ `assert_lookahead_free` 必须 raise（哨兵能红）；
3. 毒化必炸探针（对毒化列做 `log` 引发异常）→ 走 skip 分支。

- [ ] **Step 2: 运行元测试**

Run: `uv run pytest tests/backtest/test_lookahead_sentinel.py -q`
Expected: 3 个 PASS。

- [ ] **Step 3: 两种子测试文件各追加一个参数化用例**（13 个种子全覆盖，params/warmup 取自各文件现有用例值）

```python
@pytest.mark.parametrize("strategy,params,warmup", [
    (KELTNER, {"window": 20, "atr_multiplier": 2.0}, 20),
    # ... 其余 7 个opensource + 5 个quantjourney 同格式
])
def test_seed_lookahead_sentinel(strategy, params, warmup):
    assert_lookahead_free(strategy, params=params, warmup_bars=warmup)
```

- [ ] **Step 4: 运行两个种子测试文件**

Run: `uv run pytest tests/backtest/test_opensource_seeds.py tests/backtest/test_quantjourney_seeds.py -q`
Expected: 新用例全 PASS；若某种子被哨兵判红 → 这是真实前视泄漏，停下修复种子而不是放宽哨兵。

- [ ] **Step 5: Commit**

```bash
git add backend/tests/backtest/_lookahead_sentinel.py backend/tests/backtest/test_lookahead_sentinel.py \
        backend/tests/backtest/test_opensource_seeds.py backend/tests/backtest/test_quantjourney_seeds.py
git commit -m "test(seeds): lookahead 哨兵模板接入 13 个新种子"
```

---

### Task 3: 回测置换检验 + Bootstrap CI（F2）

**Files:**
- Create: `backend/app/backtest/validation.py`
- Modify: `backend/app/backtest/strategy.py`（`BacktestResultPolicy` 加字段、`StrategyBacktestResult` 加字段、`run()` 成功路径收尾接入）
- Test: `backend/tests/backtest/test_validation_stats.py`（新建）

**Interfaces:**
- Produces:
  - `permutation_test(pnls: Sequence[float], *, initial_cash: float, n_permutations: int = 1000, seed: int = 42) -> dict`，键：`status / p_value_sharpe / p_value_max_drawdown / observed_sharpe / observed_max_drawdown / n_permutations`。`len(pnls) < 20` → `status="insufficient"`，p 值为 `None`。
  - `bootstrap_sharpe_ci(equity: Sequence[float], *, n_bootstrap: int = 2000, confidence: float = 0.95, seed: int = 42) -> dict`，键：`status / ci_low / ci_high / prob_positive / confidence`。有效点 `< 30` → insufficient。
  - `BacktestResultPolicy.include_validation: bool = True`；`optimizer_trial()` 置 `False`。
  - `StrategyBacktestResult.validation: dict | None = None`。
  - `attach_validation(result, *, policy, initial_cash) -> None`（strategy.py 内函数）：`policy.include_validation` 且 `not result.error` 且 `result.trades` 非空时计算并写 `result.validation`；任何异常写 `{"status": "error", "error": ...}`，绝不影响主结果。
  - equity 曲线日收益：`result.equity_curve` 元素为 dict，净值键以 `engine.py:1287` 附近的实际键为准（`"equity"`）；实现时先读该处确认。
- Consumes: `TradeRecord.pnl_amount`（`engine.py:219`）。

- [ ] **Step 1: 写失败测试**

- 置换：单调递增 pnl 序列（如 `[10, 20, ..., 200]`）→ `p_value_sharpe <= 0.05`（顺序本身显著优于乱序）；恒定 pnl `[50]*30` → `p_value_sharpe >= 0.5`；`len<20` → insufficient。
- Bootstrap：恒正漂移净值 → `ci_low > 0` 且 `prob_positive > 0.9`；零漂移噪声净值（固定 seed）→ `ci_low < 0 < ci_high`；点数不足 → insufficient。
- policy：`BacktestResultPolicy().include_validation is True`；`optimizer_trial("sharpe").include_validation is False`。
- attach：假 result（error=None, trades 带 pnl_amount, equity_curve）→ `validation` 被填充且 `status=="ok"`；policy 关 → 保持 `None`；compute 抛异常的注入路径 → `status=="error"` 且主 stats 不变。
- evidence：`EvidenceRecorder.finish()` 的返回 dict 在 `result.validation` 为 dict 时含 `validation` 键（用最小假 result + 假 strategy 构造；无 validation 时该键为 `None` 或缺省——实现取其一并测试钉死）。非有限值必须写 `None`（复用 `_scalar`）。

- [ ] **Step 2: 运行确认失败**（模块不存在 → import error）

- [ ] **Step 3: 实现**

`validation.py` 纯 NumPy：净值 = `initial_cash + cumsum(pnl)`；日收益 = `diff(equity)/equity[:-1]`；sharpe = `mean/std*sqrt(bars_per_year)` 不需要年化——置换分布内部自洽即可（同一尺度比较），观测与置换用同一计算函数 `_path_sharpe(equity)` / `_path_max_drawdown(equity)`。p 值 = `( perm >= observed ).mean()`（sharpe 单侧）；max_drawdown 用 `|perm| >= |observed|`。
`strategy.py`：在 `run()` 的成功收尾（result stats/curves 已装配处，读文件定位唯一成功 return 前）调 `attach_validation(result, policy=result_policy, initial_cash=config.initial_cash)`。

- [ ] **Step 4: 运行验证**

Run: `uv run pytest tests/backtest/test_validation_stats.py -q`，随后 `uv run pytest tests/backtest -q`（确认现有回测测试无回归）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/backtest/validation.py backend/app/backtest/strategy.py backend/tests/backtest/test_validation_stats.py
git commit -m "feat(backtest): 置换检验与 Bootstrap Sharpe 置信区间"
```

---

### Task 4: 模型 provenance（F1）

**Files:**
- Modify: `backend/app/api/strategy.py`（新增 `_stamp_meta_provenance`；`save_strategy_code_core` 内 `expected_source == "ai"` 注入块，约 889 行旁）
- Modify: `backend/app/backtest/evidence.py`（`EvidenceRecorder.finish()` 增 `model_provenance` 块）
- Test: `backend/tests/test_strategy_provenance.py`（新建，覆盖盖章函数 + evidence 块）

**Interfaces:**
- Consumes: `find_meta_assignment`、`_insert_meta_field`（api/strategy.py 现有文本改写基建）、`ai_provider.current_ai_provider()/current_ai_model()`、evidence `finish()` 内 `self.strategy.meta`。
- Produces:
  - META 可选键 `provenance: {"source": "ai", "provider": str, "model": str, "generated_at": "<UTC ISO>"}`（`repr(dict)` 文本注入，保证纯字面量可 `ast.literal_eval`；`_validate_meta_semantics` 不检查顶层未知键，已确认兼容）。
  - evidence 键 `model_provenance`：`{"source","provider","model","generated_at","backtest_end","status": "ok"|"warning"}`；`str(config.end)[:10] > generated_at[:10]` → warning；无 provenance → 不输出该键。

- [ ] **Step 1: 写失败测试**

- `_stamp_meta_provenance`：无 provenance 的代码 → 注入后 `ast.literal_eval(find_meta_assignment(...)[1])` 可解析且字段正确；已含 provenance → 覆盖为新值；代码其余部分逐字节不变。
- evidence：假 strategy（`meta` 含 provenance）+ `config.end="2026-06-30"`、`generated_at="2026-01-01T..."` → `status=="warning"`；`generated_at="2026-12-31T..."` → `"ok"`；meta 无 provenance → 返回 dict 无 `model_provenance` 键。

- [ ] **Step 2: 运行确认失败**

- [ ] **Step 3: 实现**

- `save_strategy_code_core` 的 `expected_source == "ai" and (legacy_ai_path or req.mode == "create")` 块内（与 `research_only` 注入同处）：惰性 import `ai_provider` 两个函数 + `datetime.now(UTC)`，构造 provenance dict 并 `_stamp_meta_provenance(prepared["code"], ...)`，随后重新 `_extract_meta`。
- `finish()`：`meta = getattr(self.strategy, "meta", None)`；`prov = meta.get("provenance") if isinstance(meta, dict) else None`；为 dict 时组装块（字段逐个 `_scalar`）插入返回 dict。

- [ ] **Step 4: 运行验证**

Run: `uv run pytest tests/test_strategy_provenance.py tests/backtest/test_validation_stats.py tests/test_ai_strategy_meta_normalize.py -q`

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/strategy.py backend/app/backtest/evidence.py backend/tests/test_strategy_provenance.py
git commit -m "feat(strategy): AI 策略模型 provenance 与回测区间重叠警告"
```

---

### Task 5: 挖掘 OOS 置换门禁（F6）

**Files:**
- Modify: `backend/app/backtest/validation.py`（新增 `permutation_pvalue(returns, *, n_permutations=1000, block=5, seed=42) -> float | None`，分块循环置换）
- Modify: `backend/app/backtest/mining_runtime.py`（候选行构建处，约 1209-1258 行：逐候选拼接各折 OOS 日收益 → p 值 → 新列；折记录若未保留 OOS 日收益，在折评估处补紧凑保留）
- Modify: `backend/app/services/mining_candidates.py`（`_ARTIFACT_SCHEMA` 增 `oos_permutation_pvalue: pl.Float64`；`_OPTIONAL_ARTIFACT_COLUMNS` 增同名；`_candidate_metrics` 透出；publish 门禁调用传参）
- Modify: `backend/app/backtest/mining.py`（`evaluate_candidate_gate` 增 `permutation_pvalue: float | None = None` + `GATE_MAX_PERMUTATION_PVALUE = 0.1`）
- Modify: `docs/mining.md`（门禁清单加一行）
- Test: `backend/tests/backtest/test_guardian.py`（gate 用例扩展，沿用其现有 `evaluate_candidate_gate(**params)` 模式）+ 候选产物 schema 测试（实现时 `grep -rl "mining_candidates" backend/tests/` 定位现有测试文件追加）

**Interfaces:**
- Produces: `permutation_pvalue(returns: Sequence[float], *, n_permutations, block, seed) -> float | None`（样本 < 30 返回 None）。
- 门禁语义：`permutation_pvalue is None` → 与现状完全一致；`p > 0.1` → reason `"requires an OOS permutation p-value of at most 0.1 (got {p:.3f})"`。

- [ ] **Step 1: 写失败测试**

- `permutation_pvalue`：趋势性收益序列 → p ≤ 0.1；纯噪声（固定 seed）→ p > 0.1 或 None；< 30 样本 → None。
- gate：现有全部用例不传新参仍 PASS（None 放行）；`permutation_pvalue=0.5` → qualified=False 且 reason 含 "permutation"；`=0.05` → 不新增 reason。
- schema：含 p 值列的候选帧 publish 正常；缺列旧帧读取不报错（`_OPTIONAL_ARTIFACT_COLUMNS` 语义）。

- [ ] **Step 2-4: 实现 → 验证**

先读 `mining_runtime.py:180-260`（折评估）与 `1023-1260`（候选行构建）确认每折 OOS 日收益的保留位置；若折记录无收益序列，在折评估处为 fold record 增加紧凑 `oos_daily_returns` 列表字段（已实现 OOS SimResult 有 equity_curve 可差分）。随后候选行 `p = permutation_pvalue(concat(fold_returns))`。全部测试通过 + `uv run pytest tests/backtest/test_guardian.py tests/backtest -q`。

- [ ] **Step 5: Commit**

```bash
git add backend/app/backtest/validation.py backend/app/backtest/mining_runtime.py \
        backend/app/services/mining_candidates.py backend/app/backtest/mining.py docs/mining.md backend/tests/backtest/
git commit -m "feat(mining): 晋升门禁加入 OOS 置换检验维度"
```

---

### Task 6: 价格口径契约（F5）

**Files:**
- Modify: `backend/app/data_providers/base.py`（`ProviderCapabilities` 增 `daily_price_basis: Literal["raw", "adjusted"] = "raw"`）
- Modify: `backend/app/data_providers/tickflow_provider.py`（显式 `daily_price_basis="raw"`）
- Modify: `backend/app/data_providers/custom/loader.py`（实现前先读 `custom/provider.py` + `loader.py` 的注册路径，定位自定义源 capabilities/datasets 声明点；在该处加组合校验）
- Modify: `backend/app/data_providers/capabilities.py`（daily 能力候选条目透出 `price_basis`；生效源为 adjusted 时该条目 `note` 附双重复权警示）
- Modify: `docs/plugin-development.md`、`docs/custom-data-source.md`（basis 声明语义小节）
- Test: `backend/tests/test_provider_price_basis.py`（新建）+ 现有 capabilities 矩阵测试追加断言

**Interfaces:**
- Produces: 字段契约——默认 `"raw"` 与现状等价；校验规则：源声明 `daily_price_basis="adjusted"` 且声明 `adj_factor` 数据集 → **注册拒绝**（ValueError，错误信息说明双重复权风险与改法）；跨源场景（本源 adjusted + 其他源提供 adj_factor）注册放行、矩阵 note 警示。

- [ ] **Step 1: 写失败测试**

- `ProviderCapabilities()` 默认 `daily_price_basis == "raw"`；tickflow 实例为 `"raw"`。
- 伪造 adjusted+adj_factor 声明走自定义源注册路径 → 拒绝且错误信息含「双重复权」。
- `build_capability_matrix` 输出的 daily 条目含 `price_basis` 键；adjusted 生效源时 note 非空。
- 既有 provider/插件测试全量不回归。

- [ ] **Step 2-4: 实现 → 验证**

先读 `custom/provider.py`、`custom/loader.py`、`custom/managed.py` 确认自定义源的 capabilities 与 datasets 声明机制（`grep -n "datasets\|capabilities" backend/app/data_providers/custom/*.py`），校验放在注册单插件/源的最窄入口。Run: `uv run pytest tests/test_provider_price_basis.py` + 现有 provider/matrix 测试文件（实现时定位：`grep -rl "build_capability_matrix" backend/tests/`）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/data_providers/ docs/plugin-development.md docs/custom-data-source.md backend/tests/
git commit -m "feat(data-providers): 日线价格口径声明与双重复权防护"
```

---

## 交付顺序与分支

分支：`feat/vibe-hardening`（自当前 HEAD 切出，携带未提交文件不动）。顺序：Task 1 → 2 → 3 → 4 → 5 → 6（F6 复用 Task 3 的置换实现故排后；F5 独立殿后）。每个 Task 结束即 commit，全部完成后跑 `uv run pytest tests/ -q` 全量回归 + `git diff --check`，最后向用户汇报变更清单、验证结果与剩余风险。
