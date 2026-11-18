"""QuantJourney 移植的策略种子行为回归 — 固定样本数值断言 + 无前视 + NaN 保护。

覆盖 5 个 matrix_native 种子:
sma_long_trend / rsi_reversion_state / boll_reversion / donchian_breakout / abs_momentum
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from tests.backtest._lookahead_sentinel import assert_lookahead_free
from app.strategy.seeds.abs_momentum import MATRIX_STRATEGY as ABS_MOMENTUM
from app.strategy.seeds.boll_reversion import MATRIX_STRATEGY as BOLL_REVERSION
from app.strategy.seeds.donchian_breakout import MATRIX_STRATEGY as DONCHIAN
from app.strategy.seeds.rsi_reversion_state import MATRIX_STRATEGY as RSI_REVERSION
from app.strategy.seeds.sma_long_trend import MATRIX_STRATEGY as SMA_LONG_TREND

START = date(2024, 1, 2)


def _build_panel(paths: dict[str, list[float]]) -> pl.DataFrame:
    """按 symbol -> close 序列构造最小日线 panel (open=close, high=low=close)。"""
    n = len(next(iter(paths.values())))
    rows = []
    for offset in range(n):
        day = START + timedelta(days=offset)
        for symbol, closes in paths.items():
            close = closes[offset]
            rows.append({
                "symbol": symbol,
                "date": day,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1000.0 + (offset % 7) * 50.0,
            })
    return pl.DataFrame(rows)


def _build_panel_hlc(
    paths: dict[str, list[tuple[float, float, float]]],
) -> pl.DataFrame:
    """按 symbol -> (close, high, low) 序列构造日线 panel。"""
    n = len(next(iter(paths.values())))
    rows = []
    for offset in range(n):
        day = START + timedelta(days=offset)
        for symbol, series in paths.items():
            close, high, low = series[offset]
            rows.append({
                "symbol": symbol,
                "date": day,
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000.0,
            })
    return pl.DataFrame(rows)


def _col(signals, column: str, asset: int = 0) -> np.ndarray:
    return getattr(signals, column)[:, asset].astype(bool)


def _assert_event_only(mask: np.ndarray) -> None:
    assert not (mask[:-1] & mask[1:]).any()


def test_sma_long_trend_dead_then_golden_cross():
    # 涨 -> 跌 (死叉退出) -> 再涨 (金叉入场); 慢线 200 预热完成后交叉事件才有效,
    # 且在预热完成的首根快线必须仍在慢线上方 (否则交叉被预热吞掉, 永远不触发)
    closes_a = (
        [10.0 + i * 0.05 for i in range(150)]
        + [17.5 - (i + 1) * 0.05 for i in range(100)]
        + [12.5 + (i + 1) * 0.1 for i in range(80)]
    )
    closes_b = [10.0] * 330
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": closes_b}))
    signals = SMA_LONG_TREND.compute_signals(market, {})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 慢线预热期 (前 200 根) 内不允许出现任何信号 — 无前视
    assert not entry[:200].any()
    assert not exit_[:200].any()
    # 单边涨 -> 单边跌 -> 单边涨: 每种交叉事件各恰一次
    assert entry.sum() == 1
    assert exit_.sum() == 1
    _assert_event_only(entry)
    _assert_event_only(exit_)
    # 退出发生在回调段, 入场发生在恢复段
    assert 200 <= int(np.argmax(exit_)) < 250 <= int(np.argmax(entry))
    # 横盘标的不触发 (均线相等, 严格比较为 False)
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_sma_long_trend_nan_close_never_signals_and_recovers():
    # 上涨 -> 回调把快线压到慢线下方 -> 恢复产生金叉; 停牌段插入在预热区
    closes = [10.0 + i * 0.02 for i in range(45)]
    closes += [closes[-1] - (i + 1) * 0.15 for i in range(6)]
    closes += [closes[-1] + (i + 1) * 0.15 for i in range(12)]
    closes[15:20] = [np.nan] * 5  # 模拟停牌
    market = build_market_data_matrix(_build_panel({"AAA": closes}))
    signals = SMA_LONG_TREND.compute_signals(market, {"fast_window": 5, "slow_window": 20})

    entry = _col(signals, "entry")
    assert not entry[15:20].any()
    assert not entry[:20].any()
    assert entry[40:].any()  # 复牌后 valid_shift 跳过缺失 bar, 恢复段金叉仍能触发


def test_rsi_reversion_state_is_event_only_with_recovery_exit():
    # 平盘段 RSI 恒为 0 (gain/loss 均为 0), 必须先上涨抬高 RSI, 再下跌触发下穿入场
    closes_a = (
        [10.0] * 20
        + [10.0 + (i - 19) * 0.15 for i in range(20, 40)]
        + [13.0 - (i - 39) * 0.15 for i in range(40, 70)]
        + [8.5 + (i - 69) * 0.12 for i in range(70, 90)]
    )
    closes_b = [10.0] * 90
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": closes_b}))
    signals = RSI_REVERSION.compute_signals(market, {})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    assert entry.sum() == 1  # 下跌段 RSI 下穿 35 恰一次
    _assert_event_only(entry)
    _assert_event_only(exit_)
    first_entry = int(np.argmax(entry))
    # RSI(14) 预热期内不触发
    assert first_entry >= 21
    # 恢复段存在上穿 60 的退出事件
    assert exit_[first_entry:].any()
    # 横盘标的 RSI 恒为 0, 不满足上根 >= 35, 不触发
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_boll_reversion_entry_on_lower_band_break_and_exit_at_mid():
    closes_a = (
        [10.0] * 25
        + [9.0] * 11
        + [10.5] * 10
    )
    closes_b = [10.0] * 46
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": closes_b}))
    signals = BOLL_REVERSION.compute_signals(market, {})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 跳水当日触发一次; 跌势持续期间 prev_close 已在轨下, 不重复触发
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([25]))
    # 回升穿越中轨当日退出一次
    np.testing.assert_array_equal(np.flatnonzero(exit_), np.array([36]))
    assert not _col(signals, "entry", asset=1).any()


def test_donchian_breakout_uses_prior_window_without_today():
    closes_a = [10.0] * 60 + [10.6] * 10 + [9.2] * 10
    highs_a = [10.0] * 60 + [11.0] * 10 + [9.6] * 10
    lows_a = [10.0] * 60 + [10.4] * 10 + [9.0] * 10
    paths = {
        "AAA": list(zip(closes_a, highs_a, lows_a, strict=True)),
        "BBB": [(10.0, 10.0, 10.0)] * len(closes_a),
    }
    market = build_market_data_matrix(_build_panel_hlc(paths))
    signals = DONCHIAN.compute_signals(market, {})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 突破当日: 收盘 10.6 > 前 55 日最高 10.0 (不含当日, 当日高点 11 不计入)
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([60]))
    # 之后 prior_high 已包含突破日高点 11, 不重复入场
    # 跌破前 20 日最低价当日退出; 之后 prior_low 已包含崩塌日低点 9.0, 不重复退出
    np.testing.assert_array_equal(np.flatnonzero(exit_), np.array([70]))
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_abs_momentum_crosses_zero_once_matches_recomputation():
    # 下跌 0.05/日 -> 上涨 0.16/日, 0 值穿越点落在两 bar 之间, 避开 float32 相等边界
    closes_a = [12.0 - (i + 1) * 0.05 for i in range(60)] + [9.0 + (i + 1) * 0.16 for i in range(40)]
    closes_b = [10.0] * 100
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": closes_b}))
    signals = ABS_MOMENTUM.compute_signals(market, {"lookback": 60})

    closes_np = np.array(closes_a, dtype=np.float32)
    base = _shift_np(closes_np, 60)
    momentum = closes_np / base - 1.0
    prev = _shift_np(momentum)
    expected_entry = (momentum >= 0.0) & (prev < 0.0)
    expected_exit = (momentum < 0.0) & (prev >= 0.0)

    np.testing.assert_array_equal(_col(signals, "entry"), expected_entry)
    np.testing.assert_array_equal(_col(signals, "exit"), expected_exit)
    # 预热期内无信号
    assert not _col(signals, "entry")[:60].any()
    assert not _col(signals, "exit")[:60].any()
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def _shift_np(values: np.ndarray, periods: int = 1) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float32)
    out[periods:] = values[:-periods]
    return out


# ================================================================
# Lookahead 哨兵: 毒化未来数据, 截断点前信号必须不变
# ================================================================
@pytest.mark.parametrize("strategy,params,warmup,n_bars", [
    (SMA_LONG_TREND, {"fast_window": 5, "slow_window": 20}, 20, 64),
    (RSI_REVERSION, {}, 21, 64),
    (BOLL_REVERSION, {}, 25, 64),
    (DONCHIAN, {}, 56, 64),
    (ABS_MOMENTUM, {"lookback": 60}, 60, 80),
])
def test_seed_lookahead_sentinel(strategy, params, warmup, n_bars):
    assert_lookahead_free(strategy, params=params, warmup_bars=warmup, n_bars=n_bars)
