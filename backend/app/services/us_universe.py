"""美股标的白名单 (us universe) 配置。

data/user_data/us_universe.json 结构:
    {"us": ["AAPL", "MSFT", ...]}

- 白名单仅约束 K 线同步范围; instruments_us 维表落全市场目录 (约 1.4 万只,
  供搜索/名称解析), 与 crypto (目录即白名单) 不同。
- 文件损坏时 fail-closed 回退默认并告警。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_US = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA",
    "AVGO", "JPM", "V", "UNH", "XOM", "MA", "LLY",
]


def universe_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "us_universe.json"


def default_universe() -> dict[str, list[str]]:
    return {"us": list(DEFAULT_US)}


def load_universe(data_dir: Path) -> list[str]:
    """读取白名单; 缺失生成默认, 损坏回退默认 (fail-closed)。"""
    path = universe_path(data_dir)
    if not path.exists():
        universe = default_universe()
        save_universe(data_dir, universe)
        return universe["us"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("美股白名单文件损坏, 回退默认: %s (%s)", path, e)
        return list(DEFAULT_US)
    items = raw.get("us")
    if items is None or not isinstance(items, list):
        return list(DEFAULT_US)
    out = [str(s).strip().upper() for s in items if str(s).strip()]
    return out or list(DEFAULT_US)


def save_universe(data_dir: Path, symbols: list[str]) -> list[str]:
    """规范化并写入白名单 (去重/大写)。"""
    seen: list[str] = []
    for item in symbols or []:
        text = str(item).strip().upper()
        if text and text not in seen:
            seen.append(text)
    path = universe_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"us": seen}, ensure_ascii=False, indent=2), encoding="utf-8")
    return seen
