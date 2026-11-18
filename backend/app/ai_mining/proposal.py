"""假设提出: 进化记忆 + SOTA 摘要 → LLM → 结构化因子提案。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai_mining.llm import call_llm
from app.ai_mining.memory import (
    EvolutionMemory,
    memory_summary_for_prompt,
    sota_summary_for_prompt,
)
from app.ai_mining.prompts import (
    PROPOSAL_SYSTEM_TEMPLATE,
    PROPOSAL_USER_TEMPLATE,
    load_evolution_guide,
)
from app.strategy.custom_signals_ai import _extract_json_object

_FACTOR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,29}$")


class ProposalError(ValueError):
    """提案解析/校验失败（LLM 输出不合规且无法 salvage）。"""


@dataclass
class FactorProposal:
    name: str
    description: str
    formulation: str
    direction: str  # positive / negative（记录语义；编码时按指南已含方向）


@dataclass
class Proposal:
    hypothesis: str
    reason: str
    factors: list[FactorProposal]
    skipped: list[str]  # 被白名单拒绝的原始提案摘要（可审计）


async def propose(
    memory: EvolutionMemory,
    *,
    factors_limit: int,
    guide: str | None = None,
) -> Proposal:
    """生成一轮因子提案。身份字段走白名单校验，不信任 LLM 输出的多余字段。"""
    guide_text = guide or load_evolution_guide()
    system = PROPOSAL_SYSTEM_TEMPLATE.format(guide=guide_text, factors_limit=factors_limit)
    user = PROPOSAL_USER_TEMPLATE.format(
        sota_summary=sota_summary_for_prompt(memory),
        memory_summary=memory_summary_for_prompt(memory),
        factors_limit=factors_limit,
    )
    text = await call_llm(memory, [{"role": "system", "content": system}, {"role": "user", "content": user}])
    data = _extract_json_object(text)

    hypothesis = str(data.get("hypothesis", "")).strip()
    reason = str(data.get("reason", "")).strip()
    if not hypothesis or not reason:
        raise ProposalError(f"提案缺少 hypothesis/reason: keys={sorted(data.keys())}")
    raw_factors = data.get("factors")
    if not isinstance(raw_factors, list) or not raw_factors:
        raise ProposalError("提案 factors 为空或缺失")

    factors: list[FactorProposal] = []
    skipped: list[str] = []
    seen_names: set[str] = set()
    for item in raw_factors[: factors_limit * 2]:  # 上限内尽力 salvage
        if not isinstance(item, dict):
            skipped.append(f"非对象条目: {str(item)[:80]}")
            continue
        name = str(item.get("name", "")).strip().upper()
        description = str(item.get("description", "")).strip()[:300]
        formulation = str(item.get("formulation", "")).strip()[:500]
        direction = str(item.get("direction", "positive")).strip().lower()
        if not _FACTOR_NAME_RE.match(name):
            skipped.append(f"非法名称 '{name[:40]}'")
            continue
        if name in seen_names:
            skipped.append(f"重复名称 '{name}'")
            continue
        if not description or not formulation:
            skipped.append(f"'{name}' 缺 description/formulation")
            continue
        if direction not in {"positive", "negative"}:
            direction = "positive"
        seen_names.add(name)
        factors.append(FactorProposal(name=name, description=description, formulation=formulation, direction=direction))
        if len(factors) >= factors_limit:
            break

    if not factors:
        raise ProposalError(f"提案无有效因子: skipped={skipped}")
    return Proposal(hypothesis=hypothesis, reason=reason, factors=factors, skipped=skipped)
