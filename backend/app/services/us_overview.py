"""美股市场总览装配 — 顶层键与 A 股 OverviewMarket 对齐, 前端看板 us 市场复用。

数据范围 (如实标注): 覆盖 = instruments_us 维表 + 白名单 K 线同步范围;
breadth/榜单基于白名单标的的 enriched 最新日截面 (非全市场 1.4 万只)。
指数 (道指/纳指/标普) 为东财美股指数公开接口单点快照。

口径: change_pct 小数制; volume=股, amount=美元; 无涨跌停/连板/换手率
(流通股本缺失, turnover 类列恒空), limit/boards/radar/emotion 置空。
"""

from __future__ import annotations

import logging
from datetime import date

from app.services.trading_rules import get_trading_rules

logger = logging.getLogger(__name__)

# 复用 crypto_overview 的通用装配工具 (结构同构, 避免平行实现)
from app.services.crypto_overview import (  # noqa: E402
    _EMPTY_ACTIVITY,
    _EMPTY_RANK,
    _EMPTY_TREND,
    _finite,
    _json_safe,
    _pct_band_rows,
    _top_rows,
)

_US_INDEX_SECIDS = (
    ("100.DJIA", "DJIA", "道琼斯"),
    ("100.NDX", "NDX", "纳斯达克"),
    ("100.SPX", "SPX", "标普500"),
)
_INDEX_CACHE: tuple[float, list[dict]] | None = None
_INDEX_CACHE_TTL_S = 60.0


def _us_index_quotes() -> list[dict]:
    """美股三大指数单点快照 (东财 ulist 端点); 失败软返回空列表。"""
    import time

    global _INDEX_CACHE
    now = time.monotonic()
    if _INDEX_CACHE is not None and now - _INDEX_CACHE[0] < _INDEX_CACHE_TTL_S:
        return _INDEX_CACHE[1]
    out: list[dict] = []
    try:
        import httpx

        secids = ",".join(secid for secid, _, _ in _US_INDEX_SECIDS)
        resp = httpx.get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={"secids": secids, "invt": 2, "fltt": 2,
                    "fields": "f2,f3,f4,f12,f14"},
            headers={"Referer": "https://quote.eastmoney.com/"},
            timeout=8.0,
        )
        rows = {}
        if resp.status_code == 200:
            payload = resp.json()
            for row in (payload.get("data") or {}).get("diff") or []:
                rows[str(row.get("f12"))] = row
        for _secid, code, name in _US_INDEX_SECIDS:
            r = rows.get(code) or {}
            out.append({
                "symbol": code,
                "name": name,
                "last_price": _finite(r.get("f2")),
                "change_pct": _finite(r.get("f3")) if _finite(r.get("f3")) is None
                else _finite(r.get("f3")) / 100.0,  # 百分数 → 小数制
                "change_amount": _finite(r.get("f4")),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("美股指数快照拉取失败: %s", e)
    _INDEX_CACHE = (now, out)
    return out


def _us_enriched_rows(repo) -> tuple[list[dict], str | None]:
    """enriched 最新日截面 → (榜单行, 日期); 数据源降级同 crypto。"""
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, asset_type="us")
    latest = svc.latest_date()
    if latest is None:
        return [], None
    try:
        df = svc._load_enriched_for_date(latest)
    except Exception as e:  # noqa: BLE001 — 完整性断言失败等按无数据降级
        logger.warning("美股 enriched 最新日读取失败 (%s): %s", latest, e)
        return [], None
    if df.is_empty():
        return [], None
    cols = ["symbol", "name", "close", "change_pct", "amount"]
    rows = df.select([c for c in cols if c in df.columns]).to_dicts()
    for r in rows:
        r.setdefault("name", r.get("symbol"))
        r["turnover_rate"] = None  # 美股无流通股本口径
        r["board"] = "美股"
    return rows, str(latest)


def _trend_and_activity(repo) -> tuple[dict, dict]:
    """站上均线/新高新低 + 放量占比 — 白名单范围 (与 crypto 同构)。"""
    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, asset_type="us")
    latest = svc.latest_date()
    if latest is None:
        return dict(_EMPTY_TREND), dict(_EMPTY_ACTIVITY)
    try:
        df = svc._load_enriched_for_date(latest)
    except Exception as e:  # noqa: BLE001
        logger.warning("美股趋势指标读取失败 (%s): %s", latest, e)
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
    # 美股无流通股本, 换手率无口径; 量比沿用 5 日均量比
    activity = {
        "avg_turnover": 0,
        "high_turnover": 0,
        "high_vol_ratio": sum(1 for v in vol_ratios if v >= 1.5) / total * 100,
        "vol_ratio": sum(vol_ratios) / len(vol_ratios) if vol_ratios else 1,
    }
    return trend, activity


def build_us_overview(repo, as_of: date | None = None) -> dict:
    """装配美股市场总览 — 顶层键与 A 股 OverviewMarket 对齐, 前端 us 市场复用。"""
    rules = get_trading_rules("us")
    rows, local_as_of = _us_enriched_rows(repo)
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

    return _json_safe({
        "market": "us",
        "data_source": data_source,
        "data_window": data_window,
        "scope": "us_whitelist",
        "as_of": local_as_of,
        "quote_status": {
            "enabled": False,
            "running": False,
            "quote_age_ms": None,
            "is_trading_hours": True,  # 一期无实时推送, 展示最近交易日截面
        },
        "indices": _us_index_quotes(),
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
        "concept_rank": dict(_EMPTY_RANK),
        "industry_rank": dict(_EMPTY_RANK),
    })
