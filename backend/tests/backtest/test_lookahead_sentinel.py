"""哨兵自身的行为回归: 干净探针通过 / 泄漏探针判红 / 毒化炸探针走 skip。"""

from __future__ import annotations

import numpy as np
import pytest

from app.backtest.matrix import make_signal_matrix
from tests.backtest._lookahead_sentinel import assert_lookahead_free


class _CleanProbe:
    """仅回看的探针: 今收 vs 昨收。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return 2

    def compute_signals(self, market, params: dict):
        close = market.close
        prev = np.full_like(close, np.nan)
        prev[1:] = close[:-1]
        finite = ~np.isnan(prev)
        entry = (close > prev) & finite
        exit_ = (close < prev) & finite
        score = np.where(entry, 1.0, 0.0).astype(np.float32)
        return make_signal_matrix(
            close.shape, entry=entry, exit=exit_, score=score,
            entry_signal_ids=("probe_up",), exit_signal_ids=("probe_down",),
        )


class _LeakyProbe:
    """故意读未来: 末根 close 的符号广播进所有历史行。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return 2

    def compute_signals(self, market, params: dict):
        close = market.close
        entry = np.zeros(close.shape, dtype=bool)
        # NaN 列毒化后为 False, 基线 (正价) 为 True — 截断点前必然出现差异
        entry[:] = close[-1] >= 0
        return make_signal_matrix(close.shape, entry=entry)


class _PerturbCrashProbe:
    """毒化必炸探针: 输入含 NaN 即抛错 (验证 skip 分支)。"""

    def required_fields(self) -> frozenset[str]:
        return frozenset({"close"})

    def required_warmup_bars(self, params: dict) -> int:
        return 2

    def compute_signals(self, market, params: dict):
        close = market.close
        if not np.isfinite(close).all():
            raise ValueError("非有限输入")
        entry = np.zeros(close.shape, dtype=bool)
        entry[:, 0] = True
        return make_signal_matrix(close.shape, entry=entry)


def test_clean_probe_passes_sentinel():
    assert_lookahead_free(_CleanProbe(), params={}, warmup_bars=2)


def test_leaky_probe_is_detected():
    with pytest.raises(AssertionError, match="前视泄漏"):
        assert_lookahead_free(_LeakyProbe(), params={}, warmup_bars=2)


def test_perturb_failure_skips_instead_of_failing():
    with pytest.raises(pytest.skip.Exception, match="输入敏感性"):
        assert_lookahead_free(_PerturbCrashProbe(), params={}, warmup_bars=2)
