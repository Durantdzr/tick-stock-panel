"""Double 7s — 长趋势上方创7日新低买入, 创7日新高离场 (参考 Connors Double 7s 研究)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    rolling_max,
    rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "double_seven",
    "name": "Double 7s短线回归",
    "description": "收盘在 MA200 上方且低于前7日最低收盘时入场 (水平条件), 高于前7日最高收盘时离场",
    "tags": ["均值回归", "短线"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "window",
            "label": "极值窗口",
            "type": "int",
            "default": 7,
            "min": 3,
            "max": 20,
            "step": 1,
        },
        {
            "id": "trend_window",
            "label": "长趋势均线窗口",
            "type": "int",
            "default": 200,
            "min": 50,
            "max": 500,
            "step": 10,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "annual_vol_20d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_double_seven"]
EXIT_SIGNALS = ["signal_double_seven_exit"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class DoubleSevenMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        trend = int(params.get("trend_window", 200))
        return trend + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("window", 7))
        trend_window = int(params.get("trend_window", 200))
        # 极值取前一交易日及以前, 排除当日自身
        prior_low = shift(rolling_min(market.close, window), 1)
        prior_high = shift(rolling_max(market.close, window), 1)
        ma_trend = matrix_feature(market, f"ma{trend_window}")
        entry = (market.close < prior_low) & (market.close > ma_trend)
        exit_ = market.close > prior_high
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_double_seven",),
            exit_signal_ids=("signal_double_seven_exit",),
        )


MATRIX_STRATEGY = DoubleSevenMatrixStrategy()
