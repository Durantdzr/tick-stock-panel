"""评估与反馈: 因子值 → 面板评估 → SOTA 去重 → LLM 反馈 JSON。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.ai_mining.llm import call_llm
from app.ai_mining.memory import (
    EvolutionMemory,
    lessons_summary_for_prompt,
    load_memory,
    sota_summary_for_prompt,
)
from app.ai_mining.prompts import (
    FEEDBACK_SYSTEM_TEMPLATE,
    FEEDBACK_USER_TEMPLATE,
)
from app.ai_mining.proposal import FactorProposal
from app.ai_mining.sandbox import FactorSandboxError
from app.backtest.factor_panel import EvalError, evaluate_factor_values
from app.strategy.custom_signals_ai import _extract_json_object


@dataclass
class FactorEvalOutcome:
    proposal: FactorProposal
    payload: dict | None = None
    duplicate_of: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.payload is not None and self.payload.get("status") == "ok" and self.duplicate_of is None

    @property
    def ic(self) -> float | None:
        if not self.payload:
            return None
        return (self.payload.get("metrics") or {}).get("IC")

    @property
    def ic_ir(self) -> float | None:
        if not self.payload:
            return None
        return (self.payload.get("metrics") or {}).get("IC_IR")

    def metrics_line(self) -> str:
        if self.duplicate_of is not None:
            return f"{self.proposal.name}: 与 SOTA 因子 {self.duplicate_of} 高度相似, 去重拒绝"
        if self.payload is None or self.payload.get("status") != "ok":
            return f"{self.proposal.name}: 评估失败 ({(self.error or self.payload.get('error', 'unknown'))[:120] if self.payload else self.error})"
        metrics = self.payload.get("metrics") or {}
        meta = self.payload.get("meta") or {}
        return (
            f"{self.proposal.name}: IC={_fmt(metrics.get('IC'))} IC_IR={_fmt(metrics.get('IC_IR'))} "
            f"IC胜率={_fmt_pct(metrics.get('IC_win_rate'))} Q5年化={_fmt_pct(metrics.get('1day.excess_return_with_cost.annualized_return'))} "
            f"Q5回撤={_fmt_pct(metrics.get('1day.excess_return_with_cost.max_drawdown'))} "
            f"多空收益={_fmt_pct(metrics.get('long_short_total_return'))} 覆盖率={_fmt_pct(meta.get('coverage'))}"
        )


@dataclass
class RoundEvaluation:
    outcomes: list[FactorEvalOutcome] = field(default_factory=list)
    feedback: dict | None = None  # {"decision", "observations", "lessons"}

    def metrics_summary(self) -> str:
        return "；".join(outcome.metrics_line() for outcome in self.outcomes)

    @property
    def decision(self) -> str:
        feedback = self.feedback or {}
        return str(feedback.get("decision", "reject"))

    @property
    def observations(self) -> str:
        return str((self.feedback or {}).get("observations", ""))

    @property
    def lessons(self) -> list[str]:
        raw = (self.feedback or {}).get("lessons", [])
        return [str(lesson)[:200] for lesson in raw if str(lesson).strip()][:3]


def evaluate_factor_outcome(
    proposal: FactorProposal,
    values_path: Path,
    *,
    start: date,
    end: date,
    price_loader,
    run_dir: Path,
) -> FactorEvalOutcome:
    """单因子评估（含与既有 SOTA 的去重），进程内同步调用（worker 已隔离）。"""
    import polars as pl

    values = pl.read_parquet(values_path)
    outcome = FactorEvalOutcome(proposal=proposal)

    # SOTA 值从 run 目录读取（memory 持久化的 values_path），用于去重
    sota_values: dict[str, pl.DataFrame] = {}
    memory = load_memory(run_dir)
    for sota in memory.sota_factors:
        sota_path = run_dir / sota.values_path
        if sota_path.is_file():
            try:
                sota_values[sota.id] = pl.read_parquet(sota_path)
            except Exception:  # noqa: BLE001 - 单个 SOTA 值损坏只影响 dedup 精度
                continue
    from app.ai_mining.memory import is_duplicate_vs_sota

    duplicate_of = is_duplicate_vs_sota(values, sota_values)
    if duplicate_of == "all_nan":
        outcome.error = "因子值全为空"
        return outcome
    if duplicate_of is not None:
        outcome.duplicate_of = duplicate_of
        return outcome

    try:
        payload = evaluate_factor_values(
            values,
            start=start,
            end=end,
            price_loader=price_loader,
        )
    except EvalError as exc:
        outcome.error = exc.message
        return outcome
    outcome.payload = payload
    return outcome


async def generate_round_feedback(
    hypothesis: str,
    evaluation: RoundEvaluation,
    memory: EvolutionMemory,
    *,
    eval_days: int,
) -> dict:
    """LLM 生成轮次反馈 JSON（decision/observations/lessons），解析失败时降级为规则结论。"""
    system = FEEDBACK_SYSTEM_TEMPLATE.format(eval_days=eval_days)
    user = FEEDBACK_USER_TEMPLATE.format(
        hypothesis=hypothesis,
        metrics_summary=evaluation.metrics_summary(),
        sota_summary=sota_summary_for_prompt(memory),
        lessons_summary=lessons_summary_for_prompt(memory),
    )
    try:
        text = await call_llm(memory, [{"role": "system", "content": system}, {"role": "user", "content": user}])
        data = _extract_json_object(text)
        decision = str(data.get("decision", "reject")).strip().lower()
        return {
            "decision": decision if decision in {"accept", "reject"} else "reject",
            "observations": str(data.get("observations", ""))[:600],
            "lessons": [str(lesson)[:200] for lesson in (data.get("lessons") or []) if str(lesson).strip()][:3],
        }
    except Exception:  # noqa: BLE001 - 反馈是软依赖: LLM 挂了用规则结论兜底
        any_ok = any(outcome.ok for outcome in evaluation.outcomes)
        return {
            "decision": "accept" if any_ok else "reject",
            "observations": evaluation.metrics_summary(),
            "lessons": [],
        }


def _fmt(value: float | None) -> str:
    return "NA" if value is None else f"{value:.4f}"


def _fmt_pct(value: float | None) -> str:
    return "NA" if value is None else f"{value * 100:.1f}%"


# 兼容引用: FactorSandboxError 在本模块 re-export（loop 统一 except 用）
__all__ = ["FactorEvalOutcome", "FactorSandboxError", "RoundEvaluation"]
