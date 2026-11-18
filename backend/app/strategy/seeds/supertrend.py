"""SuperTrend — ATR 通道带棘轮 + 方向翻转线, 方向翻转入/离场 (经典公共领域算法)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    valid_rolling_mean,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "supertrend",
    "name": "SuperTrend",
    "description": "ATR 通道带只朝趋势方向收紧: 多头跟踪下带, 空头跟踪上带; 收盘跌破下带翻空, 收复上带翻多",
    "tags": ["趋势", "ATR"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "atr_window",
            "label": "ATR窗口",
            "type": "int",
            "default": 10,
            "min": 5,
            "max": 60,
            "step": 1,
        },
        {
            "id": "multiplier",
            "label": "ATR倍数",
            "type": "float",
            "default": 3.0,
            "min": 1.0,
            "max": 6.0,
            "step": 0.5,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_supertrend"]
EXIT_SIGNALS = ["signal_supertrend_exit"]
STOP_LOSS = -0.10
MAX_HOLD_DAYS = 60


class SuperTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("atr_window", 10))
        return window + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("atr_window", 10))
        k = float(params.get("multiplier", 3.0))
        close = market.close
        close_valid = np.isfinite(close)
        prev_close = shift(close, 1)
        tr = market.high - market.low
        with_pc = np.isfinite(prev_close)
        tr = np.where(with_pc, np.maximum.reduce([
            tr,
            np.abs(market.high - prev_close),
            np.abs(market.low - prev_close),
        ]), tr)
        atr = valid_rolling_mean(tr, close_valid, window)
        hl2 = (market.high + market.low) / 2.0
        basic_upper = hl2 + k * atr
        basic_lower = hl2 - k * atr

        # 棘轮递推: 逐 bar 行循环, 行内按列向量化, 避免逐标的 Python 循环
        rows, cols = market.shape
        direction = np.zeros(market.shape, dtype=np.int8)
        prev_dir = np.zeros(cols, dtype=np.int8)
        prev_fu = np.full(cols, np.nan, dtype=np.float32)
        prev_fl = np.full(cols, np.nan, dtype=np.float32)
        for i in range(rows):
            broke_up = close[i - 1] > prev_fu if i > 0 else np.zeros(cols, dtype=bool)
            broke_down = close[i - 1] < prev_fl if i > 0 else np.zeros(cols, dtype=bool)
            # 上带只在新带更低或价格已突破旧上带时下移, 否则保持旧带 (棘轮)
            keep_upper = (
                np.isfinite(prev_fu)
                & np.isfinite(basic_upper[i])
                & (basic_upper[i] >= prev_fu)
                & ~broke_up
            )
            new_fu = np.where(keep_upper, prev_fu, basic_upper[i])
            keep_lower = (
                np.isfinite(prev_fl)
                & np.isfinite(basic_lower[i])
                & (basic_lower[i] <= prev_fl)
                & ~broke_down
            )
            new_fl = np.where(keep_lower, prev_fl, basic_lower[i])
            # 多头跟踪下带: 收盘跌破下带翻空; 空头跟踪上带: 收盘收复上带翻多
            flip_up = (prev_dir != 1) & np.isfinite(new_fu) & (close[i] > new_fu)
            flip_down = (prev_dir != -1) & np.isfinite(new_fl) & (close[i] < new_fl)
            prev_dir = np.where(flip_up, 1, np.where(flip_down, -1, prev_dir)).astype(np.int8)
            direction[i] = prev_dir
            prev_fu = new_fu
            prev_fl = new_fl

        entry = np.zeros(market.shape, dtype=bool)
        exit_ = np.zeros(market.shape, dtype=bool)
        if rows > 1:
            entry[1:] = (direction[1:] == 1) & (direction[:-1] != 1)
            exit_[1:] = (direction[1:] == -1) & (direction[:-1] != -1)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_supertrend",),
            exit_signal_ids=("signal_supertrend_exit",),
        )


MATRIX_STRATEGY = SuperTrendMatrixStrategy()
