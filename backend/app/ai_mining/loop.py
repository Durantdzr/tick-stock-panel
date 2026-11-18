"""五步进化循环编排: propose → implement → evaluate → feedback → record。

单轮失败不中断整轮 run（记录后继续下一轮）；预算耗尽优雅终止；
取消检查点在轮与因子边界。产物持久化约定见 memory.py 与 runtime.py。
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.ai_mining.coder import implement_factor
from app.ai_mining.evaluator import (
    FactorEvalOutcome,
    RoundEvaluation,
    evaluate_factor_outcome,
    generate_round_feedback,
)
from app.ai_mining.llm import LlmBudgetExceededError
from app.ai_mining.memory import (
    EvolutionMemory,
    HypothesisRecord,
    SotaFactor,
    load_memory,
    save_memory,
)
from app.ai_mining.prompts import load_evolution_guide
from app.ai_mining.proposal import propose
from app.ai_mining.sandbox import PANEL_DATA_FILE


@dataclass
class LoopConfig:
    run_dir: Path
    eval_start: date
    eval_end: date
    rounds: int = 3
    factors_per_round: int = 2
    max_repairs: int = 3
    exec_timeout: float = 180.0
    max_llm_calls: int = 120
    eval_days_hint: int = 90

    def normalized(self) -> LoopConfig:
        self.rounds = max(1, min(int(self.rounds), 10))
        self.factors_per_round = max(1, min(int(self.factors_per_round), 5))
        self.max_repairs = max(0, min(int(self.max_repairs), 5))
        self.max_llm_calls = max(1, min(int(self.max_llm_calls), 400))
        return self


@dataclass
class LoopSummary:
    run_dir: Path
    rounds: list[dict] = field(default_factory=list)
    sota_count: int = 0
    llm_calls: int = 0
    stopped_reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "sota_count": self.sota_count,
            "llm_calls": self.llm_calls,
            "stopped_reason": self.stopped_reason,
        }


def _progress(cb: Callable[[dict], None] | None, *, phase: str, label: str, done: int, total: int) -> None:
    if cb is not None:
        cb({"phase": phase, "label": label, "done": done, "total": total})


def _is_cancelled(cancel_check: Any) -> bool:
    if cancel_check is None:
        return False
    if hasattr(cancel_check, "is_set"):
        return bool(cancel_check.is_set())
    return bool(cancel_check())


def _link_panel(run_dir: Path, workdir: Path) -> None:
    """因子工作目录共享 run 级面板（符号链接失败时退回复制）。"""
    link = workdir / PANEL_DATA_FILE
    source = run_dir / PANEL_DATA_FILE
    if link.exists() or link.is_symlink():
        return
    try:
        link.symlink_to(source)
    except OSError:
        import shutil

        shutil.copy2(source, link)


async def run_evolution_loop(
    config: LoopConfig,
    *,
    price_loader,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_check: Any = None,
) -> LoopSummary:
    """执行进化循环。调用方保证 run_dir/panel.parquet 已就绪。"""
    config.normalized()
    run_dir = Path(config.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    memory = load_memory(run_dir)
    memory.max_llm_calls = config.max_llm_calls
    guide = load_evolution_guide()
    summary = LoopSummary(run_dir=run_dir)

    for round_no in range(1, config.rounds + 1):
        if _is_cancelled(cancel_check):
            summary.stopped_reason = "cancelled"
            break
        round_record = await _run_single_round(
            config=config,
            round_no=round_no,
            memory=memory,
            guide=guide,
            price_loader=price_loader,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            summary=summary,
        )
        summary.rounds.append(round_record)
        save_memory(config.run_dir, memory)
        if round_record.get("budget_exhausted"):
            summary.stopped_reason = "budget_exhausted"
            break

    summary.sota_count = len(memory.sota_factors)
    summary.llm_calls = memory.llm_calls
    save_memory(run_dir, memory)
    return summary


async def _run_single_round(
    *,
    config: LoopConfig,
    round_no: int,
    memory: EvolutionMemory,
    guide: str,
    price_loader,
    progress_cb: Callable[[dict], None] | None,
    cancel_check: Any,
    summary: LoopSummary,
) -> dict[str, Any]:
    """单轮五步；返回该轮记录（含 error/budget_exhausted 标记）。"""
    record: dict[str, Any] = {"round": round_no, "decision": "reject", "error": None}
    _progress(progress_cb, phase="propose", label=f"第{round_no}轮 假设生成", done=round_no - 1, total=config.rounds)
    try:
        proposal = await propose(memory, factors_limit=config.factors_per_round, guide=guide)
    except LlmBudgetExceededError:
        record["budget_exhausted"] = True
        record["error"] = "LLM 预算耗尽，无法生成假设"
        return record

    outcomes: list[FactorEvalOutcome] = []
    impls_by_name: dict[str, Path] = {}
    for index, factor in enumerate(proposal.factors):
        if _is_cancelled(cancel_check):
            summary.stopped_reason = "cancelled"
            break
        _progress(
            progress_cb,
            phase="implement",
            label=f"第{round_no}轮 实现 {factor.name} ({index + 1}/{len(proposal.factors)})",
            done=round_no - 1,
            total=config.rounds,
        )
        workdir = config.run_dir / "rounds" / str(round_no) / factor.name
        workdir.mkdir(parents=True, exist_ok=True)
        _link_panel(config.run_dir, workdir)
        try:
            impl = await implement_factor(
                factor,
                workdir,
                memory,
                max_repairs=config.max_repairs,
                exec_timeout=config.exec_timeout,
                guide=guide,
            )
            impls_by_name[factor.name] = impl.values_path
        except Exception as exc:  # noqa: BLE001 - 单因子失败不中断本轮其它因子
            outcomes.append(
                FactorEvalOutcome(
                    proposal=factor,
                    error=f"实现失败: {str(exc)[:300]}",
                )
            )

    _progress(progress_cb, phase="evaluate", label=f"第{round_no}轮 面板评估", done=round_no - 1, total=config.rounds)
    for factor in proposal.factors:
        values_path = impls_by_name.get(factor.name)
        if values_path is None:
            continue  # 实现已失败并记录
        outcome = evaluate_factor_outcome(
            factor,
            values_path,
            start=config.eval_start,
            end=config.eval_end,
            price_loader=price_loader,
            run_dir=config.run_dir,
        )
        outcomes.append(outcome)

    if _is_cancelled(cancel_check):
        summary.stopped_reason = "cancelled"
        record["error"] = "cancelled"
        return record

    evaluation = RoundEvaluation(outcomes=outcomes)
    _progress(progress_cb, phase="feedback", label=f"第{round_no}轮 反馈生成", done=round_no - 1, total=config.rounds)
    try:
        feedback = await generate_round_feedback(
            proposal.hypothesis,
            evaluation,
            memory,
            eval_days=config.eval_days_hint,
        )
    except LlmBudgetExceededError:
        record["budget_exhausted"] = True
        feedback = evaluation.feedback or {"decision": "reject", "observations": "", "lessons": []}
    record["decision"] = feedback.get("decision", "reject")
    record["hypothesis"] = proposal.hypothesis
    record["reason"] = proposal.reason
    record["factors"] = [
        {"name": f.name, "description": f.description, "formulation": f.formulation, "direction": f.direction}
        for f in proposal.factors
    ]
    record["metrics_summary"] = evaluation.metrics_summary()
    record["observations"] = evaluation.observations

    # record 步: 记忆演进（教训全局追加、SOTA 链只收 ok 因子）
    for lesson in evaluation.lessons:
        if lesson not in memory.lessons:
            memory.lessons.append(lesson)
    ok_count = 0
    for outcome in outcomes:
        if not outcome.ok:
            continue
        ok_count += 1
        factor_impl_path = impls_by_name.get(outcome.proposal.name)
        values_rel = str(Path(factor_impl_path).relative_to(config.run_dir)) if factor_impl_path else ""
        memory.sota_factors.append(
            SotaFactor(
                id=outcome.proposal.name,
                name=outcome.proposal.name,
                description=outcome.proposal.description,
                formulation=outcome.proposal.formulation,
                round=round_no,
                ic=outcome.ic,
                ic_ir=outcome.ic_ir,
                values_path=values_rel,
            )
        )
    record["factors_ok"] = ok_count
    record["factors_total"] = len(proposal.factors)
    memory.rounds.append(
        HypothesisRecord(
            round=round_no,
            hypothesis=proposal.hypothesis,
            reason=proposal.reason,
            factors=record["factors"],
            metrics_summary=evaluation.metrics_summary(),
            decision=record["decision"],
            observations=evaluation.observations,
            lessons=evaluation.lessons,
        )
    )
    record["llm_calls"] = memory.llm_calls
    _progress(progress_cb, phase="record", label=f"第{round_no}轮 记录完成", done=round_no, total=config.rounds)
    return record


def run_loop_sync(config: LoopConfig, *, price_loader, progress_cb=None, cancel_check=None) -> dict[str, Any]:
    """同步入口（worker 子进程/测试用）。"""
    return asyncio.run(run_evolution_loop(config, price_loader=price_loader, progress_cb=progress_cb, cancel_check=cancel_check)).to_dict()
