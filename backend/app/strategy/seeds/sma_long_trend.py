"""长趋势SMA — 快慢长周期均线金叉入场, 死叉退出 (参考 QuantJourney W01/W06)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "sma_long_trend",
    "name": "长趋势SMA金叉",
    "description": "SMA快线上穿SMA慢线当日触发, 长周期趋势确认; 慢线下穿退出",
    "tags": ["均线", "趋势"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "fast_window",
            "label": "快线窗口",
            "type": "int",
            "default": 50,
            "min": 5,
            "max": 120,
            "step": 5,
        },
        {
            "id": "slow_window",
            "label": "慢线窗口",
            "type": "int",
            "default": 200,
            "min": 20,
            "max": 500,
            "step": 10,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_sma_long_trend"]
EXIT_SIGNALS = ["signal_sma_long_trend_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 60


class SmaLongTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        slow = int(params.get("slow_window", 200))
        fast = int(params.get("fast_window", 50))
        return max(slow, fast) + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        fast = int(params.get("fast_window", 50))
        slow = int(params.get("slow_window", 200))
        ma_fast = matrix_feature(market, f"ma{fast}")
        ma_slow = matrix_feature(market, f"ma{slow}")
        golden = (ma_fast > ma_slow) & (shift(ma_fast, 1) <= shift(ma_slow, 1))
        dead = (ma_fast < ma_slow) & (shift(ma_fast, 1) >= shift(ma_slow, 1))
        entry = golden
        exit_ = dead
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_sma_long_trend",),
            exit_signal_ids=("signal_sma_long_trend_exit",),
        )


MATRIX_STRATEGY = SmaLongTrendMatrixStrategy()
