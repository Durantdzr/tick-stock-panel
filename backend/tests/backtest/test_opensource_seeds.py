"""第二批开源策略种子行为回归 — 固定样本数值断言 + 无前视 + 退化输入保护。

覆盖 8 个 matrix_native 种子:
keltner_trend / supertrend / dual_thrust / rsi2_reversion / double_seven /
ichimoku_trend / engulfing_reversal / adx_trend
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from tests.backtest._lookahead_sentinel import assert_lookahead_free
from app.strategy.seeds.adx_trend import MATRIX_STRATEGY as ADX_TREND
from app.strategy.seeds.double_seven import MATRIX_STRATEGY as DOUBLE_SEVEN
from app.strategy.seeds.dual_thrust import MATRIX_STRATEGY as DUAL_THRUST
from app.strategy.seeds.engulfing_reversal import MATRIX_STRATEGY as ENGULFING
from app.strategy.seeds.ichimoku_trend import MATRIX_STRATEGY as ICHIMOKU
from app.strategy.seeds.keltner_trend import MATRIX_STRATEGY as KELTNER
from app.strategy.seeds.rsi2_reversion import MATRIX_STRATEGY as RSI2
from app.strategy.seeds.supertrend import MATRIX_STRATEGY as SUPERTREND

START = date(2024, 1, 2)


def _build_panel(paths: dict[str, list[float]]) -> pl.DataFrame:
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
                "volume": 1000.0,
            })
    return pl.DataFrame(rows)


def _build_panel_ohlc(paths: dict[str, list[tuple[float, float, float, float]]]) -> pl.DataFrame:
    """按 symbol -> (open, high, low, close) 序列构造日线 panel。"""
    n = len(next(iter(paths.values())))
    rows = []
    for offset in range(n):
        day = START + timedelta(days=offset)
        for symbol, series in paths.items():
            open_, high, low, close = series[offset]
            rows.append({
                "symbol": symbol,
                "date": day,
                "open": open_,
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


def _roll(values: np.ndarray, window: int, func) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float32)
    for i in range(window - 1, len(values)):
        out[i] = func(values[i - window + 1 : i + 1])
    return out


def _shift(values: np.ndarray, periods: int = 1) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float32)
    out[periods:] = values[:-periods]
    return out


def _ewm(values: np.ndarray, alpha: float) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=np.float32)
    seeded = False
    for i in range(len(values)):
        if not np.isfinite(values[i]):
            continue
        out[i] = values[i] if not seeded else out[i - 1] + alpha * (values[i] - out[i - 1])
        seeded = True
    return out


def test_keltner_trend_breakout_entry_and_crash_exit():
    # 锯齿震荡 (不触轨) -> 加速上涨上穿上轨 -> 单边急跌下穿下轨
    closes_a = [10.0 if i % 2 == 0 else 10.3 for i in range(30)]
    closes_a += [10.9 + (i - 30) * 0.6 for i in range(30, 40)]
    closes_a += [closes_a[39] - (i - 39) * 0.8 for i in range(40, 52)]
    market = build_market_data_matrix(
        _build_panel({"AAA": closes_a, "BBB": [10.0] * 52})
    )
    signals = KELTNER.compute_signals(market, {"window": 20, "atr_multiplier": 2.0})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 预热期 (ema20 + atr20) 内无信号
    assert not entry[:20].any()
    assert not exit_[:20].any()
    # 上穿上轨恰发生在加速段首根: 震荡期 close<=10.3 < 上轨, 首根拉升 10.9
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([30]))
    _assert_event_only(exit_)
    assert 40 <= int(np.argmax(exit_)) < 50
    # 横盘标的: TR≈0, 上下轨与均线重合, 严格比较不触发
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_supertrend_flip_down_on_crash_and_flip_up_on_recovery():
    # 上涨(方向未定) -> 崩跌跌破冻结下带(翻空) -> 修复收复上带(翻多)
    closes_a = [10.0 + i * 0.3 for i in range(15)]
    closes_a += [closes_a[14] - (i - 15 + 1) * 0.6 for i in range(15, 25)]
    closes_a += [closes_a[24] + (i - 25 + 1) * 0.3 for i in range(25, 50)]
    paths = {
        "AAA": [(c, c, c - 0.4, c) for c in closes_a],
        "BBB": [(10.0, 10.0, 10.0, 10.0)] * 50,
    }
    market = build_market_data_matrix(_build_panel_ohlc(paths))
    signals = SUPERTREND.compute_signals(market, {"atr_window": 5, "multiplier": 2.0})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    assert exit_.sum() == 1
    assert entry.sum() == 2  # 初段上带冻结在价格下方, 首次突破翻多 + 修复段二次翻多
    # ATR=0.4 (振幅), 下带冻结在顶部下方 2×ATR, 崩跌第三根 K 线跌破
    assert int(np.argmax(exit_)) == 16
    assert int(np.argmax(entry)) == 7  # 预热完成 (ATR 窗口 5) 后首次收复冻结上带
    assert int(np.flatnonzero(entry)[1]) > 25  # 修复段翻多 (需收复崩跌前的上带)
    _assert_event_only(entry)
    _assert_event_only(exit_)
    # 横盘标的 ATR=0, 通道带与价格重合, 严格比较不触发翻转
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_dual_thrust_matches_independent_recomputation():
    closes_a = [10.0 if i % 2 == 0 else 10.4 for i in range(20)]
    opens_a = [10.0, *closes_a[:-1]]
    # 突破 K 线: 高开高走远离区间; 随后崩跌 K 线击穿下轨
    opens_a += [10.4, 11.3]
    closes_a += [11.7, 9.1]
    highs_a = [max(o, c) + 0.1 for o, c in zip(opens_a[:20], closes_a[:20], strict=True)]
    lows_a = [min(o, c) - 0.1 for o, c in zip(opens_a[:20], closes_a[:20], strict=True)]
    highs_a += [11.9, 11.5]
    lows_a += [10.4, 8.9]
    paths = {
        "AAA": list(zip(opens_a, highs_a, lows_a, closes_a, strict=True)),
        "BBB": [(10.0, 10.0, 10.0, 10.0)] * 22,
    }
    market = build_market_data_matrix(_build_panel_ohlc(paths))
    signals = DUAL_THRUST.compute_signals(market, {"range_window": 4, "k_up": 0.5, "k_down": 0.5})

    opens_np = np.array(opens_a, dtype=np.float32)
    highs_np = np.array(highs_a, dtype=np.float32)
    lows_np = np.array(lows_a, dtype=np.float32)
    closes_np = np.array(closes_a, dtype=np.float32)
    hh = _shift(_roll(highs_np, 4, np.max))
    lc = _shift(_roll(closes_np, 4, np.min))
    hc = _shift(_roll(closes_np, 4, np.max))
    ll = _shift(_roll(lows_np, 4, np.min))
    rng = np.maximum(hh - lc, hc - ll)
    top = opens_np + 0.5 * rng
    bottom = opens_np - 0.5 * rng
    expected_entry = (closes_np > top) & (_shift(closes_np) <= _shift(top))
    expected_exit = (closes_np < bottom) & (_shift(closes_np) >= _shift(bottom))

    np.testing.assert_array_equal(_col(signals, "entry"), expected_entry)
    np.testing.assert_array_equal(_col(signals, "exit"), expected_exit)
    # 崩跌 K 线击穿下轨离场 (锯齿段 k1×区间 < 振幅, 反向 K 线本就触发 — 策略固有行为)
    assert _col(signals, "exit")[21]
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_rsi2_reversion_buys_extreme_oversold_in_uptrend():
    # 稳定上涨 (RSI=100, 不会入场) -> 两根急跌 (RSI2=0, 仍在 MA50 上方) -> 强反弹修复
    closes_a = [10.0 + i * 0.1 for i in range(70)]
    closes_a += [closes_a[69] - 1.0, closes_a[69] - 2.0]
    closes_a += [closes_a[71] + 1.5, closes_a[71] + 1.5 + 0.3]
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": [10.0] * 74}))
    signals = RSI2.compute_signals(market, {"trend_window": 50, "oversold": 10.0, "exit_level": 65.0})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 水平条件语义: 急跌两根均在超卖区且仍处 MA50 上方
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([70, 71]))
    # 预热期 (MA50) 内无入场
    assert not entry[:50].any()
    # 强反弹后 RSI2 修复越过退出阈值
    assert exit_[72:].any()
    # 横盘标的: close > MA200 恒 False, 不入场; RSI2=0 也不满足退出
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_double_seven_buys_new_low_in_uptrend_and_exits_on_new_high():
    # 回调幅度必须小于 MA50 的滞后距离 (否则水平条件里的长趋势过滤失败)
    closes_a = [10.0 + i * 0.1 for i in range(70)]
    closes_a += [closes_a[69] - (i - 69) * 0.65 for i in range(70, 73)]
    closes_a += [closes_a[72] + (i - 72) * 1.2 for i in range(73, 80)]
    market = build_market_data_matrix(_build_panel({"AAA": closes_a, "BBB": [10.0] * 80}))
    signals = DOUBLE_SEVEN.compute_signals(market, {"window": 7, "trend_window": 50})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 回调三根均创 7 日收盘新低且仍在 MA50 上方 (水平条件)
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([70, 71, 72]))
    assert not entry[:50].any()
    # 修复段创 7 日新高离场
    assert exit_[73:].any()
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_ichimoku_trend_entry_on_cross_above_cloud():
    # 阴跌 -> 强势拉升 (转换线上穿基准线且收盘站上云层) -> 末段跌破云层离场
    closes_a = [10.0 - (i + 1) * 0.05 for i in range(20)]
    closes_a += [closes_a[19] + (i - 19) * 0.8 for i in range(20, 40)]
    closes_a += [closes_a[39] - (i - 39) * 0.7 for i in range(40, 55)]
    paths = {
        "AAA": [(c, c + 0.2, c - 0.2, c) for c in closes_a],
        "BBB": [(10.0, 10.0, 10.0, 10.0)] * 55,
    }
    market = build_market_data_matrix(_build_panel_ohlc(paths))
    signals = ICHIMOKU.compute_signals(
        market,
        {"tenkan_window": 5, "kijun_window": 10, "senkou_window": 20, "displacement": 5},
    )

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # 云层 = 5 日前计算的先行带, 预热期为 senkou 20 + 位移 5 = 25 根 (index 24 首根可见)
    assert not entry[:24].any()
    assert not exit_[:24].any()
    assert entry.sum() == 1  # 拉升段单次金叉 + 站上云层
    assert 20 <= int(np.argmax(entry)) < 40
    # 末段跌破云底离场
    assert exit_[40:].any()
    # 横盘标的: 转换线=基准线, 云层=现价, 严格比较不触发
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_engulfing_reversal_exact_bars():
    # 连续阴跌 -> 精确构造多头吞没 K 线 -> 反弹 -> 空头吞没离场
    paths_a = []
    close = 11.0
    for _ in range(20):  # 阴跌段: 阳线不会出现, MA20 在上方
        o = close
        close = close - 0.5
        paths_a.append((o, o, close, close))
    # 多头吞没: 前日阴线 (open 1.5, close 1.0), 当日 open<=前收, close>=前开
    paths_a.append((0.9, 2.3, 0.9, 2.2))
    # 反弹两根
    paths_a.append((2.2, 2.6, 2.1, 2.5))
    paths_a.append((2.5, 2.8, 2.4, 2.7))
    # 空头吞没: 前日阳线 (open 2.5, close 2.7), 当日 open>=前收, close<=前开
    paths_a.append((2.8, 2.9, 1.9, 2.0))
    paths_a.append((2.0, 2.1, 1.8, 1.9))
    market = build_market_data_matrix(_build_panel_ohlc({
        "AAA": paths_a,
        "BBB": [(10.0, 10.0, 10.0, 10.0)] * 26,
    }))
    signals = ENGULFING.compute_signals(market, {"require_below_ma": True, "ma_window": 20})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    np.testing.assert_array_equal(np.flatnonzero(entry), np.array([20]))
    np.testing.assert_array_equal(np.flatnonzero(exit_), np.array([23]))
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


def test_adx_trend_entry_requires_strength_and_exit_on_di_flip():
    # 震荡 (ADX 低, 不入场) -> 强单边上涨 (ADX 上升, +DI>-DI, 水平条件入场) -> 反向下跌 (-DI>+DI 离场)
    closes_a = [10.0 if i % 2 == 0 else 10.15 for i in range(25)]
    closes_a += [closes_a[24] + (i - 24) * 0.5 for i in range(25, 45)]
    closes_a += [closes_a[44] - (i - 44) * 0.5 for i in range(45, 60)]
    paths = {
        "AAA": [(c, c + 0.3, c - 0.3, c) for c in closes_a],
        "BBB": [(10.0, 10.0, 10.0, 10.0)] * 60,
    }
    market = build_market_data_matrix(_build_panel_ohlc(paths))
    signals = ADX_TREND.compute_signals(market, {"window": 7, "adx_threshold": 25.0})

    entry = _col(signals, "entry")
    exit_ = _col(signals, "exit")
    # ADX 需要双轮 Wilder 平滑, 预热期内无入场
    assert not entry[:21].any()
    # 震荡段 ADX 未达阈值, 无入场
    assert not entry[:25].any()
    # 单边段满足 +DI>-DI 且 ADX>=25 (水平条件, 允许持续多根)
    assert entry[25:45].any()
    # 下跌段 −DI 反超, 离场
    assert exit_[45:].any()
    # 横盘标的: DM 全 0, DI 与 ADX 均为 0, 不触发 (除零保护)
    assert not _col(signals, "entry", asset=1).any()
    assert not _col(signals, "exit", asset=1).any()


# ================================================================
# Lookahead 哨兵: 毒化未来数据, 截断点前信号必须不变
# ================================================================
@pytest.mark.parametrize("strategy,params,warmup", [
    (KELTNER, {"window": 20, "atr_multiplier": 2.0}, 20),
    (SUPERTREND, {"atr_window": 5, "multiplier": 2.0}, 5),
    (DUAL_THRUST, {"range_window": 4, "k_up": 0.5, "k_down": 0.5}, 5),
    (RSI2, {"trend_window": 50, "oversold": 10.0, "exit_level": 65.0}, 50),
    (DOUBLE_SEVEN, {"window": 7, "trend_window": 50}, 50),
    (ICHIMOKU, {"tenkan_window": 5, "kijun_window": 10,
                "senkou_window": 20, "displacement": 5}, 24),
    (ENGULFING, {"require_below_ma": True, "ma_window": 20}, 20),
    (ADX_TREND, {"window": 7, "adx_threshold": 25.0}, 21),
])
def test_seed_lookahead_sentinel(strategy, params, warmup):
    assert_lookahead_free(strategy, params=params, warmup_bars=warmup)
