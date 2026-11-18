"""DMI趋势 — +DI > −DI 且 ADX 达到趋势强度阈值时入场 (Wilder 公共领域算法)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    safe_divide,
    valid_ewm_adjust_false,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "adx_trend",
    "name": "DMI趋势强度",
    "description": "+DI 高于 −DI 且 ADX 不低于阈值时入场 (水平条件), −DI 高于 +DI 时离场",
    "tags": ["趋势", "DMI"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "window",
            "label": "平滑窗口",
            "type": "int",
            "default": 14,
            "min": 7,
            "max": 40,
            "step": 1,
        },
        {
            "id": "adx_threshold",
            "label": "ADX阈值",
            "type": "float",
            "default": 25.0,
            "min": 10.0,
            "max": 45.0,
            "step": 1.0,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_adx_trend"]
EXIT_SIGNALS = ["signal_adx_trend_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 40


class AdxTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        window = int(params.get("window", 14))
        return window * 3 + 10

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        window = int(params.get("window", 14))
        threshold = float(params.get("adx_threshold", 25.0))
        valid = np.isfinite(market.close)
        prev_high = shift(market.high, 1)
        prev_low = shift(market.low, 1)
        up_move = market.high - prev_high
        down_move = prev_low - market.low
        has_prev = np.isfinite(up_move) & np.isfinite(down_move)
        plus_dm = np.where(has_prev & (up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where(has_prev & (down_move > up_move) & (down_move > 0), down_move, 0.0)
        prev_close = shift(market.close, 1)
        with_pc = np.isfinite(prev_close)
        tr = np.where(with_pc, np.maximum.reduce([
            market.high - market.low,
            np.abs(market.high - prev_close),
            np.abs(market.low - prev_close),
        ]), market.high - market.low)
        alpha = 1.0 / float(window)
        smooth_tr = valid_ewm_adjust_false(tr, valid, alpha=alpha)
        smooth_plus = valid_ewm_adjust_false(plus_dm, valid, alpha=alpha)
        smooth_minus = valid_ewm_adjust_false(minus_dm, valid, alpha=alpha)
        eps = np.float32(1e-12)
        plus_di = 100.0 * safe_divide(smooth_plus, smooth_tr + eps)
        minus_di = 100.0 * safe_divide(smooth_minus, smooth_tr + eps)
        di_sum = plus_di + minus_di
        dx = 100.0 * safe_divide(np.abs(plus_di - minus_di), di_sum + eps)
        adx = valid_ewm_adjust_false(dx, valid, alpha=alpha)
        entry = (plus_di > minus_di) & np.isfinite(adx) & (adx >= threshold)
        exit_ = minus_di > plus_di
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_adx_trend",),
            exit_signal_ids=("signal_adx_trend_exit",),
        )


MATRIX_STRATEGY = AdxTrendMatrixStrategy()
