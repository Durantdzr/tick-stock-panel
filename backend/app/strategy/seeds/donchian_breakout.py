"""Donchian通道突破 — 收盘突破不含当日的前N日最高价入场, 跌破前M日最低价退出 (参考 QuantJourney O20)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    rolling_max,
    rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "donchian_breakout",
    "name": "Donchian通道突破",
    "description": "收盘突破不含当日的前N日最高价当日入场, 收盘跌破不含当日的前M日最低价当日离场",
    "tags": ["突破", "通道"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "entry_window",
            "label": "入场通道窗口",
            "type": "int",
            "default": 55,
            "min": 10,
            "max": 120,
            "step": 5,
        },
        {
            "id": "exit_window",
            "label": "退出通道窗口",
            "type": "int",
            "default": 20,
            "min": 5,
            "max": 60,
            "step": 5,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "vol_ratio_5d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_donchian_breakout"]
EXIT_SIGNALS = ["signal_donchian_breakout_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 40


class DonchianBreakoutMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        entry_window = int(params.get("entry_window", 55))
        return entry_window + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        entry_window = int(params.get("entry_window", 55))
        exit_window = int(params.get("exit_window", 20))
        # shift 排除当日: 与前 N 日区间比较, 避免把当日高点计入突破基准
        prior_high = shift(rolling_max(market.high, entry_window), 1)
        prior_low = shift(rolling_min(market.low, exit_window), 1)
        entry = market.close > prior_high
        exit_ = market.close < prior_low
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_donchian_breakout",),
            exit_signal_ids=("signal_donchian_breakout_exit",),
        )


MATRIX_STRATEGY = DonchianBreakoutMatrixStrategy()
