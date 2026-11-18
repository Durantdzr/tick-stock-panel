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

import polars as pl
import pytest

from app.plugins.yahoo_us.client import YahooError
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


# ---------------------------------------------------------------- provider
class _FakeYahooClient:
    """按 symbol 返回预置 payload / 抛错; 记录 chart 调用供断言。"""

    def __init__(self, payloads: dict[str, dict] | None = None, fail: set[str] | None = None):
        self.payloads = payloads or {}
        self.fail = fail or set()
        self.charts: list[tuple[str, int, int]] = []

    def chart(self, symbol: str, period1: int, period2: int, retries: bool = True) -> dict:
        self.charts.append((symbol, period1, period2))
        if symbol in self.fail:
            raise YahooError(f"HTTP 404 ({symbol} chart)", status_code=404)
        return self.payloads[symbol]

    def close(self) -> None:
        pass


def _provider_with_split(monkeypatch=None) -> tuple[object, _FakeYahooClient]:
    from app.plugins.yahoo_us.provider import YahooUSProvider

    fake = _FakeYahooClient(payloads={"AAPL": _load("chart_aapl_split_window.json")})
    return YahooUSProvider(client=fake), fake


def test_get_daily_symbol_mapping() -> None:
    from app.plugins.yahoo_us.provider import YahooUSProvider

    fake = _FakeYahooClient(payloads={"BRK-B": _load("chart_aapl_split_window.json")})
    provider = YahooUSProvider(client=fake)
    provider.get_daily(["BRK.B", "brk_b"], datetime(2020, 8, 20), None, asset_type="us")
    requested = [c[0] for c in fake.charts]
    assert requested == ["BRK-B", "BRK-B"], "内部 ticker 必须映射为雅虎格式 ('.'/'_'→'-', 大写)"


def test_get_daily_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    provider, fake = _provider_with_split()
    start = datetime(2020, 8, 20)
    df = provider.get_daily(["AAPL"], start, None, asset_type="us")

    from app.data_providers.normalizer import DAILY_COLS

    assert set(df.columns) <= set(DAILY_COLS) | {"source"}
    assert df.schema["date"] == pl.Date
    assert df["source"].unique().to_list() == ["yahoo_us"]
    assert "amount" not in df.columns, "雅虎无成交额字段, 缺失即缺失, 不伪造"
    row28 = df.filter(pl.col("date") == date(2020, 8, 28))
    assert row28["close"][0] == pytest.approx(124.8075 * 4, abs=1e-4), "close 须为反向还原后的原始价"
    # 窗口语义: start → period1 原样; end=None → period2 = now (全史则 period1=0)
    symbol_ys, period1, period2 = fake.charts[-1]
    assert symbol_ys == "AAPL" and period1 == int(start.timestamp())
    assert period2 > int(datetime.now().timestamp()) - 60
    provider.get_daily(["AAPL"], None, None, asset_type="us")
    assert fake.charts[-1][1] == 0, "start=None 表示全量历史 (period1=0)"


def test_get_daily_throttles_between_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", sleeps.append)
    payload = _load("chart_aapl_split_window.json")
    from app.plugins.yahoo_us.provider import YahooUSProvider

    fake = _FakeYahooClient(payloads={"AAPL": payload, "MSFT": payload})
    provider = YahooUSProvider(client=fake)
    provider.get_daily(["AAPL", "MSFT"], datetime(2020, 8, 20), None, asset_type="us")
    assert len(sleeps) >= 1, "标的之间必须节流"


def test_get_daily_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    from app.data_providers.base import ProviderRequestError
    from app.plugins.yahoo_us.provider import YahooUSProvider

    fake = _FakeYahooClient(
        payloads={"AAPL": _load("chart_aapl_split_window.json")}, fail={"MSFT"})
    provider = YahooUSProvider(client=fake)
    with pytest.raises(ProviderRequestError) as excinfo:
        provider.get_daily(["AAPL", "MSFT"], datetime(2020, 8, 20), None, asset_type="us")
    assert "MSFT" in str(excinfo.value)


def test_require_us_guard() -> None:
    from app.data_providers.base import ProviderRequestError

    provider, _ = _provider_with_split()
    with pytest.raises(ProviderRequestError):
        provider.get_daily(["AAPL"], None, None, asset_type="stock")
    with pytest.raises(ProviderRequestError):
        provider.get_instruments("stock")


def test_get_instruments_explicitly_unsupported() -> None:
    from app.data_providers.base import ProviderRequestError

    provider, _ = _provider_with_split()
    with pytest.raises(ProviderRequestError, match="维表"):
        provider.get_instruments("us")


def test_plugin_manifest_declares_us_market() -> None:
    from app.data_providers import custom as custom_sources

    manifest = custom_sources.plugin_manifest("yahoo_us")
    assert manifest is not None
    assert manifest["name"] == "yahoo_us"
    assert manifest["asset_types"] == ["us"]
    assert set(manifest["datasets"]) == {"daily", "adj_factor"}
    assert manifest["runtime"] == "none"
    assert manifest["entry"] == "app.plugins.yahoo_us.provider:YahooUSProvider"
    assert manifest["check"] == "app.plugins.yahoo_us.provider:availability"


def test_us_market_source_excluded_from_ashare_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.data_providers import capabilities
    from app.data_providers import custom as custom_sources

    plugins = [{
        "name": "yahoo_us", "display": "雅虎财经·美股",
        "datasets": {"daily", "adj_factor"}, "asset_types": {"us"},
        "available": True, "status": "ok", "kind": "plugin",
    }]
    monkeypatch.setattr(custom_sources, "list_plugins", lambda: plugins)
    monkeypatch.setattr(custom_sources, "list_sources", lambda: [])
    matrix = capabilities.build_capability_matrix({}, "expert")
    for cap in matrix["capabilities"]:
        names = [c["name"] for c in cap["candidates"]]
        assert "yahoo_us" not in names, f"市场专用源不得进入 A 股能力候选: {cap['id']}"


def test_availability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.plugins.yahoo_us import provider as yprovider

    ok = _FakeYahooClient(payloads={"AAPL": _load("chart_aapl_split_window.json")})
    monkeypatch.setattr(yprovider, "YahooClient", lambda timeout=15.0: ok)
    assert yprovider.availability() == (True, "ok")

    throttled = _FakeYahooClient(fail=set())
    def _throttle(symbol, p1, p2, retries=True):
        raise YahooError("HTTP 429 (AAPL chart)", status_code=429)
    throttled.chart = _throttle
    monkeypatch.setattr(yprovider, "YahooClient", lambda timeout=15.0: throttled)
    available, reason = yprovider.availability()
    assert available and "限频" in reason, "429 证明接口可达: 注册放行, 限频由同步轮次退避"

    dead = _FakeYahooClient(fail=set())
    def _dead(symbol, p1, p2, retries=True):
        raise YahooError("HTTP 404 (AAPL chart)", status_code=404)
    dead.chart = _dead
    monkeypatch.setattr(yprovider, "YahooClient", lambda timeout=15.0: dead)
    available, reason = yprovider.availability()
    assert not available and reason


# ---------------------------------------------------------------- adj factors
def _provider(payload_by_symbol: dict[str, dict]):
    from app.plugins.yahoo_us.provider import YahooUSProvider

    return YahooUSProvider(client=_FakeYahooClient(payloads=payload_by_symbol))


def test_adj_factors_split_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    provider = _provider({"AAPL": _load("chart_aapl_split_window.json")})
    df = provider.get_adj_factors(["AAPL"], None, None, asset_type="us")
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "AAPL"
    assert row["trade_date"] == date(2020, 8, 31)
    assert row["ex_factor"] == pytest.approx(4.0)
    assert row["dividend"] is None and row["prev_close"] is None


def test_adj_factors_dividend_component(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    provider = _provider({"AAPL": _load("chart_aapl_div_window.json")})
    prev = {"AAPL": {date(2020, 2, 6): 318.0}}
    df = provider.get_adj_factors(["AAPL"], None, None, asset_type="us", prev_closes=prev)
    assert df.height == 2, "div 夹具含分红事件 + 8 月拆股事件"
    div_row = df.filter(pl.col("trade_date") == date(2020, 2, 7)).row(0, named=True)
    assert div_row["ex_factor"] == pytest.approx(318.0 / (318.0 - 0.1925), abs=1e-9)
    assert div_row["dividend"] == pytest.approx(0.1925, abs=1e-9)
    assert div_row["prev_close"] == 318.0
    split_row = df.filter(pl.col("trade_date") == date(2020, 8, 31)).row(0, named=True)
    assert split_row["ex_factor"] == pytest.approx(4.0)
    assert split_row["dividend"] is None


def test_adj_factors_missing_prev_close_skips_dividend(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    provider = _provider({"AAPL": _load("chart_aapl_div_window.json")})
    # 只提供 ex_date 之后的日期 → 事件前一日缺失
    prev = {"AAPL": {date(2020, 3, 2): 320.0}}
    with caplog.at_level(logging.WARNING, logger="app.plugins.yahoo_us.provider"):
        df = provider.get_adj_factors(["AAPL"], None, None, asset_type="us", prev_closes=prev)
    assert df.height == 1, "分红分量缺失 → 该事件不产出; 拆股分量不受影响"
    assert df["trade_date"].to_list() == [date(2020, 8, 31)]
    assert any("prev_close" in r.message for r in caplog.records)


def test_adj_factors_same_day_split_and_dividend_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    ts = 1598880600  # 2020-08-31 13:30 EDT
    payload = {
        "chart": {"result": [{
            "meta": {"exchangeTimezoneName": "America/New_York"},
            "timestamp": [ts],
            "indicators": {"quote": [{"open": [30.0], "high": [31.0], "low": [29.0],
                                      "close": [30.5], "volume": [1000]}]},
            "events": {
                "splits": {str(ts): {"date": ts, "numerator": 4.0, "denominator": 1.0}},
                "dividends": {str(ts): {"date": ts, "amount": 0.5}},
            },
        }], "error": None},
    }
    provider = _provider({"XYZ": payload})
    prev = {"XYZ": {date(2020, 8, 28): 100.0}}
    df = provider.get_adj_factors(["XYZ"], None, None, asset_type="us", prev_closes=prev)
    assert df.height == 1, "同日拆股+分红合并为一行"
    row = df.row(0, named=True)
    # d_as_traded = 0.5 × later(=1) × 同日拆股 4 = 2.0 (分红按除权日前股数计)
    assert row["ex_factor"] == pytest.approx(4.0 * 100.0 / (100.0 - 2.0), abs=1e-9)
    assert row["dividend"] == pytest.approx(2.0, abs=1e-9)
    assert row["prev_close"] == 100.0


def test_adj_factors_incremental_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    provider = _provider({"AAPL": _load("chart_aapl_split_window.json")})
    from datetime import datetime as dt

    df = provider.get_adj_factors(["AAPL"], dt(2020, 9, 1), None, asset_type="us")
    assert df.height == 0, "增量锚点语义: trade_date > start_time 才返回"
    df2 = provider.get_adj_factors(["AAPL"], dt(2020, 8, 30), None, asset_type="us")
    assert df2["trade_date"].to_list() == [date(2020, 8, 31)]


def test_adj_factors_frame_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.provider.time.sleep", lambda _s: None)
    from app.data_providers.normalizer import ADJ_FACTOR_COLS, normalize_adj_factors

    provider = _provider({"AAPL": _load("chart_aapl_split_window.json")})
    df = provider.get_adj_factors(["AAPL"], None, None, asset_type="us")
    assert set(df.columns) == {"symbol", "trade_date", "ex_factor", "dividend", "prev_close"}
    assert df.schema["trade_date"] == pl.Date
    assert df.schema["ex_factor"] == pl.Float64
    recast = normalize_adj_factors(df)
    assert recast["ex_factor"].to_list() == df["ex_factor"].to_list()
    assert set(ADJ_FACTOR_COLS) <= set(recast.columns)
