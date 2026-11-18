"""一目均衡表 — 转换线/基准线交叉 + 云层过滤 (经典公共领域策略)"""

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    valid_rolling_max,
    valid_rolling_min,
)
from app.backtest.matrix import (
    valid_shift as shift,
)

META = {
    "id": "ichimoku_trend",
    "name": "一目均衡表趋势",
    "description": "转换线上穿基准线且收盘高于云层当日入场; 转换线下穿基准线或收盘跌破云层离场",
    "tags": ["趋势", "云层"],
    "asset_types": ["stock", "etf"],
    "timeframes": ["1d"],
    "params": [
        {
            "id": "tenkan_window",
            "label": "转换线窗口",
            "type": "int",
            "default": 9,
            "min": 5,
            "max": 30,
            "step": 1,
        },
        {
            "id": "kijun_window",
            "label": "基准线窗口",
            "type": "int",
            "default": 26,
            "min": 10,
            "max": 60,
            "step": 1,
        },
        {
            "id": "senkou_window",
            "label": "先行带B窗口",
            "type": "int",
            "default": 52,
            "min": 20,
            "max": 120,
            "step": 2,
        },
        {
            "id": "displacement",
            "label": "云层位移",
            "type": "int",
            "default": 26,
            "min": 10,
            "max": 60,
            "step": 1,
        },
    ],
    "scoring": {"momentum_20d": 0.5, "annual_vol_20d": 0.3, "change_pct": 0.2},
    "order_by": "score",
    "descending": True,
    "limit": 100,
}

EXECUTION_BACKEND = "matrix_native"
ENTRY_SIGNALS = ["signal_ichimoku_trend"]
EXIT_SIGNALS = ["signal_ichimoku_trend_exit"]
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 40


class IchimokuTrendMatrixStrategy:
    def required_fields(self) -> frozenset[str]:
        return frozenset({"close", "high", "low"})

    def required_warmup_bars(self, params: dict) -> int:
        senkou = int(params.get("senkou_window", 52))
        disp = int(params.get("displacement", 26))
        return senkou + disp + 5

    def compute_signals(self, market: MarketDataMatrix, params: dict) -> SignalMatrix:
        tenkan_n = int(params.get("tenkan_window", 9))
        kijun_n = int(params.get("kijun_window", 26))
        senkou_n = int(params.get("senkou_window", 52))
        disp = int(params.get("displacement", 26))
        valid = np.isfinite(market.close)

        def midpoint(window: int) -> np.ndarray:
            return (
                valid_rolling_max(market.high, valid, window)
                + valid_rolling_min(market.low, valid, window)
            ) / 2.0

        tenkan = midpoint(tenkan_n)
        kijun = midpoint(kijun_n)
        # 当前 bar 所见的云层 = 26 日前计算的先行带 A/B (位移前视排除)
        senkou_a = shift((tenkan + kijun) / 2.0, disp)
        senkou_b = shift(midpoint(senkou_n), disp)
        cloud_top = np.maximum(senkou_a, senkou_b)
        cloud_bottom = np.minimum(senkou_a, senkou_b)
        golden = (tenkan > kijun) & (shift(tenkan, 1) <= shift(kijun, 1))
        dead = (tenkan < kijun) & (shift(tenkan, 1) >= shift(kijun, 1))
        entry = golden & (market.close > cloud_top)
        exit_ = dead | (market.close < cloud_bottom)
        return make_signal_matrix(
            market.shape,
            entry=entry.astype(np.uint8),
            exit=exit_.astype(np.uint8),
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            exit_signal_code=np.where(exit_, 0, -1).astype(np.int16),
            entry_signal_ids=("signal_ichimoku_trend",),
            exit_signal_ids=("signal_ichimoku_trend_exit",),
        )


MATRIX_STRATEGY = IchimokuTrendMatrixStrategy()
