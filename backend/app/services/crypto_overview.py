"""crypto 市场总览装配 — 与 A 股 build_market_overview 平行的轻量版。

数据优先级与口径 (全部显式声明, 见 docs/crypto-market.md):
  - 涨跌/成交额/榜单: 币安 24hr 快照 (全 USDT 现货对, 24 小时滚动窗口口径,
    非 A 股日历日口径); 快照不可达时 fail-soft 降级到本地 enriched 最新日
    (白名单标的, 日历日口径), 两者都无则空态。响应带 data_source /
    data_window 供前端提示。
  - 趋势(站上均线/新高新低)/量比: 只能来自本地 enriched (快照无历史),
    始终反映已同步白名单范围, 未同步时为零值。
  - 涨跌停/换手率/板块/概念: crypto 无此口径, 恒为零/空 (交易规则注册表
    trading_rules 声明 limit_ladder/sector_heat 未开放)。

只读 USDT 现货对: 合约 (.P) 与现货同名同波动, 混入榜单会造成重复计数;
合约维度 (资金费率) 由 /api/info/sentiment 提供。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from app.services.market_overview_builder import _finite, _json_safe, _pct_band_rows
from app.services.trading_rules import get_trading_rules

logger = logging.getLogger(__name__)

# 总览头部固定展示的主流币 (对齐 index_const 的"产品级固定契约"模式:
# 单一权威在本模块, 后端装配与前端引用同一份清单不建副本)
CRYPTO_CORE_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT")
_CRYPTO_CORE_NAMES = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "BNBUSDT": "BNB",
    "SOLUSDT": "SOL", "XRPUSDT": "XRP",
}

_EMPTY_TREND = {"above_ma5": 0, "above_ma20": 0, "above_ma60": 0,
                "above_ma5_pct": 0, "above_ma20_pct": 0, "above_ma60_pct": 0,
                "new_high": 0, "new_low": 0}
_EMPTY_ACTIVITY = {"avg_turnover": 0, "high_turnover": 0, "high_vol_ratio": 0, "vol_ratio": 1}
_EMPTY_RANK = {"leading": [], "lagging": []}


def _top_rows(rows: list[dict], key: str, descending: bool, limit: int = 8) -> list[dict]:
    """榜单行 (与 A 股 _top_rows 同形, board 用快照自带标签而非 A 股板块逻辑)。"""
    filtered = [r for r in rows if _finite(r.get(key)) is not None]
    filtered.sort(key=lambda r: _finite(r.get(key)) or 0, reverse=descending)
    return [
        {
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "close": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "turnover_rate": None,
            "board": r.get("board") or "现货",
        }
        for r in filtered[:limit]
    ]


def _records_to_rows(records: list[dict]) -> list[dict]:
    """realtime records → 榜单行; 排除合约 (.P, 与现货重复计数)。"""
    rows = []
    for r in records:
        symbol = str(r.get("symbol") or "").strip()
        if not symbol or symbol.endswith(".P"):
            continue
        rows.append({
            "symbol": symbol,
            "name": r.get("name") or symbol,
            "close": _finite(r.get("last_price")),
            "change_pct": _finite(r.get("change_pct")),
            "amount": _finite(r.get("amount")),
            "board": "现货",
        })
    return rows


def _snapshot_rows() -> tuple[list[dict], bool]:
    """24hr 快照 → 榜单行 (symbol/name/close/change_pct/amount/board)。"""
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    name = preferences.get_crypto_data_provider()
    provider = custom_sources.get_provider(name)
    if provider is None:
        return [], False
    try:
        records = provider.get_realtime()
    except Exception as e:  # noqa: BLE001 — 快照失败走本地降级, 不打断总览
        logger.warning("crypto 24hr 快照拉取失败 (%s): %s", name, e)
        return [], False
    rows = _records_to_rows(records)
    return rows, bool(rows)


def _local_enriched(repo) -> tuple[list[dict], str | None]:
    """本地 enriched 最新日 → (榜单行, 日期); 降级数据源, 日历日口径。"""
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, asset_type="crypto")
    latest = svc.latest_date()
    if latest is None:
        return [], None
    try:
        df = svc._load_enriched_for_date(latest)
    except Exception as e:  # noqa: BLE001 — 完整性断言失败等按无数据降级
        logger.warning("crypto enriched 最新日读取失败 (%s): %s", latest, e)
        return [], None
    if df.is_empty():
        return [], None
    cols = ["symbol", "name", "close", "change_pct", "amount"]
    rows = df.select([c for c in cols if c in df.columns]).to_dicts()
    for r in rows:
        r.setdefault("name", r.get("symbol"))
        r["turnover_rate"] = None
        r["board"] = "现货"
    return rows, str(latest)


def _trend_and_activity(repo) -> tuple[dict, dict]:
    """站上均线/新高新低 + 放量占比 — 仅本地 enriched 白名单范围。"""
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, asset_type="crypto")
    latest = svc.latest_date()
    if latest is None:
        return dict(_EMPTY_TREND), dict(_EMPTY_ACTIVITY)
    try:
        df = svc._load_enriched_for_date(latest)
    except Exception as e:  # noqa: BLE001
        logger.warning("crypto 趋势指标读取失败 (%s): %s", latest, e)
        return dict(_EMPTY_TREND), dict(_EMPTY_ACTIVITY)
    if df.is_empty():
        return dict(_EMPTY_TREND), dict(_EMPTY_ACTIVITY)
    rows = df.to_dicts()
    total = len(rows) or 1

    def above(ma_key: str) -> int:
        return sum(
            1 for r in rows
            if _finite(r.get("close")) is not None and _finite(r.get(ma_key)) is not None
            and (_finite(r.get("close")) or 0) >= (_finite(r.get(ma_key)) or 0)
        )

    new_high = sum(
        1 for r in rows
        if _finite(r.get("high_60d")) is not None
        and (_finite(r.get("close")) or 0) >= (_finite(r.get("high_60d")) or 0)
    )
    new_low = sum(
        1 for r in rows
        if _finite(r.get("low_60d")) is not None
        and (_finite(r.get("close")) or 0) <= (_finite(r.get("low_60d")) or 0)
    )
    vol_ratios = [v for v in (_finite(r.get("vol_ratio_5d")) for r in rows) if v is not None]
    trend = {
        "above_ma5": above("ma5"),
        "above_ma20": above("ma20"),
        "above_ma60": above("ma60"),
        "above_ma5_pct": above("ma5") / total * 100,
        "above_ma20_pct": above("ma20") / total * 100,
        "above_ma60_pct": above("ma60") / total * 100,
        "new_high": new_high,
        "new_low": new_low,
    }
    # crypto 无流通股本, 换手率无口径; 量比沿用 5 日均量比
    activity = {
        "avg_turnover": 0,
        "high_turnover": 0,
        "high_vol_ratio": sum(1 for v in vol_ratios if v >= 1.5) / total * 100,
        "vol_ratio": sum(vol_ratios) / len(vol_ratios) if vol_ratios else 1,
    }
    return trend, activity


def _index_quotes(snapshot_rows: list[dict], local_rows: list[dict]) -> list[dict]:
    """主流币 mini 卡 — 快照优先, 快照不可达时回退本地 enriched 收盘价。"""
    by_symbol = {r.get("symbol"): r for r in snapshot_rows}
    by_symbol_local = {r.get("symbol"): r for r in local_rows}
    out = []
    for symbol in CRYPTO_CORE_SYMBOLS:
        r = by_symbol.get(symbol) or by_symbol_local.get(symbol) or {}
        out.append({
            "symbol": symbol,
            "name": _CRYPTO_CORE_NAMES.get(symbol, symbol),
            "last_price": _finite(r.get("close")),
            "change_pct": _finite(r.get("change_pct")),
            "change_amount": None,
        })
    return out


def _concept_rank(rows: list[dict], data_dir) -> dict:
    """本地概念映射聚合 → concept_rank (DeFi/MEME/L1 等板块等价物)。

    rows 为总览行情行 (snapshot 优先, 降级 enriched 最新日), 含
    symbol/name/change_pct/amount。与 A 股 OverviewDimensionRankItem 同构:
    按概念聚合成员 change_pct 均值, leading/lagging 各取前 5;
    source_field=None → 行为静态展示 (不可点开成分弹窗, crypto 无 ext 源)。
    多概念成员在每个概念内各计一次 (与 A 股多概念归属同口径)。
    """
    from app.services import crypto_concepts

    mapping = crypto_concepts.concept_of_symbol(
        crypto_concepts.load_concepts(Path(data_dir)))
    if not mapping or not rows:
        return dict(_EMPTY_RANK)

    by_symbol = {r.get("symbol"): r for r in rows}
    buckets: dict[str, list[dict]] = {}
    for symbol, concepts in mapping.items():
        row = by_symbol.get(symbol)
        if row is None:
            continue  # 白名单外/未同步成员自然落空
        for name in concepts:
            buckets.setdefault(name, []).append(row)

    items: list[dict] = []
    for name, members in buckets.items():
        pcts = [v for v in (_finite(m.get("change_pct")) for m in members) if v is not None]
        if not pcts:
            continue
        leader = max(members, key=lambda m: _finite(m.get("change_pct")) or 0)
        items.append({
            "name": name,
            "count": len(members),
            "avg_pct": sum(pcts) / len(pcts),
            "up_count": sum(1 for v in pcts if v > 0),
            "down_count": sum(1 for v in pcts if v < 0),
            "amount": sum(_finite(m.get("amount")) or 0 for m in members),
            "source_field": None,
            "leader": {
                "symbol": leader.get("symbol"),
                "name": leader.get("name"),
                "change_pct": _finite(leader.get("change_pct")),
            },
        })
    by_avg_desc = sorted(items, key=lambda x: x["avg_pct"], reverse=True)
    by_avg_asc = sorted(items, key=lambda x: x["avg_pct"])
    # 与 A 股 _dimension_rank 同口径: leading = 降序前 5, lagging = 升序前 5
    # (总概念数 < 10 时两者允许重叠, 前端两列各取前 5 展示)
    return {"leading": by_avg_desc[:5], "lagging": by_avg_asc[:5]}


def build_crypto_overview(repo, as_of: date | None = None) -> dict:
    """装配 crypto 市场总览 — 顶层键与 A 股 OverviewMarket 对齐, 便于前端复用。

    as_of 参数与 A 股历史复盘语义不同: 快照只有"当前", 传入值仅影响本地
    enriched 降级数据的日期归属; 显式历史复盘对 crypto 暂不提供。
    """
    rules = get_trading_rules("crypto")
    rows, snapshot_ok = _snapshot_rows()
    local_rows, local_as_of = _local_enriched(repo)
    if snapshot_ok:
        data_source = "binance_snapshot"
        data_window = rules["change_pct_window"]
    else:
        rows = local_rows
        data_source = "local_enriched" if rows else "none"
        data_window = "trading_day" if rows else rules["change_pct_window"]

    total = len(rows)
    up = sum(1 for r in rows if (_finite(r.get("change_pct")) or 0) > 0)
    down = sum(1 for r in rows if (_finite(r.get("change_pct")) or 0) < 0)
    flat = max(0, total - up - down)
    amounts = [_finite(r.get("amount")) or 0 for r in rows]
    total_amount = sum(amounts)
    pct_values = [v for v in (_finite(r.get("change_pct")) for r in rows) if v is not None]

    trend, activity = _trend_and_activity(repo)
    repo_data_dir = repo.store.data_dir

    return _json_safe({
        "market": "crypto",
        "data_source": data_source,
        "data_window": data_window,
        "scope": "spot_usdt",
        "as_of": local_as_of,
        "quote_status": {
            "enabled": False,
            "running": False,
            "quote_age_ms": None,
            "is_trading_hours": True,  # 7×24 无收盘概念
        },
        "indices": _index_quotes(rows if snapshot_ok else [], local_rows),
        "breadth": {
            "total": total,
            "up": up,
            "down": down,
            "flat": flat,
            "up_pct": up / total * 100 if total else 0,
            "down_pct": down / total * 100 if total else 0,
            "avg_pct": sum(pct_values) / len(pct_values) if pct_values else 0,
            "median_pct": sorted(pct_values)[len(pct_values) // 2] if pct_values else 0,
            "strong_up": sum(1 for v in pct_values if v >= 0.03),
            "strong_down": sum(1 for v in pct_values if v <= -0.03),
        },
        "amount": {"total": total_amount, "avg": total_amount / total if total else 0},
        "boards": [],
        "limit": {"limit_up": 0, "broken": 0, "failed": 0, "limit_down": 0,
                  "max_boards": 0, "seal_rate": 0, "tiers": []},
        "distribution": _pct_band_rows(pct_values),
        "trend": trend,
        "activity": activity,
        "radar": None,
        "emotion": None,
        "top_gainers": _top_rows(rows, "change_pct", True),
        "top_losers": _top_rows(rows, "change_pct", False),
        "turnover_leaders": _top_rows(rows, "amount", True),
        "active_leaders": [],  # 无换手率口径
        "concept_rank": _concept_rank(rows, repo_data_dir),
        "industry_rank": dict(_EMPTY_RANK),
    })
