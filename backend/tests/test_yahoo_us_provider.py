"""YahooUSProvider 契约测试 (golden 夹具驱动 + fake client 注入, 零真实网络)。

parse_chart: 反向拆股还原与事件解析, 方向/数值由 Task 0 夹具钉死
(synthetic 夹具: 结构与数学自洽; 方向依据 yfinance/spectre 文档惯例,
真机复核见设计文档实施决定 2)。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.plugins.yahoo_us.provider import parse_chart

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
NY = ZoneInfo("America/New_York")
_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=NY)  # 盘中之外, 当日 bar 规则不触发


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parse_split_window_direction_and_reverse_adjust() -> None:
    payload = _load("chart_aapl_split_window.json")
    rows, splits, dividends = parse_chart(payload, "AAPL", now_et=_NOW)
    assert rows and all(r["symbol"] == "AAPL" for r in rows)
    assert (date(2020, 8, 31), pytest.approx(4.0)) in splits, "splits to/from 方向: 4:1 → 4.0"
    assert dividends == []
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    displayed = result["indicators"]["quote"][0]
    idx28 = ts.index(1598621400)  # 2020-08-28 (拆股前最后一个交易日)
    row28 = next(r for r in rows if r["date"] == date(2020, 8, 28))
    assert row28["close"] == pytest.approx(displayed["close"][idx28] * 4.0, abs=1e-6)
    assert row28["open"] == pytest.approx(displayed["open"][idx28] * 4.0, abs=1e-6)
    # 拆股日之后的行不受还原影响 (later = 1)
    row31 = next(r for r in rows if r["date"] == date(2020, 8, 31))
    assert row31["close"] == pytest.approx(displayed["close"][ts.index(1598880600)], abs=1e-9)


def test_parse_volume_reverse_adjusted() -> None:
    payload = _load("chart_aapl_split_window.json")
    rows, _, _ = parse_chart(payload, "AAPL", now_et=_NOW)
    displayed_vol = payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"]
    row28 = next(r for r in rows if r["date"] == date(2020, 8, 28))
    assert row28["volume"] == displayed_vol[4] / 4.0
    row31 = next(r for r in rows if r["date"] == date(2020, 8, 31))
    assert row31["volume"] == displayed_vol[5]


def test_parse_div_window_as_traded_amount() -> None:
    payload = _load("chart_aapl_div_window.json")
    rows, splits, dividends = parse_chart(payload, "AAPL", now_et=_NOW)
    assert (date(2020, 2, 7), pytest.approx(0.1925, abs=1e-9)) in dividends, \
        "displayed 0.048125 × 后续拆股 4 = as-traded 0.1925"
    assert (date(2020, 8, 31), pytest.approx(4.0)) in splits
    # 价格同样被 8 月拆股回溯调整: displayed × 4 = as-traded
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    displayed = result["indicators"]["quote"][0]
    idx06 = ts.index(1581013800)  # 2020-02-06
    row06 = next(r for r in rows if r["date"] == date(2020, 2, 6))
    assert row06["close"] == pytest.approx(displayed["close"][idx06] * 4.0, abs=1e-6)


def test_parse_timestamp_converts_to_ny_trading_date() -> None:
    # EST 段 (2 月, UTC-5) 与 EDT 段 (8 月, UTC-4) 边界各取一
    rows_feb, _, _ = parse_chart(_load("chart_aapl_div_window.json"), "AAPL", now_et=_NOW)
    assert rows_feb[0]["date"] == date(2020, 2, 3)
    rows_aug, _, _ = parse_chart(_load("chart_aapl_split_window.json"), "AAPL", now_et=_NOW)
    assert rows_aug[0]["date"] == date(2020, 8, 24)


def test_parse_drops_unfinished_intraday_bar() -> None:
    payload = _load("chart_aapl_split_window.json")
    result = payload["chart"]["result"][0]
    now_am = datetime(2026, 11, 18, 10, 0, tzinfo=NY)
    result["timestamp"][-1] = int(now_am.timestamp())
    result["indicators"]["quote"][0]["close"][-1] = 111.0

    rows_am, _, _ = parse_chart(payload, "AAPL", now_et=now_am)
    assert all(r["date"] != now_am.date() for r in rows_am), "盘中当日未完成 bar 必须丢弃"

    rows_pm, _, _ = parse_chart(payload, "AAPL", now_et=datetime(2026, 11, 18, 16, 5, tzinfo=NY))
    assert any(r["date"] == now_am.date() and r["close"] == pytest.approx(111.0)
               for r in rows_pm), "收盘后当日 bar 保留"
