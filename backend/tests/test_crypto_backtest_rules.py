"""crypto 回测撮合参数化测试 (trading_rules → MatcherConfig)。

覆盖: 市场规则派生、任意精度数量、limit-lock 短路 (无涨跌停市场不再被 A 股
板块幅度规则误标封板)、T+0 当日风险退出 (对照 T+1 拒绝)。撮合口径红线
(CONTRIBUTING §5.3): 各市场入场撮合统一 open_t+1, 测试据此构造信号日。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import _limit_lock_matrices
from app.backtest.strategy import _market_matcher_overrides
from app.services.trading_rules import is_feature_enabled

# ================================================================
# 市场规则派生
# ================================================================

def test_market_matcher_overrides_by_asset_type():
    assert _market_matcher_overrides("crypto") == {
        "t_plus_1": False, "lot_size": 0.0, "price_limit": False,
    }
    assert _market_matcher_overrides("stock") == {
        "t_plus_1": True, "lot_size": 100.0, "price_limit": True,
    }
    # 未知类型 fail-closed 按 A 股保守默认
    assert _market_matcher_overrides("unknown")["t_plus_1"] is True


def test_crypto_backtest_feature_enabled():
    """回测守卫闸门: crypto 已开放 (deny_reason 无原因)。"""
    assert is_feature_enabled("crypto", "backtest") is True


# ================================================================
# 任意精度数量 (lot_size=0)
# ================================================================

def test_lot_round_shares_lot_size_100_floors_to_lots():
    from app.backtest.engine import _lot_round_shares
    # 10_000 预算 / 价格 95 (无成本) = 105.26 → 整手 100
    assert _lot_round_shares(10_000.0, 95.0, 0.0, 100.0) == pytest.approx(100.0)
    # 不足一手 → 0 (buy_lot_size 跳过)
    assert _lot_round_shares(5_000.0, 95.0, 0.0, 100.0) == 0.0


def test_lot_round_shares_lot_size_0_full_precision():
    from app.backtest.engine import _lot_round_shares
    # crypto: 按预算全额成交, 浮点数量 (0.0131 BTC 等)
    shares = _lot_round_shares(1_000.0, 76_000.0, 0.0, 0.0)
    assert shares == pytest.approx(1_000.0 / 76_000.0)
    assert shares > 0


def test_lot_round_shares_invalid_price_returns_zero():
    from app.backtest.engine import _lot_round_shares
    assert _lot_round_shares(1_000.0, 0.0, 0.0, 0.0) == 0.0
    assert _lot_round_shares(1_000.0, float("nan"), 0.0, 0.0) == 0.0


# ================================================================
# limit-lock 短路 (无涨跌停市场)
# ================================================================

def _limit_fixture(shape=(4, 2)):
    close = np.full(shape, 100.0)
    raw_close = np.full(shape, 100.0)
    seen = np.ones(shape, dtype=bool)
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(shape[0])]
    # A 股主板符号: 若不短路, 10% 板幅规则可能把 ±10% 波动日误标封板
    symbols = ["600000.SH", "BTCUSDT"]
    names = ["浦发银行", "BTC"]
    return close, raw_close, seen, dates, symbols, names


def test_limit_lock_matrices_disabled_returns_all_zero():
    close, raw_close, seen, dates, symbols, names = _limit_fixture()
    up, down = _limit_lock_matrices(
        close, raw_close, seen, dates, symbols, names, {}, enable=False,
    )
    assert not up.any()
    assert not down.any()


def test_limit_lock_matrices_enabled_computes_normally():
    """enable=True 时保持原行为: 大幅跳空可被标封板 (回归保护)。"""
    close, raw_close, seen, dates, symbols, names = _limit_fixture()
    # 次日对 600000.SH 跳空 +11% (超主板 10% 板幅) → 涨停锁可检出
    close[1, 0] = 111.0
    raw_close[1, 0] = 111.0
    up, _down = _limit_lock_matrices(
        close, raw_close, seen, dates, symbols, names, {},
        apply_latest_limits=False, enable=True,
    )
    assert up[1, 0] == 1


# ================================================================
# T+0 同日卖后再买 (对照 T+1 拒绝)
# ================================================================

def _row(symbol: str, day: int, price: float, **overrides) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "date": date(2024, 1, 1) + timedelta(days=day),
        "open": overrides.get("open", price),
        "high": overrides.get("high", price),
        "low": overrides.get("low", price),
        "close": overrides.get("close", price),
        "volume": overrides.get("volume", 1_000_000),
        "score": overrides.get("score", 0.0),
        "signal_limit_up": False,
        "signal_limit_down": False,
        "signal_entry": overrides.get("signal_entry", False),
        "signal_exit": overrides.get("signal_exit", False),
    }


def _reentry_panel() -> tuple[pl.DataFrame, pl.Series, pl.Series]:
    """day0 买入 (open_t+1 → day1 开盘成交), day1 同时出信号离场 + 再入场。

    T+1: day1 卖出后当日不可再买 (buy_same_day_reentry);
    T+0: day1 卖出后当日可再买 (入场仍 open_t+1 → day2 开盘成交, 防未来函数)。
    """
    rows = [
        _row("A", 0, 100.0, signal_entry=True),
        _row("A", 1, 101.0, open=100.0, high=102.0, low=99.0, signal_exit=True, signal_entry=True),
        _row("A", 2, 103.0, open=101.0, high=104.0, low=100.0),
        _row("A", 3, 104.0, open=103.0, high=105.0, low=102.0),
    ]
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    return panel, panel["signal_entry"], panel["signal_exit"]


def test_t1_blocks_same_day_reentry():
    panel, entries, exits = _reentry_panel()
    config = MatcherConfig(
        matching="open_t+1", t_plus_1=True,
        fees_pct=0, slippage_bps=0, initial_capital=2_000_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    result = engine.simulate_portfolio(panel, entries, exits, config)
    assert len(result.trades) == 1  # 再买入被拒, 全程仅一笔
    assert result.stats["execution"].get("buy_same_day_reentry", 0) == 1


def test_t0_allows_same_day_reentry():
    panel, entries, exits = _reentry_panel()
    config = MatcherConfig(
        matching="open_t+1", t_plus_1=False,
        fees_pct=0, slippage_bps=0, initial_capital=2_000_000,
    )
    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    result = engine.simulate_portfolio(panel, entries, exits, config)
    assert len(result.trades) == 2  # 卖出后当日再买 (第二笔 open_t+1 次日开盘成交, 防未来函数)
    assert result.trades[0].exit_date == result.trades[1].entry_date  # 同日卖出再入场


def test_limit_lock_short_circuit_via_loader_flag():
    """enable=False 时 A 股符号也零填充 (参数短路优先于符号规则)。"""
    close, raw_close, seen, dates, symbols, names = _limit_fixture()
    close[1, 0] = 111.0  # 无短路时会标涨停锁
    up, down = _limit_lock_matrices(
        close, raw_close, seen, dates, symbols, names, {},
        apply_latest_limits=False, enable=False,
    )
    assert not up.any() and not down.any()
