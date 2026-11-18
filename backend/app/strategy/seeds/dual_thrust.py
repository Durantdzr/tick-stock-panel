"""Dual Thrust — 前 N 日区间 × 非对称系数构造上下轨, 上穿入场, 下穿退出 (经典公共领域策略)"""

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
    "id": "dual_thrust",
    "name": "Dual Thrust区间突破",
    "description": "以前N日 HH−LC 与 HC−LL 的较大值构造区间, 收盘上穿开盘价+k1×区间入场, 下穿开盘价−k2×区间离场",
    "tags": ["突破", "区间"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "range_window",
            "label": "区间回看窗口",
            "type": "int",
            "default": 4,
            "min": 2,
            "max": 20,
            "step": 1,
        },
        {
            "id": "k_up",
            "label": "上轨系数k1",
            "type": "float",
            "default": 0.5,
            "min": 0.1,
            "max": 1.5,
            "step": 0.1,
        },
        {
            "id": "k_down",
            "label": "下轨系数k2",
            "type": "float",
            "default": 0.5,
            "min": 0.1,
            "max": 1.5,
            "step": 0.1,
        },
    ],
    "scoring": {"momentum_20d": 0.4, "vol_ratio_5d": 0.3, "change_pct": 0.3},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_dual_thrust"]
EXIT_SIGNALS = ["signal_dual_thrust_exit"]
STOP_LOSS = -0.07
MAX_HOLD_DAYS = 15


class DualThrustMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"open", "close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("range_window", 4))
        return window + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("range_window", 4))
        k_up = float(params.get("k_up", 0.5))
        k_down = float(params.get("k_down", 0.5))
        # 区间取前一交易日及以前, 避免把当日纳入参考区间
        hh = shift(rolling_max(market.high, window), 1)
        lc = shift(rolling_min(market.close, window), 1)
        hc = shift(rolling_max(market.close, window), 1)
        ll = shift(rolling_min(market.low, window), 1)
        rng = np.maximum(hh - lc, hc - ll)
        top = market.open + k_up * rng
        bottom = market.open - k_down * rng
        entry = (market.close > top) & (shift(market.close, 1) <= shift(top, 1))
        exit_ = (market.close < bottom) & (shift(market.close, 1) >= shift(bottom, 1))
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_dual_thrust",),
            exit_signal_ids=("signal_dual_thrust_exit",),
        )


MATRIX_STRATEGY = DualThrustMatrixStrategy()
