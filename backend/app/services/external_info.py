"""外部资讯聚合 — 财经新闻快讯 + 加密市场情绪指标 (恐惧贪婪/资金费率)。

定位: 看板决策辅助组件的只读外部源。全部 fail-soft — 单源失败不影响
其他源, 全部失败返回空列表 + errors 说明; 进程内 TTL 缓存, 仅看板组件
按需拉取, 不进入实时热路径 (CONTRIBUTING §6.3)。

源均为公开免 Key 端点; 解析器 (parse_*) 是纯函数并配单测 fixture,
网络拉取 (_fetch_text) 与解析分离, 不依赖真实网络。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
_FETCH_TIMEOUT = 8.0
_NEWS_TTL_S = 300
_FNG_TTL_S = 1800
_FUNDING_TTL_S = 300

# 每市场新闻源 (kind: rss=RSS2.0/Atom; sina_zhibo/eastmoney_fast=公开 JSON 快讯)
_NEWS_SOURCES: dict[str, list[dict]] = {
    "stock": [
        {"id": "sina_7x24", "label": "新浪财经 7×24", "kind": "sina_zhibo",
         "url": "https://zhibo.sina.com.cn/api/zhibo/feed?page=1&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1"},
        {"id": "eastmoney_7x24", "label": "东方财富快讯", "kind": "eastmoney_fast",
         "url": "https://np-listapi.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=20"},
    ],
    "crypto": [
        {"id": "coindesk", "label": "CoinDesk", "kind": "rss",
         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"id": "cointelegraph", "label": "Cointelegraph", "kind": "rss",
         "url": "https://cointelegraph.com/rss"},
    ],
}

_FNG_URL = "https://api.alternative.me/fng/?limit=30&format=json"

_FNG_LABELS_ZH = {
    "Extreme Fear": "极度恐惧", "Fear": "恐惧", "Neutral": "中性",
    "Greed": "贪婪", "Extreme Greed": "极度贪婪",
}

_news_cache: dict[str, tuple[float, dict]] = {}
_fng_cache: tuple[float, dict] | None = None
_funding_cache: tuple[float, list] | None = None
_cache_lock = threading.Lock()


def _fetch_text(url: str) -> str:
    # follow_redirects: 部分源 (如 CoinDesk RSS) 会 308 跳转到规范域名
    resp = httpx.get(url, timeout=_FETCH_TIMEOUT, follow_redirects=True,
                     headers={"User-Agent": "tick-stock-panel/1.0"})
    resp.raise_for_status()
    return resp.text


def _iso_of(raw) -> str | None:
    """各种时间口径 → ISO 字符串 (失败软返回 None, 不猜测格式)。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if isinstance(raw, (int, float)) or text.isdigit():
        try:
            ts = float(text)
            # 恐惧贪婪的时间戳是秒
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=CN_TZ).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            return None
    for parser in (
        lambda t: datetime.fromisoformat(t.replace("Z", "+00:00")),
        parsedate_to_datetime,
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M:%S"),
        lambda t: datetime.strptime(t, "%Y-%m-%d %H:%M"),
    ):
        try:
            dt = parser(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CN_TZ)
            return dt.astimezone(CN_TZ).isoformat(timespec="seconds")
        except (ValueError, TypeError):
            continue
    return None


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(raw, max_len: int = 200) -> str:
    """摘要去 HTML 标签 + 压缩空白 + 截断。"""
    if raw is None:
        return ""
    text = _WS_RE.sub(" ", _TAG_RE.sub("", str(raw))).strip()
    return text[:max_len]


# ================================================================
# 解析器 (纯函数, 单测 fixture 覆盖)
# ================================================================

def parse_rss(text: str, limit: int = 20) -> list[dict]:
    """RSS 2.0 / Atom → 标准条目。结构异常抛 ValueError (调用方按源隔离)。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"RSS XML 解析失败: {e}") from e
    items: list[dict] = []

    def _child_text(elem, tag: str) -> str | None:
        # 兼容无命名空间与 Atom 命名空间; itertext 覆盖内嵌子元素 (如 <p> 摘要)
        for child in elem:
            if child.tag.rsplit("}", 1)[-1] == tag:
                return "".join(child.itertext())
        return None

    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "item":
            title = _child_text(elem, "title")
            link = _child_text(elem, "link")
            items.append({
                "title": _clean_text(title, 200),
                "url": (link or "").strip() or None,
                "summary": _clean_text(_child_text(elem, "description"), 200),
                "published_at": _iso_of(_child_text(elem, "pubDate")),
            })
        elif tag == "entry":
            link = None
            for child in elem:
                if child.tag.rsplit("}", 1)[-1] == "link":
                    link = child.get("href")
                    break
            items.append({
                "title": _clean_text(_child_text(elem, "title"), 200),
                "url": (link or "").strip() or None,
                "summary": _clean_text(_child_text(elem, "summary"), 200),
                "published_at": _iso_of(_child_text(elem, "updated")),
            })
    out = [it for it in items if it["title"]]
    return out[:limit]


def parse_sina_zhibo(text: str, limit: int = 20) -> list[dict]:
    """新浪财经 7×24 快讯 JSON → 标准条目。"""
    payload = json.loads(text)
    feed = (payload.get("result") or {}).get("data") or {}
    entries = (feed.get("feed") or {}).get("list") or []
    out = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        body = _clean_text(entry.get("rich_text") or entry.get("text"), 200)
        if not body:
            continue
        out.append({
            "title": body[:80] or body,
            "url": (entry.get("docurl") or "").strip() or None,
            "summary": body,
            "published_at": _iso_of(entry.get("create_time")),
        })
    return out


def parse_eastmoney_fast(text: str, limit: int = 20) -> list[dict]:
    """东方财富 7×24 快讯 JSON → 标准条目。"""
    payload = json.loads(text)
    entries = ((payload.get("data") or {}).get("fastNewsList")) or []
    out = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        body = _clean_text(entry.get("summary") or entry.get("title"), 200)
        if not body:
            continue
        out.append({
            "title": _clean_text(entry.get("title"), 120) or body[:80],
            "url": None,
            "summary": body,
            "published_at": _iso_of(entry.get("showTime")),
        })
    return out


_PARSERS = {
    "rss": parse_rss,
    "sina_zhibo": parse_sina_zhibo,
    "eastmoney_fast": parse_eastmoney_fast,
}


# ================================================================
# 聚合入口 (带 TTL 缓存)
# ================================================================

def fetch_news(asset_type: str, *, force: bool = False) -> dict:
    """聚合某市场的新闻快讯。返回 {items, sources, errors, updated_at}。"""
    now = time.monotonic()
    with _cache_lock:
        cached = _news_cache.get(asset_type)
        if cached and not force and now - cached[0] < _NEWS_TTL_S:
            return cached[1]

    sources = _NEWS_SOURCES.get(asset_type) or []
    items: list[dict] = []
    errors: list[str] = []
    ok_sources: list[str] = []
    for source in sources:
        parser = _PARSERS.get(source["kind"])
        if parser is None:
            continue
        try:
            entries = parser(_fetch_text(source["url"]))
        except Exception as e:  # noqa: BLE001 — 单源失败隔离
            logger.warning("资讯源 %s 拉取失败: %s", source["id"], e)
            errors.append(f"{source['label']}: {e}")
            continue
        ok_sources.append(source["label"])
        for entry in entries:
            entry["source"] = source["label"]
        items.extend(entries)

    # 按发布时间倒序, 缺时间的排后; 全量截断 60 条
    items.sort(key=lambda it: it.get("published_at") or "", reverse=True)
    items = items[:60]
    payload = {
        "asset_type": asset_type,
        "items": items,
        "sources": ok_sources,
        "errors": errors,
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
    }
    with _cache_lock:
        _news_cache[asset_type] = (now, payload)
    return payload


def parse_fng(text: str) -> dict:
    """alternative.me 恐惧贪婪 JSON → {current, history}。"""
    payload = json.loads(text)
    data = payload.get("data") or []
    history = []
    for row in data:
        try:
            value = int(str(row.get("value")))
        except (TypeError, ValueError):
            continue
        label_en = str(row.get("value_classification") or "")
        history.append({
            "date": _iso_of(row.get("timestamp")),
            "value": value,
            "label": _FNG_LABELS_ZH.get(label_en, label_en),
        })
    # API 返回最新在前 → 统一按时间倒序
    history.sort(key=lambda it: it["date"] or "", reverse=True)
    current = history[0] if history else None
    return {"current": current, "history": history[:30]}


def fetch_fear_greed(*, force: bool = False) -> dict:
    """加密市场恐惧贪婪指数 (0-100, alternative.me, 免 Key)。"""
    global _fng_cache
    now = time.monotonic()
    with _cache_lock:
        if _fng_cache and not force and now - _fng_cache[0] < _FNG_TTL_S:
            return _fng_cache[1]
    try:
        data = parse_fng(_fetch_text(_FNG_URL))
        error = None
    except Exception as e:  # noqa: BLE001
        logger.warning("恐惧贪婪指数拉取失败: %s", e)
        data, error = {"current": None, "history": []}, str(e)
    payload = {**data, "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"), "error": error}
    with _cache_lock:
        _fng_cache = (now, payload)
    return payload


def fetch_funding_rates(*, force: bool = False) -> list[dict]:
    """U 本位合约资金费率 — 经 crypto 数据源 provider (默认 binance) 获取。

    仅返回白名单合约标的 (决策相关性), 按 |费率| 降序。
    """
    global _funding_cache
    now = time.monotonic()
    with _cache_lock:
        if _funding_cache and not force and now - _funding_cache[0] < _FUNDING_TTL_S:
            return _funding_cache[1]

    from app.data_providers import custom as custom_sources
    from app.services import crypto_universe, preferences

    records: list[dict] = []
    try:
        provider = custom_sources.get_provider(preferences.get_crypto_data_provider())
        if provider is not None and hasattr(provider, "get_funding_rates"):
            records = provider.get_funding_rates()
    except Exception as e:  # noqa: BLE001 — fail-soft, 空列表即不可用
        logger.warning("资金费率拉取失败: %s", e)

    wanted: set[str] | None = None
    try:
        from app.config import settings as app_settings

        universe = crypto_universe.load_universe(app_settings.data_dir)
        wanted = {f"{s}.P" for s in universe.get("futures") or []} or None
    except Exception:  # noqa: BLE001
        wanted = None
    if wanted:
        records = [r for r in records if r.get("symbol") in wanted]
    records.sort(key=lambda r: abs(r.get("funding_rate") or 0), reverse=True)
    with _cache_lock:
        _funding_cache = (now, records)
    return records


def fetch_sentiment(asset_type: str) -> dict:
    """/api/info/sentiment 聚合: crypto=恐惧贪婪+资金费率; 其他市场空态。"""
    if asset_type != "crypto":
        return {"asset_type": asset_type, "fng": None, "funding": [],
                "note": "加密货币市场专用的外部情绪指标 (A股情绪见总览情绪雷达)"}
    return {
        "asset_type": asset_type,
        "fng": fetch_fear_greed(),
        "funding": fetch_funding_rates(),
        "updated_at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
    }
