"""RSI(2)超卖回归 — 长趋势上方的极端超卖买入, RSI修复后离场 (参考 Connors RSI2 研究)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
)

META = {
    "id": "rsi2_reversion",
    "name": "RSI(2)超卖回归",
    "description": "收盘在 MA200 上方且 RSI(2) 低于超卖阈值时入场 (水平条件), RSI(2) 修复至退出阈值离场",
    "tags": ["RSI", "均值回归"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "oversold",
            "label": "超卖入场阈值",
            "type": "float",
            "default": 10.0,
            "min": 2.0,
            "max": 30.0,
            "step": 1.0,
        },
        {
            "id": "exit_level",
            "label": "退出阈值",
            "type": "float",
            "default": 65.0,
            "min": 40.0,
            "max": 90.0,
            "step": 5.0,
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
ENTRY_SIGNALS = ["signal_rsi2_reversion"]
EXIT_SIGNALS = ["signal_rsi2_reversion_exit"]
STOP_LOSS = -0.06
MAX_HOLD_DAYS = 15


class Rsi2ReversionMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        trend = int(params.get("trend_window", 200))
        return trend + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        oversold = float(params.get("oversold", 10.0))
        exit_level = float(params.get("exit_level", 65.0))
        trend_window = int(params.get("trend_window", 200))
        rsi = matrix_feature(market, "rsi_2")
        ma_trend = matrix_feature(market, f"ma{trend_window}")
        entry = (rsi < oversold) & (market.close > ma_trend)
        exit_ = rsi > exit_level
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_rsi2_reversion",),
            exit_signal_ids=("signal_rsi2_reversion_exit",),
        )


MATRIX_STRATEGY = Rsi2ReversionMatrixStrategy()
