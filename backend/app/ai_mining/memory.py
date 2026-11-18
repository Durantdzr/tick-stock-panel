"""结构化进化记忆: 假设/指标/教训/SOTA 链的持久化与 prompt 摘要。

设计取舍: 不做向量 RAG（零 embedding 依赖），把记忆压成结构化 JSON 直接
注入提示词——轮数有限（个位数）时信息密度足够，且完全可审计。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import polars as pl

from app.services.fs_utils import atomic_write_text

MEMORY_FILE = "memory.json"

# 去重阈值: 候选因子与某 SOTA 的逐日截面秩相关均值超过该值即视为高度相似
DEDUP_CORRELATION_THRESHOLD = 0.9
_DEDUP_MIN_PAIRS_PER_DAY = 10


@dataclass
class HypothesisRecord:
    round: int
    hypothesis: str
    reason: str
    factors: list[dict]
    metrics_summary: str
    decision: str  # accept / reject
    observations: str
    lessons: list[str] = field(default_factory=list)


@dataclass
class SotaFactor:
    id: str
    name: str
    description: str
    formulation: str
    round: int
    ic: float | None = None
    ic_ir: float | None = None
    values_path: str = ""  # run 目录内相对路径


@dataclass
class EvolutionMemory:
    rounds: list[HypothesisRecord] = field(default_factory=list)
    sota_factors: list[SotaFactor] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    llm_calls: int = 0
    max_llm_calls: int = 120

    def can_call_llm(self, extra: int = 1) -> bool:
        return self.llm_calls + extra <= self.max_llm_calls

    def summary_lines(self) -> list[str]:
        lines: list[str] = []
        for record in self.rounds:
            lines.append(
                f"第{record.round}轮[{record.decision}] 假设: {record.hypothesis} | "
                f"指标: {record.metrics_summary} | 观察: {record.observations}"
            )
        return lines


def load_memory(run_dir: Path) -> EvolutionMemory:
    """读取记忆; 文件缺失或损坏时返回新记忆（损坏文件改名留存以便排查）。"""
    path = Path(run_dir) / MEMORY_FILE
    if not path.is_file():
        return EvolutionMemory()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rounds = [HypothesisRecord(**item) for item in data.get("rounds", [])]
        sota = [SotaFactor(**item) for item in data.get("sota_factors", [])]
        return EvolutionMemory(
            rounds=rounds,
            sota_factors=sota,
            lessons=list(data.get("lessons", [])),
            llm_calls=int(data.get("llm_calls", 0)),
            max_llm_calls=int(data.get("max_llm_calls", 120)),
        )
    except (json.JSONDecodeError, TypeError, KeyError, ValueError):
        corrupted = path.with_suffix(".json.corrupt")
        if path.exists():
            path.replace(corrupted)
        return EvolutionMemory()


def save_memory(run_dir: Path, memory: EvolutionMemory) -> None:
    atomic_write_text(Path(run_dir) / MEMORY_FILE, json.dumps(asdict(memory), ensure_ascii=False, indent=1))


def sota_summary_for_prompt(memory: EvolutionMemory, *, limit: int = 12) -> str:
    if not memory.sota_factors:
        return "（暂无 SOTA 因子，本轮为首轮）"
    ranked = sorted(memory.sota_factors, key=lambda s: (s.ic_ir or 0), reverse=True)[:limit]
    lines = [f"- {s.id}: {s.description} | 公式: {s.formulation} | IC={_fmt(s.ic)} IC_IR={_fmt(s.ic_ir)}" for s in ranked]
    return "\n".join(lines)


def lessons_summary_for_prompt(memory: EvolutionMemory) -> str:
    if not memory.lessons:
        return "（暂无）"
    return "\n".join(f"- {lesson}" for lesson in memory.lessons[-6:])


def memory_summary_for_prompt(memory: EvolutionMemory, *, last_rounds: int = 3) -> str:
    lines = memory.summary_lines()
    if not lines:
        return "（首轮，无历史）"
    return "\n".join(lines[-last_rounds:])


def _fmt(value: float | None) -> str:
    return "NA" if value is None else f"{value:.4f}"


def is_duplicate_vs_sota(
    candidate: pl.DataFrame,
    sota_values: dict[str, pl.DataFrame],
    *,
    threshold: float = DEDUP_CORRELATION_THRESHOLD,
) -> str | None:
    """候选因子值 vs SOTA 的逐日截面秩相关均值去重。

    candidate/sota_values 的 DataFrame 均为 (symbol, date, value) 长表。
    返回首个相关性超阈值的 SOTA 因子 id；无相似返回 None。
    """
    if not sota_values:
        return None
    cand = candidate.drop_nulls(subset=["value"]).to_pandas()
    if len(cand) == 0:
        return "all_nan"
    for factor_id, sota in sota_values.items():
        merged = cand.merge(sota.drop_nulls(subset=["value"]).to_pandas(), on=["symbol", "date"], suffixes=("_c", "_s"))
        if len(merged) == 0:
            continue
        merged["_rc"] = merged.groupby("date")["value_c"].rank(method="average")
        merged["_rs"] = merged.groupby("date")["value_s"].rank(method="average")
        day_corrs: list[float] = []
        for _, group in merged.groupby("date"):
            if len(group) < _DEDUP_MIN_PAIRS_PER_DAY:
                continue
            corr = group["_rc"].corr(group["_rs"])
            if corr == corr:  # 过滤 NaN（单边常数）
                day_corrs.append(float(corr))
        if day_corrs and sum(day_corrs) / len(day_corrs) >= threshold:
            return factor_id
    return None
