"""回测附加统计检验: 交易顺序置换检验与 Bootstrap Sharpe 置信区间。

借镜 HKUDS/Vibe-Trading backtest/validation.py (MIT) 的方法, 按本仓库口径重写:
- 置换检验回答「这条净值路径是否只是交易排序的运气」: 打乱交易 pnl 顺序重排
  净值, 比较观测 Sharpe/最大回撤落在置换分布的哪一侧。
- Bootstrap 回答「日收益重抽样误差下 Sharpe 的置信区间与正收益概率」。
两者都是路径统计, 与复权口径无关; 输出只作诊断附件 (validation 块),
不改变回测主结果, 样本不足时如实返回 insufficient 而非伪造 p 值。
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

MIN_TRADES = 20
MIN_EQUITY_POINTS = 30


def _equity_path(pnls: np.ndarray, initial_capital: float) -> np.ndarray:
    return initial_capital + np.concatenate([[0.0], np.cumsum(pnls)])


def _path_sharpe(equity: np.ndarray) -> float:
    if equity.size < 2:
        return 0.0
    returns = np.diff(equity) / equity[:-1]
    std = returns.std()
    if not np.isfinite(std) or std == 0.0:
        return 0.0
    return float(returns.mean() / std)


def _path_max_drawdown(equity: np.ndarray) -> float:
    if equity.size < 2:
        return 0.0
    peak = np.maximum.accumulate(equity)
    with np.errstate(divide="ignore", invalid="ignore"):
        drawdown = equity / peak - 1.0
    return float(np.nanmin(drawdown))


def permutation_test(
    pnls: Sequence[float],
    *,
    initial_capital: float,
    n_permutations: int = 1000,
    seed: int = 42,
) -> dict:
    """打乱交易 pnl 顺序重排净值, 返回观测 Sharpe/回撤的置换 p 值。"""
    values = np.asarray(pnls, dtype=np.float64)
    insufficient = {
        "status": "insufficient",
        "p_value_sharpe": None,
        "p_value_max_drawdown": None,
        "observed_sharpe": None,
        "observed_max_drawdown": None,
        "n_trades": int(values.size),
    }
    if values.size < MIN_TRADES or not np.isfinite(values).all():
        return insufficient

    observed_path = _equity_path(values, initial_capital)
    observed_sharpe = _path_sharpe(observed_path)
    observed_dd = _path_max_drawdown(observed_path)
    rng = np.random.default_rng(seed)
    hits_sharpe = 0
    hits_dd = 0
    for _ in range(n_permutations):
        path = _equity_path(rng.permutation(values), initial_capital)
        if _path_sharpe(path) >= observed_sharpe:
            hits_sharpe += 1
        if _path_max_drawdown(path) <= observed_dd:  # 更深的回撤 (更负)
            hits_dd += 1
    return {
        "status": "ok",
        "p_value_sharpe": hits_sharpe / n_permutations,
        "p_value_max_drawdown": hits_dd / n_permutations,
        "observed_sharpe": observed_sharpe,
        "observed_max_drawdown": observed_dd,
        "n_trades": int(values.size),
    }


def bootstrap_sharpe_ci(
    equity: Sequence[float],
    *,
    n_bootstrap: int = 2000,
    confidence: float = 0.95,
    seed: int = 42,
) -> dict:
    """净值一阶差分日收益重抽样, 返回 Sharpe 置信区间与正收益概率。"""
    values = np.asarray(equity, dtype=np.float64)
    result = {
        "status": "insufficient",
        "ci_low": None,
        "ci_high": None,
        "prob_positive": None,
        "confidence": confidence,
    }
    if values.size < MIN_EQUITY_POINTS:
        return result
    returns = np.diff(values) / values[:-1]
    returns = returns[np.isfinite(returns)]
    if returns.size < MIN_EQUITY_POINTS - 1:
        return result

    rng = np.random.default_rng(seed)
    samples = rng.choice(returns, size=(n_bootstrap, returns.size), replace=True)
    stds = samples.std(axis=1)
    sharpes = np.where(stds > 0, samples.mean(axis=1) / stds, 0.0)
    alpha = (1.0 - confidence) / 2.0
    low, high = np.quantile(sharpes, [alpha, 1.0 - alpha])
    return {
        "status": "ok",
        "ci_low": float(low),
        "ci_high": float(high),
        "prob_positive": float((sharpes > 0).mean()),
        "confidence": confidence,
    }
