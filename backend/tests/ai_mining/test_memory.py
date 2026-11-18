"""进化记忆测试: 持久化 round-trip / SOTA 去重阈值 / 损坏容错。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.ai_mining.memory import (
    DEDUP_CORRELATION_THRESHOLD,
    EvolutionMemory,
    HypothesisRecord,
    SotaFactor,
    is_duplicate_vs_sota,
    lessons_summary_for_prompt,
    load_memory,
    memory_summary_for_prompt,
    save_memory,
    sota_summary_for_prompt,
)


def _values(base_value: float, days: int = 20, symbols: int = 15) -> pl.DataFrame:
    rows = []
    for s in range(symbols):
        for d in range(days):
            rows.append(
                {
                    "symbol": f"6000{s:02d}.SH",
                    "date": date(2026, 6, 1) + timedelta(days=d),
                    "value": base_value * (1 + s) + d * 0.01,
                }
            )
    return pl.DataFrame(rows)


def _noisy_copy(df: pl.DataFrame, noise: float = 0.5, seed: int = 7) -> pl.DataFrame:
    import random

    rng = random.Random(seed)
    perturbed = df.with_columns(
        pl.col("value") + pl.Series("n", [rng.uniform(-noise, noise) for _ in range(df.height)])
    )
    return perturbed


class TestRoundTrip:
    def test_save_load_preserves_all(self, tmp_path: Path):
        memory = EvolutionMemory(
            rounds=[
                HypothesisRecord(
                    round=1,
                    hypothesis="低波动异象",
                    reason="低波股更抗跌",
                    factors=[{"name": "LOWVOL20"}],
                    metrics_summary="IC=0.09",
                    decision="accept",
                    observations="信号稳定",
                    lessons=["波动率用 20 日窗口更稳"],
                )
            ],
            sota_factors=[
                SotaFactor(id="LOWVOL20", name="LOWVOL20", description="低波动", formulation="-std(ret,20)", round=1, ic=0.09, ic_ir=0.3)
            ],
            lessons=["教训A"],
            llm_calls=17,
            max_llm_calls=120,
        )
        save_memory(tmp_path, memory)
        loaded = load_memory(tmp_path)
        assert loaded.rounds[0].hypothesis == "低波动异象"
        assert loaded.sota_factors[0].ic == pytest.approx(0.09)
        assert loaded.lessons == ["教训A"]
        assert loaded.llm_calls == 17

    def test_missing_file_returns_fresh(self, tmp_path: Path):
        memory = load_memory(tmp_path)
        assert memory.rounds == []
        assert memory.max_llm_calls == 120

    def test_corrupted_file_rebuilt_with_backup(self, tmp_path: Path):
        (tmp_path / "memory.json").write_text("{ not valid json !!", encoding="utf-8")
        memory = load_memory(tmp_path)
        assert memory.rounds == []
        assert (tmp_path / "memory.json.corrupt").is_file()


class TestDedup:
    def test_identical_values_flagged(self):
        sota = _values(1.0)
        dup = is_duplicate_vs_sota(_values(1.0), {"LOWVOL20": sota})
        assert dup == "LOWVOL20"

    def test_noisy_copy_above_threshold(self):
        sota = _values(1.0)
        dup = is_duplicate_vs_sota(_noisy_copy(sota, noise=0.05), {"LOWVOL20": sota})
        assert dup == "LOWVOL20"  # 微扰仍高相关

    def test_uncorrelated_not_flagged(self):
        sota = _values(1.0)
        anti = _values(1.0)
        # 反向 + 噪声: 与 SOTA 呈负相关，不应判重
        anti = anti.with_columns((-pl.col("value")).alias("value"))
        anti = _noisy_copy(anti, noise=2.0, seed=11)
        assert is_duplicate_vs_sota(anti, {"LOWVOL20": sota}) is None

    def test_all_nan_candidate_flagged_as_all_nan(self):
        df = pl.DataFrame({"symbol": ["a"], "date": [date(2026, 1, 1)], "value": [None]}, schema={"symbol": pl.String, "date": pl.Date, "value": pl.Float64})
        assert is_duplicate_vs_sota(df, {"X": _values(1.0)}) == "all_nan"

    def test_empty_sota_returns_none(self):
        assert is_duplicate_vs_sota(_values(1.0), {}) is None

    def test_threshold_constant_is_strict(self):
        assert pytest.approx(0.9) == DEDUP_CORRELATION_THRESHOLD


class TestPromptSummaries:
    def test_sota_summary_lines(self):
        memory = EvolutionMemory(
            sota_factors=[
                SotaFactor(id="A", name="A", description="因子A", formulation="f(a)", round=1, ic=0.1, ic_ir=0.5),
                SotaFactor(id="B", name="B", description="因子B", formulation="f(b)", round=1, ic=None, ic_ir=None),
            ]
        )
        text = sota_summary_for_prompt(memory)
        assert "因子A" in text and "0.1000" in text and "NA" in text

    def test_empty_summaries(self):
        memory = EvolutionMemory()
        assert "暂无" in sota_summary_for_prompt(memory)
        assert "暂无" in lessons_summary_for_prompt(memory)
        assert "首轮" in memory_summary_for_prompt(memory)

    def test_round_summary_contains_decision(self):
        memory = EvolutionMemory(rounds=[HypothesisRecord(round=2, hypothesis="h", reason="r", factors=[], metrics_summary="IC=0.02", decision="reject", observations="弱", lessons=[])])
        assert "[reject]" in memory_summary_for_prompt(memory)
