"""加密货币标的白名单 (crypto universe) 配置。

data/user_data/crypto_universe.json 结构:
    {"spot": ["BTCUSDT", ...], "futures": ["BTCUSDT", ...]}

- 现货与合约分别维护; 合约写入时自动加 .P 内部后缀。
- 首次读取时生成默认主流币白名单; 文件损坏时 fail-closed 回退默认并告警。
- 只接受 USDT 计价交易对 (provider exchangeInfo 按 quoteAsset=USDT 过滤,
  白名单里的非 USDT 条目会被同步时静默跳过)。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_SPOT = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "TONUSDT"]
DEFAULT_FUTURES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                   "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "TONUSDT"]

PERP_SUFFIX = ".P"


def universe_path(data_dir: Path) -> Path:
    return Path(data_dir) / "user_data" / "crypto_universe.json"


def default_universe() -> dict[str, list[str]]:
    return {"spot": list(DEFAULT_SPOT), "futures": list(DEFAULT_FUTURES)}


def load_universe(data_dir: Path) -> dict[str, list[str]]:
    """读取白名单; 缺失生成默认, 损坏回退默认 (fail-closed)。"""
    path = universe_path(data_dir)
    if not path.exists():
        universe = default_universe()
        save_universe(data_dir, universe)
        return universe
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("crypto 白名单文件损坏, 回退默认: %s (%s)", path, e)
        return default_universe()
    out: dict[str, list[str]] = {}
    for market, defaults in (("spot", DEFAULT_SPOT), ("futures", DEFAULT_FUTURES)):
        items = raw.get(market)
        if items is None or not isinstance(items, list):
            items = defaults  # 键缺失/类型错误 → 默认; 显式空列表表示清空该市场
        out[market] = [
            str(s).strip().upper()
            for s in items
            if str(s).strip()
        ]
    return out


def save_universe(data_dir: Path, universe: dict[str, list[str]]) -> dict[str, list[str]]:
    """规范化并写入白名单 (去重/去 .P 后缀/大写)。"""
    clean: dict[str, list[str]] = {}
    for market in ("spot", "futures"):
        items = universe.get(market) if isinstance(universe, dict) else None
        seen: list[str] = []
        for item in items or []:
            text = str(item).strip().upper().removesuffix(PERP_SUFFIX)
            if text and text not in seen:
                seen.append(text)
        clean[market] = seen
    path = universe_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def internal_symbols(universe: dict[str, list[str]]) -> list[str]:
    """白名单 → 内部 symbol 列表 (合约加 .P 后缀, 与币安 provider 命名空间一致)。"""
    out = [f"{s}{PERP_SUFFIX}" for s in universe.get("futures", [])]
    out.extend(universe.get("spot", []))
    return out
