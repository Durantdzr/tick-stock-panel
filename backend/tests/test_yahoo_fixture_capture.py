"""雅虎 v8 chart golden 夹具: 捕获 (network) 与 schema 校验 (离线)。

夹具是 Task 2 parse_chart 的方向实证数据源:
- chart_aapl_split_window.json: AAPL 2020-08-31 4:1 拆股窗口 (period2=捕获时刻)
  → 实证 splits numerator/denominator 的 to/from 方向;
- chart_aapl_div_window.json: AAPL 2020-02-07 分红窗口 (period2=2021-06-01,
  载荷含 2020-08-31 拆股事件) → 实证 displayed dividend 是否已按后续拆股调整
  (as-traded $0.1925/股: displayed≈0.1925 未调整 / ≈0.048125 已调整)。

test_validate_committed_fixtures 离线常跑, 充当提交内夹具的结构守卫;
test_capture_missing_fixtures 需 --run-network + 代理, 仅补缺失夹具 (不覆盖已有)。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
NY = ZoneInfo("America/New_York")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_SPLIT_WINDOW = "chart_aapl_split_window.json"
_DIV_WINDOW = "chart_aapl_div_window.json"
# period1/period2 为 epoch 秒 (排他与 yfinance 口径一致; period2=None 表示捕获时刻)
_WINDOWS = {
    _SPLIT_WINDOW: {"symbol": "AAPL", "period1": 1598227200, "period2": None},   # 2020-08-24
    _DIV_WINDOW: {"symbol": "AAPL", "period1": 1580688000, "period2": 1622505600},  # 2020-02-03 .. 2021-06-01
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _events_at(payload: dict, kind: str, want: date) -> dict:
    """取 events.<kind> 中日期等于 want 的事件 (key 或内嵌 date 均为 epoch 秒)。"""
    result = payload["chart"]["result"][0]
    events = (result.get("events") or {}).get(kind) or {}
    for entry in events.values():
        ts = int(entry.get("date") or 0)
        if ts == 0:
            continue
        if datetime.fromtimestamp(ts, NY).date() == want:
            return entry
    return {}


def _quote_lengths(payload: dict) -> tuple[int, int]:
    result = payload["chart"]["result"][0]
    ts = result["timestamp"]
    close = result["indicators"]["quote"][0]["close"]
    return len(ts), len(close)


def test_validate_committed_fixtures() -> None:
    """离线 schema 守卫: 两份夹具存在且事件/quote 结构符合 v8 口径。"""
    split = _load(_SPLIT_WINDOW)
    entry = _events_at(split, "splits", date(2020, 8, 31))
    assert entry, "split 夹具缺少 2020-08-31 拆股事件"
    # 方向实证记录: numerator/denominator 的 to/from 语义由本夹具冻结
    assert float(entry["numerator"]) / float(entry["denominator"]) > 0

    div = _load(_DIV_WINDOW)
    entry = _events_at(div, "dividends", date(2020, 2, 7))
    assert entry, "dividend 夹具缺少 2020-02-07 分红事件"
    assert float(entry["amount"]) > 0

    for payload in (split, div):
        n_ts, n_close = _quote_lengths(payload)
        assert n_ts > 0 and n_ts == n_close


@pytest.mark.network
def test_capture_missing_fixtures() -> None:
    """补齐缺失或 _synthetic 夹具 (真机捕获覆盖合成夹具); 429 换 query2 主机重试。"""
    now_s = int(datetime.now().timestamp())
    missing = []
    for n in _WINDOWS:
        path = FIXTURES / n
        if not path.exists() or json.loads(path.read_text(encoding="utf-8")).get("_synthetic"):
            missing.append(n)
    if not missing:
        pytest.skip("夹具齐备, 无需捕获")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in missing:
        spec = _WINDOWS[name]
        payload = None
        for host in ("query1", "query2", "query1"):
            url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{spec['symbol']}"
            resp = httpx.get(
                url,
                params={
                    "period1": spec["period1"],
                    "period2": spec["period2"] or now_s,
                    "interval": "1d",
                    "events": "div,splits",
                    "includeAdjustedClose": "false",
                },
                headers={"User-Agent": UA},
                timeout=30.0,
            )
            if resp.status_code == 200:
                payload = resp.json()
                break
            if resp.status_code == 429:
                continue  # 限频: 换主机/再试
        assert payload is not None, f"捕获失败 (末次 HTTP {resp.status_code}): {name}"
        (FIXTURES / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
