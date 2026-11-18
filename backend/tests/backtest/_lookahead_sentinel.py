"""Lookahead 哨兵: 毒化未来数据, 断言截断点前信号逐位不变。

借镜 HKUDS/Vibe-Trading test_lookahead 的方法 (MIT): 固定种子随机游走 panel
→ compute_signals 基线 → 从截断点后一行起逐列交替毒化 NaN/1e10 → 重算 →
断言 [0, cut] 行的 entry/exit/score/信号 id 与基线一致。

- 截断点前任何输出变化 = 前视泄漏 (AssertionError, 附样例)。
- 毒化导致计算异常 = 输入敏感性而非泄漏 (pytest.skip, 显式标注)。
- 后段输出不比较: 毒化本就为改变它。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix

_POISON_START = date(2024, 1, 2)


def _random_walk_panel(*, n_bars: int, n_symbols: int, seed: int) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    walk = 10.0 + np.cumsum(rng.normal(0.0, 0.3, size=(n_bars, n_symbols)), axis=0)
    walk = walk - walk.min(axis=0, keepdims=True) + 5.0  # 保证全为正价
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    rows = []
    for offset in range(n_bars):
        day = _POISON_START + timedelta(days=offset)
        for j, symbol in enumerate(symbols):
            close = float(walk[offset, j])
            rows.append({
                "symbol": symbol, "date": day,
                "open": close, "high": close, "low": close, "close": close,
                "volume": 1000.0,
            })
    return pl.DataFrame(rows)


def _poisoned_matrix(market, cut: int):
    """截断点后一行起, 偶数列置 NaN、奇数列置 1e10 (浮点列)。"""

    def change(array: np.ndarray) -> np.ndarray:
        if array.ndim != 2 or array.dtype.kind != "f":
            return array
        values = array.copy()
        rows = np.arange(values.shape[0]) > cut
        poison = np.empty_like(values)
        poison[:] = np.where(
            np.arange(values.shape[1]) % 2 == 0, np.nan, 1e10
        )
        values[rows] = poison[rows]
        values.flags.writeable = False
        return values

    return replace(
        market,
        **{name: change(getattr(market, name))
           for name in ("open", "high", "low", "close", "volume")},
        fields={key: change(value) for key, value in market.fields.items()},
        _valid_bars=None,
    )


def assert_lookahead_free(
    strategy,
    *,
    params: dict,
    warmup_bars: int,
    n_bars: int = 64,
    n_symbols: int = 3,
    seed: int = 7,
) -> None:
    """种子在随机游走 panel 上不得使用截断点之后的数据。"""
    market = build_market_data_matrix(
        _random_walk_panel(n_bars=n_bars, n_symbols=n_symbols, seed=seed)
    )
    cut = max(warmup_bars + 2, n_bars // 2)
    if cut >= n_bars - 1:
        raise ValueError(f"warmup_bars={warmup_bars} 相对 n_bars={n_bars} 过大, 无毒化空间")
    baseline = strategy.compute_signals(market, dict(params))
    try:
        actual = strategy.compute_signals(_poisoned_matrix(market, cut), dict(params))
    except Exception as exc:  # noqa: BLE001 — 毒化输入引发的失败必须与泄漏区分
        pytest.skip(f"毒化导致计算失败（输入敏感性, 非泄漏证据）: {type(exc).__name__}: {exc}")

    compared: list[tuple[str, np.ndarray, np.ndarray, bool]] = [
        ("entry", baseline.entry, actual.entry, False),
        ("exit", baseline.exit, actual.exit, False),
        ("score", baseline.score, actual.score, True),
        ("entry_signal_code", baseline.entry_signal_code, actual.entry_signal_code, False),
        ("exit_signal_code", baseline.exit_signal_code, actual.exit_signal_code, False),
    ]
    examples: list[str] = []
    for name, expected, observed, float_cmp in compared:
        head = slice(0, cut + 1)
        if float_cmp:
            diff = ~np.isclose(expected[head], observed[head], rtol=1e-6, atol=1e-7, equal_nan=True)
        else:
            diff = expected[head] != observed[head]
        for row, asset in np.argwhere(diff)[: max(0, 5 - len(examples))]:
            examples.append(
                f"{name}@{market.timestamp_labels[int(row)]}/{market.symbols[int(asset)]}: "
                f"baseline={expected[int(row), int(asset)]!r} "
                f"poisoned={observed[int(row), int(asset)]!r}"
            )
    if examples:
        raise AssertionError(
            "检测到前视泄漏: 毒化截断点之后的数据改变了历史信号\n  " + "\n  ".join(examples)
        )
