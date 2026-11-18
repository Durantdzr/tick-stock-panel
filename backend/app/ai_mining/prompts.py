"""ai_mining 提示词资产：指南加载 + proposal/coder/feedback 模板。

惯例对齐 strategy/ai_generator（md 指南 + 内联 system 前缀）与
strategy/custom_signals_ai（JSON 硬约束 + .format 占位）。
"""
from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def load_evolution_guide() -> str:
    """读取因子进化指南（模块级缓存由调用方决定是否再做）。"""
    return (_PROMPTS_DIR / "factor-evolution.md").read_text(encoding="utf-8")


# ── 第一步: 假设提出 ────────────────────────────────────────────────────

PROPOSAL_SYSTEM_TEMPLATE = """你是一名严谨的量化因子研究员，为 A 股日线数据提出新的选股因子假设。

{guide}

## 输出要求

只输出一个 JSON 对象，禁止 markdown 代码块、解释文字或多余字段。结构：

{{
  "hypothesis": "本轮核心假设，两三句话，说明为什么这些因子可能有超额收益",
  "reason": "假设依据（经济学逻辑/市场现象/上轮反馈），两三句话",
  "factors": [
    {{
      "name": "大写字母开头的因子名，仅大写字母数字下划线，3-30 字符",
      "description": "因子含义，一句话",
      "formulation": "计算公式或伪代码，一句话",
      "direction": "positive 或 negative。positive=值越大越看多；若因子天然反向请同时把计算取负、direction 填 positive"
    }}
  ]
}}

最多 {factors_limit} 个因子。与 SOTA 清单高度相似的因子不要提。"""

PROPOSAL_USER_TEMPLATE = """## 已有 SOTA 因子（勿提高度相似假设）

{sota_summary}

## 历史轮次与反馈

{memory_summary}

## 本轮任务

提出最多 {factors_limit} 个新因子假设。若上轮被拒绝，先消化 observations/lessons 再提新方向。"""


# ── 第二步: 因子编码 ────────────────────────────────────────────────────

CODER_SYSTEM_TEMPLATE = """你是量化因子实现工程师，把因子规格翻译成可执行的 pandas 脚本。

{guide}

## 输出要求

只输出一个 fenced python 代码块（```python ... ```），代码必须：
- 定义 `calculate_{factor_name}()` 并在 `if __name__ == "__main__":` 中调用
- 读 `panel.parquet`，把因子值写为 `factor_values.parquet`（symbol,date,value 三列长表）
- 输出列 date 用 `datetime.date` 类型（或可被 pandas 解析为日期的 datetime64）
- 不要 try/except；不要读写其它文件；不要请求网络；不要 print 大段内容"""

CODER_USER_TEMPLATE = """## 因子规格

- 名称: {factor_name}
- 含义: {description}
- 公式: {formulation}
- 方向: {direction}（{direction_note}）

## 本次任务

实现该因子脚本。"""

CODER_REPAIR_USER_TEMPLATE = """## 你上一次的实现没有通过校验

### 校验反馈

{feedback}

### 上一次代码

```python
{previous_code}
```

## 本次任务

输出修复后的**完整**脚本（不是补丁），同样只输出一个 fenced python 代码块。保持因子逻辑不变，只修复反馈指出的问题。"""


# ── 第三步: 反馈生成 ────────────────────────────────────────────────────

FEEDBACK_SYSTEM_TEMPLATE = """你是量化研究主管，根据本轮因子的真实回测指标评估假设，并给出下一轮方向建议。

评估口径备忘：IC/IC_IR 为日截面 Rank IC（主要信号强度指标）；Q5 年化收益/回撤为绝对口径（含市场 beta，解读时看多空与 IC 为主）；多空为 Q5-Q1 理论价差诊断。评估窗口约 {eval_days} 个交易日，样本有限，IC 绝对值小属正常。

## 输出要求

只输出一个 JSON 对象：

{{
  "decision": "accept 或 reject。accept=本轮假设值得延续（至少一个因子有信号或方向可迭代）；reject=方向不对需换",
  "observations": "对指标的解读，两三句话（哪些因子强、强弱与假设是否一致）",
  "lessons": ["可执行的教训，1-3 条，供下一轮假设参考"]
}}"""

FEEDBACK_USER_TEMPLATE = """## 本轮假设

{hypothesis}

## 因子指标（真实回测）

{metrics_summary}

## 当前 SOTA（本轮之前的最优记录）

{sota_summary}

## 历史教训（延续）

{lessons_summary}

## 本次任务

给出本轮评估结论。"""


def direction_note(direction: str) -> str:
    return "值越大越看多（计算已含方向，无需再取负）" if direction == "positive" else "值越大越看空（请在计算中取负，使 Q5 为看多方向）"
