"""回测附加统计检验 — 置换检验 / Bootstrap CI 的统计行为与接线契约。"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestResult,
    _attach_validation,
)
from app.backtest.validation import bootstrap_sharpe_ci, permutation_test


# ================================================================
# 置换检验: 交易顺序是否携带信息
# ================================================================
def test_permutation_detects_ordered_drift_as_significant():
    # 单调递增 pnl: 净值路径的斜率结构是乱序很难复现的
    result = permutation_test(
        [float(x) for x in range(10, 210, 10)], initial_capital=1_000_000.0
    )
    assert result["status"] == "ok"
    assert result["p_value_sharpe"] <= 0.05
    assert result["observed_sharpe"] > 0
    assert 0.0 <= result["p_value_max_drawdown"] <= 1.0


def test_permutation_constant_pnl_is_not_significant():
    # 恒定 pnl: 任何置换给出同一路径, 观测值就是分布中位数
    result = permutation_test([50.0] * 40, initial_capital=1_000_000.0)
    assert result["status"] == "ok"
    assert result["p_value_sharpe"] >= 0.5


def test_permutation_insufficient_trades():
    result = permutation_test([10.0] * 5, initial_capital=1_000_000.0)
    assert result["status"] == "insufficient"
    assert result["p_value_sharpe"] is None
    assert result["p_value_max_drawdown"] is None


# ================================================================
# Bootstrap Sharpe 置信区间
# ================================================================
def test_bootstrap_positive_drift_ci_excludes_zero():
    equity = [1_000_000.0 * (1.001 ** i) for i in range(120)]
    result = bootstrap_sharpe_ci(equity)
    assert result["status"] == "ok"
    assert result["ci_low"] > 0
    assert result["prob_positive"] > 0.9
    assert result["ci_low"] <= result["ci_high"]


def test_bootstrap_zero_drift_ci_contains_zero():
    rng = np.random.default_rng(11)
    equity = [1_000_000.0]
    for r in rng.normal(0.0, 0.01, size=120):
        equity.append(equity[-1] * (1 + r))
    result = bootstrap_sharpe_ci(equity, seed=11)
    assert result["status"] == "ok"
    assert result["ci_low"] < 0 < result["ci_high"]


def test_bootstrap_insufficient_points():
    result = bootstrap_sharpe_ci([100.0, 101.0, 102.0])
    assert result["status"] == "insufficient"
    assert result["ci_low"] is None


# ================================================================
# 接线: policy 门控 + 结果附加 + 异常隔离
# ================================================================
def test_policy_gates():
    assert BacktestResultPolicy().include_validation is True
    assert BacktestResultPolicy.optimizer_trial("sharpe").include_validation is False


def _result(trades, equity) -> StrategyBacktestResult:
    return StrategyBacktestResult(
        run_id="abc1234567",
        config={},
        stats={"sharpe": 1.0},
        equity_curve=equity,
        trades=trades,
    )


def test_attach_validation_fills_block():
    trades = [{"pnl_amount": float(x)} for x in range(10, 210, 10)]
    equity = [
        {"date": "2024-01-02", "equity": 1_000_000.0 * (1.001 ** i)}
        for i in range(120)
    ]
    result = _result(trades, equity)
    _attach_validation(result, policy=BacktestResultPolicy(), initial_capital=1_000_000.0)
    assert result.validation is not None
    assert result.validation["status"] == "ok"
    assert result.validation["permutation"]["status"] == "ok"
    assert result.validation["bootstrap"]["status"] == "ok"


def test_attach_validation_skipped_for_policy_off():
    result = _result([{"pnl_amount": 1.0}] * 30, [])
    _attach_validation(
        result,
        policy=BacktestResultPolicy.optimizer_trial("sharpe"),
        initial_capital=1_000_000.0,
    )
    assert result.validation is None


def test_attach_validation_skipped_without_trades():
    result = _result([], [{"equity": 1_000_000.0 * (1.001 ** i)} for i in range(120)])
    _attach_validation(result, policy=BacktestResultPolicy(), initial_capital=1_000_000.0)
    assert result.validation is None


def test_attach_validation_error_isolated():
    result = _result([{"pnl_amount": "not-a-number"}] * 30, [])
    _attach_validation(result, policy=BacktestResultPolicy(), initial_capital=1_000_000.0)
    assert result.validation is not None
    assert result.validation["status"] == "error"
    assert "Error" in result.validation["error"] or "ValueError" in result.validation["error"]
    assert result.stats == {"sharpe": 1.0}  # 主结果不受影响


# ================================================================
# evidence 留档
# ================================================================
def _recorder(tmp_path):
    from app.backtest.evidence import EvidenceRecorder

    config = SimpleNamespace(
        start="2024-01-01", end="2024-02-01", strategy_id="s1", asset_type="stock"
    )
    strategy = SimpleNamespace(
        composite=None, execution_backend="matrix_native", meta={}, file_path=None
    )

    class _Engine:
        def get(self, strategy_id):
            raise ValueError

    return EvidenceRecorder(tmp_path, config, strategy, _Engine())


def test_evidence_includes_validation_block(tmp_path):
    result = SimpleNamespace(run_id="abc1234567", stats={"sharpe": 1.0}, trades=[])
    result.validation = {"status": "ok", "permutation": {"p_value_sharpe": 0.01}}
    manifest = _recorder(tmp_path).finish(result, {"end": "2024-02-01"})
    assert manifest["validation"] == {"status": "ok", "permutation": {"p_value_sharpe": 0.01}}


def test_evidence_validation_absent_is_none(tmp_path):
    result = SimpleNamespace(run_id="abc1234567", stats={}, trades=[])
    manifest = _recorder(tmp_path).finish(result, {"end": "2024-02-01"})
    assert manifest["validation"] is None
