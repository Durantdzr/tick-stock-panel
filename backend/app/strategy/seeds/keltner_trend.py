"""Keltner通道趋势 — EMA ± k×ATR 通道突破, 上穿上轨入场, 下穿下轨退出 (参考 vnpy KingKeltner)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_rolling_mean,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "keltner_trend",
    "name": "Keltner通道趋势",
    "description": "收盘上穿 EMA+k×ATR 上轨当日入场, 下穿 EMA−k×ATR 下轨当日离场",
    "tags": ["通道", "趋势", "ATR"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "window",
            "label": "通道窗口",
            "type": "int",
            "default": 40,
            "min": 10,
            "max": 120,
            "step": 5,
        },
        {
            "id": "atr_multiplier",
            "label": "ATR倍数",
            "type": "float",
            "default": 2.0,
            "min": 0.5,
            "max": 5.0,
            "step": 0.25,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_keltner_trend"]
EXIT_SIGNALS = ["signal_keltner_trend_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 40


class KeltnerTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("window", 40))
        return window + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("window", 40))
        k = float(params.get("atr_multiplier", 2.0))
        close = market.close
        close_valid = np.isfinite(close)
        ema = matrix_feature(market, f"ema{window}")
        atr = self._atr(market, window, close_valid)
        upper = ema + k * atr
        lower = ema - k * atr
        entry = (close > upper) & (shift(close, 1) <= shift(upper, 1))
        exit_ = (close < lower) & (shift(close, 1) >= shift(lower, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_keltner_trend",),
            exit_signal_ids=("signal_keltner_trend_exit",),
        )

    @staticmethod
    def _atr(market: MarketDataMatrix, window: int, close_valid: np.ndarray) -> np.ndarray:
        prev_close = shift(market.close, 1)
        tr = market.high - market.low
        with_pc = np.isfinite(prev_close)
        tr = np.where(with_pc, np.maximum.reduce([
            tr,
            np.abs(market.high - prev_close),
            np.abs(market.low - prev_close),
        ]), tr)
        return valid_rolling_mean(tr, close_valid, window)


MATRIX_STRATEGY = KeltnerTrendMatrixStrategy()
