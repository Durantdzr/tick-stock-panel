"""循环/运行时集成测试: 假 LLM + 合成面板, 全流程进程内跑通（不触真实数据）。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from app.ai_mining import llm as llm_mod
from app.ai_mining import runtime as runtime_mod
from app.ai_mining.memory import load_memory
from app.backtest.worker import make_worker_task

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

EVAL_START = date(2026, 6, 10)
EVAL_END = date(2026, 6, 25)


class ScriptedLLM:
    """按序回放响应; 记录调用内容供断言。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    async def __call__(self, messages, **kwargs):
        content = messages[-1]["content"] if messages else ""
        self.calls.append(content[:120])
        if not self.responses:
            raise AssertionError("脚本化 LLM 响应已耗尽")
        return self.responses.pop(0)


def _panel_df() -> pd.DataFrame:
    rows = []
    for index in range(6):
        base = pd.Timestamp("2026-05-01")
        for day in range(60):
            rows.append(
                {
                    "symbol": f"60000{index:02d}.SH",
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
    return pd.DataFrame(rows)


def _price_loader(panel: pd.DataFrame):
    df = pl.from_pandas(panel).with_columns(pl.col("date").cast(pl.Date))

    def loader(start, end, columns, asset_type):
        selected = [c for c in columns if c in df.columns]
        return df.filter((pl.col("date") >= start) & (pl.col("date") <= end)).select(selected)

    return loader


class _DuckEngine:
    """runtime 面板供给用的最小引擎替身。"""

    def __init__(self, panel: pd.DataFrame) -> None:
        self._panel = panel

    def load_panel(self, symbols, start, end, columns=None, asset_type="stock", **_):
        df = pl.from_pandas(self._panel).with_columns(pl.col("date").cast(pl.Date))
        selected = [c for c in (columns or []) if c in df.columns]
        return df.filter((pl.col("date") >= start) & (pl.col("date") <= end)).select(selected)


def _scripted_responses() -> list[str]:
    proposal_json = json.dumps(
        {
            "hypothesis": "动量在温和上涨的市场里可以延续",
            "reason": "合成数据呈趋势上行",
            "factors": [
                {"name": "MOM5", "description": "5日动量", "formulation": "close/close.shift(5)-1", "direction": "positive"}
            ],
        },
        ensure_ascii=False,
    )
    feedback_json = json.dumps(
        {"decision": "accept", "observations": "IC 为正且稳定", "lessons": ["趋势市可沿用动量族"]},
        ensure_ascii=False,
    )
    return [proposal_json, f"```python\n{VALID_FACTOR_CODE}\n```", feedback_json]


def test_runtime_full_round(tmp_path: Path, monkeypatch):
    scripted = ScriptedLLM(_scripted_responses())
    monkeypatch.setattr(llm_mod, "generate_ai_text", scripted)
    panel = _panel_df()
    monkeypatch.setattr(runtime_mod, "make_price_loader", lambda data_dir, asset_type: _price_loader(panel))

    run_dir = runtime_mod.run_dir_for(tmp_path, "runtest1")
    progress_events: list[dict] = []
    summary = runtime_mod.run_ai_mining_runtime(
        {
            "run_id": "runtest1",
            "request": {
                "eval_start": EVAL_START.isoformat(),
                "eval_end": EVAL_END.isoformat(),
                "rounds": 1,
                "factors_per_round": 1,
                "max_llm_calls": 10,
            },
        },
        data_dir=tmp_path,
        engine=_DuckEngine(panel),
        progress_cb=progress_events.append,
        cancel_check=None,
    )

    assert summary["stopped_reason"] == "completed"
    assert summary["llm_calls"] == 3
    assert summary["sota_count"] == 1
    rounds = summary["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["decision"] == "accept"
    assert rounds[0]["factors_ok"] == 1
    assert "MOM5" in rounds[0]["metrics_summary"]

    # 产物核验: 面板/因子工作区/记忆持久化
    assert (run_dir / "panel.parquet").is_file()
    memory = load_memory(run_dir)
    assert memory.llm_calls == 3
    assert memory.rounds[0].decision == "accept"
    assert memory.sota_factors[0].id == "MOM5"
    assert (run_dir / memory.sota_factors[0].values_path).is_file()
    phases = [event["phase"] for event in progress_events]
    assert phases[0] == "propose" and phases[-1] == "record"


def test_runtime_cancel_stops_before_round(tmp_path: Path, monkeypatch):
    scripted = ScriptedLLM(_scripted_responses())
    monkeypatch.setattr(llm_mod, "generate_ai_text", scripted)
    panel = _panel_df()
    monkeypatch.setattr(runtime_mod, "make_price_loader", lambda data_dir, asset_type: _price_loader(panel))

    summary = runtime_mod.run_ai_mining_runtime(
        {
            "run_id": "runcancel",
            "request": {
                "eval_start": EVAL_START.isoformat(),
                "eval_end": EVAL_END.isoformat(),
                "rounds": 1,
                "max_llm_calls": 10,
            },
        },
        data_dir=tmp_path,
        engine=_DuckEngine(panel),
        cancel_check=lambda: True,
    )
    assert summary["stopped_reason"] == "cancelled"
    assert summary["llm_calls"] == 0


class TestWorkerKind:
    def test_make_task_accepts_dict_config(self, tmp_path: Path):
        config = {"run_id": "r1", "request": {"eval_start": "2026-06-10", "eval_end": "2026-06-25"}}
        task = make_worker_task("llm_evo", tmp_path, config)
        assert task["kind"] == "llm_evo"
        assert task["config"] == config

    def test_make_task_rejects_non_dict(self, tmp_path: Path):
        with pytest.raises(TypeError, match="llm_evo"):
            make_worker_task("llm_evo", tmp_path, "not-a-dict")
