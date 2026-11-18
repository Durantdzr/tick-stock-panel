"""布林带均值回归 — 收盘跌破布林下轨入场, 回升穿越中轨退出 (参考 QuantJourney W21)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    rolling_mean,
    rolling_std,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "boll_reversion",
    "name": "布林带均值回归",
    "description": "收盘自上而下跌破布林下轨当日入场, 收盘上穿中轨当日离场",
    "tags": ["布林带", "均值回归"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "window",
            "label": "统计窗口",
            "type": "int",
            "default": 20,
            "min": 10,
            "max": 60,
            "step": 5,
        },
        {
            "id": "n_std",
            "label": "下轨标准差倍数",
            "type": "float",
            "default": 2.0,
            "min": 1.0,
            "max": 3.5,
            "step": 0.5,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "annual_vol_20d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_boll_reversion"]
EXIT_SIGNALS = ["signal_boll_reversion_exit"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20


class BollReversionMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("window", 20))
        return window + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("window", 20))
        n_std = float(params.get("n_std", 2.0))
        close = market.close
        mid = rolling_mean(close, window)
        lower = mid - n_std * rolling_std(close, window)
        prev_close = shift(close, 1)
        entry = (close < lower) & (prev_close >= shift(lower, 1))
        exit_ = (close > mid) & (prev_close <= shift(mid, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_boll_reversion",),
            exit_signal_ids=("signal_boll_reversion_exit",),
        )


MATRIX_STRATEGY = BollReversionMatrixStrategy()
