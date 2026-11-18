"""因子编码: 规格 → LLM 代码 → AST 校验（错误回喂修复 ≤max_repairs） → 沙箱执行。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.ai_mining.llm import call_llm
from app.ai_mining.memory import EvolutionMemory
from app.ai_mining.prompts import (
    CODER_REPAIR_USER_TEMPLATE,
    CODER_SYSTEM_TEMPLATE,
    CODER_USER_TEMPLATE,
    direction_note,
    load_evolution_guide,
)
from app.ai_mining.proposal import FactorProposal
from app.ai_mining.sandbox import (
    FACTOR_CODE_FILE,
    FactorSandboxError,
    load_factor_values,
    run_factor_script,
    validate_factor_code,
)
from app.strategy.ai_generator import AIStrategyGenerator


@dataclass
class FactorImplementation:
    proposal: FactorProposal
    code: str
    workdir: Path
    values_path: Path
    repair_history: list[str] = field(default_factory=list)


async def implement_factor(
    proposal: FactorProposal,
    workdir: Path,
    memory: EvolutionMemory,
    *,
    max_repairs: int = 3,
    exec_timeout: float = 120.0,
    guide: str | None = None,
) -> FactorImplementation:
    """实现单个因子并沙箱执行，返回实现产物；失败抛 FactorSandboxError（含可回喂摘要）。

    调用方保证 workdir 内已有 panel.parquet（panel 供给由 loop 统一做一次）。
    """
    guide_text = guide or load_evolution_guide()
    system = CODER_SYSTEM_TEMPLATE.format(guide=guide_text, factor_name=proposal.name)
    user = CODER_USER_TEMPLATE.format(
        factor_name=proposal.name,
        description=proposal.description,
        formulation=proposal.formulation,
        direction=proposal.direction,
        direction_note=direction_note(proposal.direction),
    )

    code: str | None = None
    repair_history: list[str] = []
    for attempt in range(1 + max_repairs):
        text = await call_llm(memory, [{"role": "system", "content": system}, {"role": "user", "content": user}])
        candidate = AIStrategyGenerator._extract_code_block(text)
        try:
            validate_factor_code(candidate)
            code = candidate
            break
        except FactorSandboxError as validation_error:
            repair_history.append(f"attempt{attempt + 1}: {validation_error.message[:500]}")
            user = CODER_REPAIR_USER_TEMPLATE.format(
                feedback=validation_error.message,
                previous_code=candidate[:8000],
            )
    if code is None:
        raise FactorSandboxError(
            "codegen_failed",
            f"连续 {1 + max_repairs} 次未通过校验: {'; '.join(repair_history[-2:])}",
        )

    (Path(workdir) / FACTOR_CODE_FILE).write_text(code, encoding="utf-8")
    result = run_factor_script(Path(workdir), timeout=exec_timeout)
    if not result.ok:
        raise FactorSandboxError("execution", result.error_feedback())

    values_path = Path(workdir) / "factor_values.parquet"
    load_factor_values(values_path)  # 输出契约校验（值由 evaluator 按 needs 重新读取）
    return FactorImplementation(
        proposal=proposal,
        code=code,
        workdir=Path(workdir),
        values_path=values_path,
        repair_history=repair_history,
    )
