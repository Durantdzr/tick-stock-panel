"""绝对动量过滤 — lookback日累计收益由负转正入场, 转负退出 (参考 QuantJourney W04 单标的版)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "abs_momentum",
    "name": "绝对动量过滤",
    "description": "lookback日累计收益自下而上穿越0当日入场, 自上而下穿越0当日离场",
    "tags": ["动量", "趋势"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "lookback",
            "label": "动量回看窗口",
            "type": "int",
            "default": 250,
            "min": 60,
            "max": 500,
            "step": 10,
        },
    ],
    "scoring": {"momentum_60d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_abs_momentum"]
EXIT_SIGNALS = ["signal_abs_momentum_exit"]
STOP_LOSS = -0.10
MAX_HOLD_DAYS = 60


class AbsMomentumMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        lookback = int(params.get("lookback", 250))
        return lookback + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        lookback = int(params.get("lookback", 250))
        base = shift(market.close, lookback)
        momentum = market.close / base - 1.0
        prev = shift(momentum, 1)
        entry = (momentum >= 0.0) & (prev < 0.0)
        exit_ = (momentum < 0.0) & (prev >= 0.0)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_abs_momentum",),
            exit_signal_ids=("signal_abs_momentum_exit",),
        )


MATRIX_STRATEGY = AbsMomentumMatrixStrategy()
