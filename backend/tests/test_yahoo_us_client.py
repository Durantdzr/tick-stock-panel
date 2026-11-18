"""YahooClient 契约测试 (httpx.MockTransport 注入, 零真实网络)。

覆盖: 请求构造 / 429 cookie 引导重试 / 退避序列 / 非 429 立即失败 /
结构校验 fail-closed / 网络抖动重试。
"""

from __future__ import annotations

import httpx
import pytest

from app.plugins.yahoo_us.client import USER_AGENT, YahooClient, YahooError


def _ok_payload() -> dict:
    return {"chart": {"result": [{"meta": {}, "timestamp": [1]}], "error": None}}


def _handler_store(calls: list[httpx.Request], chart_status: int):
    """记录请求; fc.yahoo.com 恒 404 (Set-Cookie 语义), chart 返回固定状态码。"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        return httpx.Response(chart_status, json=_ok_payload() if chart_status == 200 else None)

    return handler


def test_chart_success_and_query() -> None:
    calls: list[httpx.Request] = []
    client = YahooClient(transport=httpx.MockTransport(_handler_store(calls, 200)))
    data = client.chart("AAPL", 1000, 2000)
    assert data == _ok_payload()
    req = calls[-1]
    assert req.url.path == "/v8/finance/chart/AAPL"
    assert req.headers["user-agent"] == USER_AGENT
    params = dict(req.url.params)
    assert params["interval"] == "1d"
    assert params["events"] == "div,splits"
    assert params["includeAdjustedClose"] == "false"
    assert params["period1"] == "1000"
    assert params["period2"] == "2000"
    client.close()


def test_429_triggers_cookie_bootstrap_then_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.plugins.yahoo_us.client.time.sleep", lambda _s: None)
    calls: list[httpx.Request] = []
    chart_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal chart_calls
        calls.append(request)
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        chart_calls += 1
        if chart_calls == 1:
            return httpx.Response(429)
        return httpx.Response(200, json=_ok_payload())

    client = YahooClient(transport=httpx.MockTransport(handler))
    assert client.chart("AAPL", 1, 2) == _ok_payload()
    assert any(r.url.host == "fc.yahoo.com" for r in calls), "429 后必须触发 cookie 引导"


def test_429_exhausts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.plugins.yahoo_us.client.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        return httpx.Response(429)

    client = YahooClient(transport=httpx.MockTransport(handler))
    with pytest.raises(YahooError):
        client.chart("AAPL", 1, 2)
    assert sleeps == [5.0, 15.0], "退避序列应为 5s/15s, 共 3 次尝试"


def test_404_fails_immediately_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.plugins.yahoo_us.client.time.sleep", sleeps.append)
    calls: list[httpx.Request] = []
    client = YahooClient(transport=httpx.MockTransport(_handler_store(calls, 404)))
    with pytest.raises(YahooError):
        client.chart("BAD.SYM", 1, 2)
    assert sleeps == [], "非限频状态码不得重试"
    chart_calls = [r for r in calls if r.url.host != "fc.yahoo.com"]
    assert len(chart_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"chart": {"error": {"code": "Bad Request", "description": "x"}}},
        {"chart": {}},
        {"unexpected": 1},
    ],
)
def test_bad_structure_raises(payload: dict) -> None:
    client = YahooClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=payload)))
    with pytest.raises(YahooError):
        client.chart("AAPL", 1, 2)


def test_network_error_retries_then_exhausts(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.plugins.yahoo_us.client.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = YahooClient(transport=httpx.MockTransport(handler))
    with pytest.raises(YahooError):
        client.chart("AAPL", 1, 2)
    assert sleeps == [1.0, 2.0], "网络抖动退避应为 1s/2s, 共 3 次尝试"


def test_probe_mode_single_attempt_no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """availability 走 chart(retries=False): 单次尝试, 零退避 (启动路径不得阻塞)。"""
    sleeps: list[float] = []
    monkeypatch.setattr("app.plugins.yahoo_us.client.time.sleep", sleeps.append)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fc.yahoo.com":
            return httpx.Response(404)
        calls.append(1)
        return httpx.Response(429)

    client = YahooClient(transport=httpx.MockTransport(handler))
    with pytest.raises(YahooError) as excinfo:
        client.chart("AAPL", 1, 2, retries=False)
    assert calls == [1] and sleeps == [], "探测模式 = 1 次请求, 无退避"
    assert excinfo.value.status_code == 429
