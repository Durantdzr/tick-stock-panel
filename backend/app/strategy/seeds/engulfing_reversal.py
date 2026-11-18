"""阳包阴反转 — 多头吞没形态入场, 空头吞没形态离场 (经典 K 线形态)"""

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
    "id": "engulfing_reversal",
    "name": "阳包阴反转",
    "description": "多头吞没 (当日阳线实体完全包住前日阴线实体) 且处于 MA20 下方时入场; 空头吞没离场",
    "tags": ["形态", "反转"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "require_below_ma",
            "label": "要求处于MA20下方",
            "type": "bool",
            "default": True,
        },
        {
            "id": "ma_window",
            "label": "过滤均线窗口",
            "type": "int",
            "default": 20,
            "min": 5,
            "max": 60,
            "step": 5,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "annual_vol_20d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_engulfing_reversal"]
EXIT_SIGNALS = ["signal_engulfing_reversal_exit"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class EngulfingReversalMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("ma_window", 20))
        return window + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        open_, close = market.open, market.close
        prev_open, prev_close = shift(open_, 1), shift(close, 1)
        # 多头吞没: 前日阴线, 当日阳线, 实体完全包住前日实体
        bullish_engulf = (
            (close > open_)
            & (prev_close < prev_open)
            & (open_ <= prev_close)
            & (close >= prev_open)
        )
        bearish_engulf = (
            (close < open_)
            & (prev_close > prev_open)
            & (open_ >= prev_close)
            & (close <= prev_open)
        )
        entry = bullish_engulf
        if params.get("require_below_ma", True):
            ma_window = int(params.get("ma_window", 20))
            entry = entry & (prev_close < matrix_feature(market, f"ma{ma_window}"))
        exit_ = bearish_engulf
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_engulfing_reversal",),
            exit_signal_ids=("signal_engulfing_reversal_exit",),
        )


MATRIX_STRATEGY = EngulfingReversalMatrixStrategy()
