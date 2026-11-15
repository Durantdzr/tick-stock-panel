"""美股交易日判定 (exchange_calendars XNYS, 单一权威)。

与 A 股探针链 (services/trading_day.py) 的区别: XNYS 日历本地权威,
无需网络探针; 缓存独立于 A 股的全局单槽 (避免互相覆盖)。
日历库不可用时回退 ET weekday<5 近似 (漏节假日, fail-open 但日志留痕)。
"""

from __future__ import annotations

import logging
import threading
from datetime import date

logger = logging.getLogger(__name__)

_TTL_S = 3600.0
_cache: dict[str, tuple[bool, float]] = {}
_cache_lock = threading.Lock()
_calendar_checked = False
_calendar: object | None = None


def _get_calendar():
    """懒加载 XNYS 日历 (进程级单例); 失败返回 None (调用方走兜底)。"""
    global _calendar, _calendar_checked
    if _calendar_checked:
        return _calendar
    try:
        import exchange_calendars

        _calendar = exchange_calendars.get_calendar("XNYS", start="1990-01-01")
    except Exception as e:  # noqa: BLE001
        logger.warning("exchange_calendars XNYS 加载失败, 交易日回退 weekday 近似: %s", e)
        _calendar = None
    _calendar_checked = True
    return _calendar


def is_us_trading_day(day: date) -> bool:
    """美股 (纽交所) 是否交易日。XNYS 权威; 日历不可用时 weekday<5 兜底。"""
    import time as _time

    key = day.isoformat()
    now = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[1] < _TTL_S:
            return hit[0]

    cal = _get_calendar()
    if cal is not None:
        try:
            import pandas as pd

            verdict = bool(cal.is_session(pd.Timestamp(day)))
        except Exception as e:  # noqa: BLE001
            logger.warning("XNYS 查询失败 %s, 回退 weekday 近似: %s", day, e)
            verdict = day.weekday() < 5
    else:
        verdict = day.weekday() < 5

    with _cache_lock:
        _cache[key] = (verdict, now)
    return verdict


def us_today() -> date:
    """当前美东日期 (回测窗口默认右端等场景)。"""
    try:
        from zoneinfo import ZoneInfo

        from app.market_time import cn_now

        return cn_now().astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001
        from app.market_time import cn_today

        return cn_today()
