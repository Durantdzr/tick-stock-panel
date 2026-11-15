"""加密货币概念板块映射 (DeFi/MEME/L1 等板块等价物) 配置。

data/user_data/crypto_concepts.json 结构 (正向映射, 概念 → 成员):
    {"DeFi": ["ETHUSDT", "LINKUSDT"], "MEME": ["DOGEUSDT", ...], ...}

- 概念名自由命名 (如 DeFi/MEME/L1/预言机/数字黄金), 成员为白名单内 symbol。
- 缺省生成默认映射 (覆盖默认白名单 10 标的); 损坏 fail-closed 回退默认。
- 白名单之外的成员在聚合时自然落空 (不报错), 由看板概念行 count 体现。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认概念映射 (覆盖 crypto_universe 默认白名单 10 标的; 可经 API 修改)
DEFAULT_CONCEPTS: dict[str, list[str]] = {
    "L1": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "ADAUSDT", "AVAXUSDT", "TONUSDT"],
    "DeFi": ["ETHUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT"],
    "MEME": ["DOGEUSDT", "SOLUSDT"],
    "支付结算": ["XRPUSDT", "DOGEUSDT", "TONUSDT"],
    "预言机": ["LINKUSDT"],
    "数字黄金": ["BTCUSDT"],
    "CEX 平台币": ["BNBUSDT"],
}


def concepts_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "crypto_concepts.json"


def default_concepts() -> dict[str, list[str]]:
    return {name: list(members) for name, members in DEFAULT_CONCEPTS.items()}


def _clean(concepts: dict[str, list[str]]) -> dict[str, list[str]]:
    """清洗: 剔除空概念/空成员, 成员去重 + 大写, 保留概念插入顺序。"""
    out: dict[str, list[str]] = {}
    for name, members in (concepts or {}).items():
        label = str(name).strip()
        if not label:
            continue
        seen: list[str] = []
        for item in members or []:
            text = str(item).strip().upper()
            if text and text not in seen:
                seen.append(text)
        if seen:
            out[label] = seen
    return out


def load_concepts(data_dir: Path) -> dict[str, list[str]]:
    """读取概念映射; 缺失生成默认, 损坏/清洗后为空回退默认 (fail-closed)。"""
    path = concepts_path(data_dir)
    if not path.exists():
        concepts = default_concepts()
        save_concepts(data_dir, concepts)
        return concepts
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("crypto 概念映射文件损坏, 回退默认: %s (%s)", path, e)
        return default_concepts()
    cleaned = _clean(raw if isinstance(raw, dict) else {})
    if not cleaned:
        return default_concepts()
    return cleaned


def save_concepts(data_dir: Path, concepts: dict[str, list[str]]) -> dict[str, list[str]]:
    """清洗并写入概念映射。"""
    cleaned = _clean(concepts)
    path = concepts_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    return cleaned


def concept_of_symbol(concepts: dict[str, list[str]]) -> dict[str, list[str]]:
    """正向映射 → symbol → 命中的概念列表 (聚合/展示用)。"""
    out: dict[str, list[str]] = {}
    for name, members in concepts.items():
        for symbol in members:
            out.setdefault(symbol, []).append(name)
    return out
