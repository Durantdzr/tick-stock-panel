"""LLM 调用网关: 预算硬上限的唯一计数点。"""
from __future__ import annotations

from app.ai_mining.memory import EvolutionMemory
from app.services.ai_provider import Message, generate_ai_text


class LlmBudgetExceededError(RuntimeError):
    """本轮 run 的 LLM 调用预算耗尽——显式失败，不静默降级。"""


async def call_llm(
    memory: EvolutionMemory,
    messages: list[Message],
    *,
    temperature: float = 0.3,
    timeout: float = 300.0,
) -> str:
    """计费封装: 超预算显式失败; 每次调用计入 memory.llm_calls（调用方负责落盘）。"""
    if not memory.can_call_llm(extra=1):
        raise LlmBudgetExceededError(
            f"LLM 调用预算已耗尽: {memory.llm_calls}/{memory.max_llm_calls}，本轮 run 终止"
        )
    memory.llm_calls += 1
    # max_tokens=None 是项目惯例: 不传该参数, 避免推理模型思考挤占正文截断 JSON
    return await generate_ai_text(messages, temperature=temperature, max_tokens=None, timeout=timeout)
