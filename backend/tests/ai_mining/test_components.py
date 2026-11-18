"""proposal/coder/evaluator 组件测试: mock LLM，全链路不触网。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.ai_mining import coder as coder_mod
from app.ai_mining import evaluator as evaluator_mod
from app.ai_mining import llm as llm_mod
from app.ai_mining import proposal as proposal_mod
from app.ai_mining.memory import (
    EvolutionMemory,
    SotaFactor,
    load_memory,
    save_memory,
)
from app.ai_mining.proposal import FactorProposal

VALID_FACTOR_CODE = '''
import pandas as pd

def calculate_mom5():
    df = pd.read_parquet("panel.parquet")
    df = df.sort_values(["symbol", "date"])
    close = df.set_index(["symbol", "date"])["close"].unstack(level="symbol")
    mom = close / close.shift(5) - 1.0
    out = mom.stack().rename("value").reset_index()
    out.to_parquet("factor_values.parquet", index=False)

if __name__ == "__main__":
    calculate_mom5()
'''


def _write_panel(workdir: Path, n_days: int = 30, symbols: tuple[str, ...] = ("600001.SH", "600002.SH")) -> None:
    import pandas as pd

    rows = []
    for index, symbol in enumerate(symbols):
        base = pd.Timestamp("2026-06-01")
        for day in range(n_days):
            rows.append(
                {
                    "symbol": symbol,
                    "date": base + pd.Timedelta(days=day),
                    "open": 10.0 + index,
                    "high": 10.5 + index,
                    "low": 9.5 + index,
                    "close": 10.0 + index + day * (0.1 + index * 0.05),
                    "volume": 1000.0 + day,
                    "amount": 1e6 + day,
                    "turnover_rate": 0.02,
                }
            )
    workdir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(workdir / "panel.parquet", index=False)


class TestProposal:
    async def test_happy_path_salvages_valid_factors(self, monkeypatch):
        dirty = "```json\n" + json.dumps(
            {
                "hypothesis": "低波动异象在 A 股持续",
                "reason": "避险需求",
                "factors": [
                    {"name": "LOWVOL20", "description": "20日波动率取负", "formulation": "-std(ret,20)", "direction": "positive"},
                    {"name": "bad-name!", "description": "x", "formulation": "y"},
                    {"name": "LOWVOL20", "description": "重复", "formulation": "z"},
                ],
            },
            ensure_ascii=False,
        ) + ",\n垃圾尾巴\n```"
        async def fake_llm(messages, **kwargs):
            return dirty
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        memory = EvolutionMemory()
        proposal = await proposal_mod.propose(memory, factors_limit=3)
        assert proposal.hypothesis.startswith("低波动")
        assert [f.name for f in proposal.factors] == ["LOWVOL20"]
        assert proposal.factors[0].direction == "positive"
        assert len(proposal.skipped) == 2
        assert memory.llm_calls == 1

    async def test_missing_hypothesis_raises(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return json.dumps({"reason": "只有 reason"})
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        with pytest.raises(proposal_mod.ProposalError, match="hypothesis"):
            await proposal_mod.propose(EvolutionMemory(), factors_limit=2)

    async def test_no_valid_factors_raises(self, monkeypatch):
        async def fake_llm(messages, **kwargs):
            return json.dumps({"hypothesis": "h", "reason": "r", "factors": [{"name": "!!!", "description": "d", "formulation": "f"}]})
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        with pytest.raises(proposal_mod.ProposalError, match="无有效因子"):
            await proposal_mod.propose(EvolutionMemory(), factors_limit=2)


class TestCoder:
    async def test_happy_path(self, monkeypatch, tmp_path: Path):
        async def fake_llm(messages, **kwargs):
            return f"```python\n{VALID_FACTOR_CODE}\n```"
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        _write_panel(tmp_path)
        memory = EvolutionMemory()
        proposal = FactorProposal(name="MOM5", description="5日动量", formulation="close/close.shift(5)-1", direction="positive")
        impl = await coder_mod.implement_factor(proposal, tmp_path, memory, exec_timeout=60)
        assert impl.repair_history == []
        assert impl.values_path.is_file()
        assert memory.llm_calls == 1

    async def test_repair_after_validation_failure(self, monkeypatch, tmp_path: Path):
        calls = {"n": 0}
        async def fake_llm(messages, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return "```python\nimport pandas as pd\ndf = pd.read_csv('x.csv')\n```\n"
            return f"```python\n{VALID_FACTOR_CODE}\n```\n"
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        _write_panel(tmp_path)
        memory = EvolutionMemory()
        proposal = FactorProposal(name="MOM5", description="d", formulation="f", direction="positive")
        impl = await coder_mod.implement_factor(proposal, tmp_path, memory, exec_timeout=60)
        assert len(impl.repair_history) == 1
        assert "read_csv" in impl.repair_history[0]
        assert memory.llm_calls == 2

    async def test_exhausted_repairs_raises(self, monkeypatch, tmp_path: Path):
        async def fake_llm(messages, **kwargs):
            return "```python\nimport pandas as pd\ndf = pd.read_csv('x.csv')\n```\n"
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        _write_panel(tmp_path)
        memory = EvolutionMemory()
        proposal = FactorProposal(name="BAD", description="d", formulation="f", direction="positive")
        with pytest.raises(Exception) as excinfo:
            await coder_mod.implement_factor(proposal, tmp_path, memory, max_repairs=2, exec_timeout=30)
        assert "codegen_failed" in str(excinfo.value)
        assert memory.llm_calls == 3


class TestEvaluator:
    def _prices(self, n_days: int = 45, symbols: int = 6) -> pl.DataFrame:
        rows = []
        for index in range(symbols):
            for day in range(n_days):
                rows.append({
                    "symbol": f"60000{index:02d}.SH",
                    "date": date(2026, 5, 20) + timedelta(days=day),
                    "open": 10.0 + index, "high": 10.5 + index, "low": 9.5 + index,
                    "close": 10.0 + index * 2 + day * (0.1 + index * 0.05),
                    "volume": 1000.0 + day, "amount": 1e6 + day, "turnover_rate": 0.02,
                })
        return pl.DataFrame(rows)

    def _loader(self, panel: pl.DataFrame):
        def loader(start, end, columns, asset_type):
            selected = [c for c in columns if c in panel.columns]
            return panel.filter((pl.col("date") >= start) & (pl.col("date") <= end)).select(selected)
        return loader

    def _factor_values(self, symbols: int = 6) -> pl.DataFrame:
        rows = []
        for index in range(symbols):
            for day in range(12, 42):
                rows.append({"symbol": f"60000{index:02d}.SH", "date": date(2026, 5, 20) + timedelta(days=day), "value": float(index)})
        return pl.DataFrame(rows)

    def test_evaluate_ok(self, tmp_path: Path):
        values_path = tmp_path / "f.parquet"
        self._factor_values().write_parquet(values_path)
        outcome = evaluator_mod.evaluate_factor_outcome(
            FactorProposal(name="LOWVOL20", description="d", formulation="f", direction="positive"),
            values_path,
            start=date(2026, 6, 1), end=date(2026, 6, 30),
            price_loader=self._loader(self._prices()),
            run_dir=tmp_path,
        )
        assert outcome.ok
        assert outcome.payload["metrics"]["IC"] is not None
        assert "LOWVOL20" in outcome.metrics_line()

    def test_duplicate_flagged(self, tmp_path: Path):
        # 去重要求每日 >=10 对样本, 用 15 只股票构造截面
        values_path = tmp_path / "f.parquet"
        self._factor_values(symbols=15).write_parquet(values_path)
        sota_values = tmp_path / "sota_LOWVOL20.parquet"
        self._factor_values(symbols=15).write_parquet(sota_values)
        memory = EvolutionMemory(sota_factors=[SotaFactor(id="LOWVOL20", name="LOWVOL20", description="d", formulation="f", round=1, values_path="sota_LOWVOL20.parquet")])
        save_memory(tmp_path, memory)
        outcome = evaluator_mod.evaluate_factor_outcome(
            FactorProposal(name="CLONE", description="d", formulation="f", direction="positive"),
            values_path,
            start=date(2026, 6, 1), end=date(2026, 6, 30),
            price_loader=self._loader(self._prices()),
            run_dir=tmp_path,
        )
        assert outcome.duplicate_of == "LOWVOL20"
        assert not outcome.ok
        assert "高度相似" in outcome.metrics_line()

    async def test_feedback_llm_and_fallback(self, monkeypatch, tmp_path: Path):
        evaluation = evaluator_mod.RoundEvaluation(outcomes=[])
        async def fake_llm(messages, **kwargs):
            return json.dumps({"decision": "accept", "observations": "信号可延续", "lessons": ["保持低波方向", "  "]})
        monkeypatch.setattr(llm_mod, "generate_ai_text", fake_llm)
        memory = EvolutionMemory()
        feedback = await evaluator_mod.generate_round_feedback("h", evaluation, memory, eval_days=90)
        assert feedback["decision"] == "accept"
        assert feedback["lessons"] == ["保持低波方向"]

        async def broken_llm(messages, **kwargs):
            raise RuntimeError("上游挂了")
        monkeypatch.setattr(llm_mod, "generate_ai_text", broken_llm)
        fallback = await evaluator_mod.generate_round_feedback("h", evaluation, memory, eval_days=90)
        assert fallback["decision"] == "reject"  # 无 ok outcome 时规则兜底
        assert load_memory(tmp_path).llm_calls == 0  # memory 由 loop 落盘, 本函数不写
