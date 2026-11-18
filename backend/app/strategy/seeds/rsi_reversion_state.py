"""RSI均值回归状态机 — RSI跌破超卖阈值入场, 上破退出阈值离场 (参考 QuantJourney W03)"""

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
    "id": "rsi_reversion_state",
    "name": "RSI超卖回归",
    "description": "RSI自上而下跌破超卖阈值当日入场, 上破退出阈值当日离场",
    "tags": ["RSI", "均值回归"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "oversold",
            "label": "超卖入场阈值",
            "type": "float",
            "default": 35.0,
            "min": 10.0,
            "max": 50.0,
            "step": 1.0,
        },
        {
            "id": "exit_level",
            "label": "退出阈值",
            "type": "float",
            "default": 60.0,
            "min": 50.0,
            "max": 90.0,
            "step": 1.0,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "annual_vol_20d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_rsi_reversion_state"]
EXIT_SIGNALS = ["signal_rsi_reversion_state_exit"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 20


class RsiReversionStateMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        del params
        return 20

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        oversold = float(params.get("oversold", 35.0))
        exit_level = float(params.get("exit_level", 60.0))
        rsi = matrix_feature(market, "rsi_14")
        prev_rsi = shift(rsi, 1)
        # 穿越事件形式的状态机等价: 状态栏 (RSI<oversold) 与事件栏 (上根 >= oversold) 分离
        entry = (rsi < oversold) & (prev_rsi >= oversold)
        exit_ = (rsi > exit_level) & (prev_rsi <= exit_level)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_rsi_reversion_state",),
            exit_signal_ids=("signal_rsi_reversion_state_exit",),
        )


MATRIX_STRATEGY = RsiReversionStateMatrixStrategy()
