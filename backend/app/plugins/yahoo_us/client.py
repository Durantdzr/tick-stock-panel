"""雅虎财经 v8 chart REST 客户端。

端点: GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}
      ?period1&period2&interval=1d&events=div,splits&includeAdjustedClose=false
公共行情免 Key; 浏览器 UA 必须 (默认 UA 会被 403), cookie 由 fc.yahoo.com 引导
(A3), 429/401/999 时刷新后按 5s/15s 退避重试; 网络抖动按 1s/2s 重试 ×3。
失败语义 fail-closed: 重试穷尽或结构不符抛 YahooError, 绝不静默返空。
period2 恒为 now (provider 层保证): 事件日之后的所有调整拆股必在载荷 events 内。
代理: httpx trust_env 默认信任 HTTPS_PROXY (与 binance 插件同路径)。
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "https://query1.finance.yahoo.com"
_COOKIE_BOOTSTRAP_URL = "https://fc.yahoo.com"
_RATE_LIMIT_CODES = {429, 401, 999}
_THROTTLE_BACKOFF_S = (5.0, 15.0)
_NET_BACKOFF_S = (1.0, 2.0)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


class YahooError(Exception):
    """雅虎接口错误 (网络失败 / HTTP 非 200 / 响应结构不符 / 重试穷尽)。

    status_code: HTTP 状态码 (可缺省; 网络异常/结构异常时为 None),
    供 availability 区分「可达但限频」与「不可达」。
    """

    def __init__(self, msg: str, status_code: int | None = None) -> None:
        super().__init__(msg)
        self.status_code = status_code


class YahooClient:
    """雅虎 v8 chart 公共行情客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, timeout: float = 15.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            base_url=_BASE_URL,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _bootstrap_cookie(self) -> None:
        """fc.yahoo.com 恒 404 但 Set-Cookie (A3); 刷新限频上下文, 忽略状态码。"""
        try:
            self._http.get(_COOKIE_BOOTSTRAP_URL)
        except httpx.HTTPError as e:
            logger.debug("yahoo cookie bootstrap failed: %s", e)

    def chart(self, symbol: str, period1: int, period2: int, retries: bool = True) -> dict:
        """拉取日线 chart 载荷 (含 div/splits 事件)。失败抛 YahooError。

        retries=False: 单次尝试无退避 — availability 探测专用 (该函数在 app
        启动 load_all() 时同步执行, 重试退避会阻塞启动 20s+)。
        """
        params = {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "div,splits",
            "includeAdjustedClose": "false",
        }
        resp: httpx.Response | None = None
        net_err: httpx.HTTPError | None = None
        for attempt in range(1 if not retries else 3):
            try:
                resp = self._http.get(f"/v8/finance/chart/{symbol}", params=params)
            except httpx.HTTPError as e:
                net_err = e
                if retries and attempt < 2:
                    time.sleep(_NET_BACKOFF_S[attempt])
                continue
            if resp.status_code == 200:
                break
            if resp.status_code in _RATE_LIMIT_CODES:
                self._bootstrap_cookie()
                if retries and attempt < 2:
                    time.sleep(_THROTTLE_BACKOFF_S[attempt])
                continue
            raise YahooError(f"HTTP {resp.status_code} ({symbol} chart)", status_code=resp.status_code)
        if resp is None or resp.status_code != 200:
            detail = f"HTTP {resp.status_code}" if resp is not None else f"网络请求失败: {net_err}"
            raise YahooError(
                f"雅虎接口重试穷尽 (3 次): {symbol} {detail}",
                status_code=resp.status_code if resp is not None else None,
            ) from net_err
        try:
            payload = resp.json()
        except ValueError as e:
            raise YahooError(f"响应不是 JSON: {symbol} chart") from e
        chart = payload.get("chart") if isinstance(payload, dict) else None
        if not isinstance(chart, dict):
            raise YahooError(f"响应结构异常: {symbol} chart (缺 chart)")
        if chart.get("error"):
            raise YahooError(f"雅虎返回错误: {symbol} {chart['error']}")
        result = chart.get("result")
        if not isinstance(result, list) or not result:
            raise YahooError(f"响应结构异常: {symbol} chart (result 为空)")
        return payload
