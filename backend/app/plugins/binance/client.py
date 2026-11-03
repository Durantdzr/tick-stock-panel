"""币安 REST 客户端 (现货 api.binance.com / U本位合约 fapi.binance.com)。

公共行情端点免 Key; 可选 X-MBX-APIKEY 头提升限额 (只读公共数据, Key 无签名需求)。
合约端点字段结构与现货高度相似但有差异 (如 futures klines 无 close_time 差异,
ticker 字段命名一致), 统一在本层归一。

时间口径: 币安 openTime/closeTime 均为 UTC epoch ms; 由 provider 层统一 +8h
转北京时间 (CONTRIBUTING §3.3: 分钟 datetime 为北京墙钟 naive)。
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"

_MARKET_BASES = {"spot": SPOT_BASE, "futures": FUTURES_BASE}
_KLINE_PATHS = {"spot": "/api/v3/klines", "futures": "/fapi/v1/klines"}
_EXCHANGE_INFO_PATHS = {"spot": "/api/v3/exchangeInfo", "futures": "/fapi/v1/exchangeInfo"}
_TICKER_PATHS = {"spot": "/api/v3/ticker/24hr", "futures": "/fapi/v1/ticker/24hr"}
_PREMIUM_INDEX_PATHS = {"spot": None, "futures": "/fapi/v1/premiumIndex"}

_KLINE_LIMIT = 1000  # 两市场单次返回上限均 ≥1000, 统一按 1000 分页


class BinanceError(Exception):
    """币安接口错误(网络失败 / HTTP 非 200 / 响应结构不符)。"""


class BinanceClient:
    """币安公共行情客户端 (线程安全: httpx.Client 可并发复用)。"""

    def __init__(self, api_key: str = "", timeout: float = 15.0) -> None:
        headers = {"X-MBX-APIKEY": api_key} if api_key else {}
        self._http = httpx.Client(headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _get(self, market: str, path: str, params: dict) -> object:
        base = _MARKET_BASES.get(market)
        if base is None:
            raise BinanceError(f"未知币安市场: {market}")
        try:
            resp = self._http.get(f"{base}{path}", params=params)
        except httpx.HTTPError as e:
            raise BinanceError(f"网络请求失败: {e}") from e
        if resp.status_code != 200:
            # 币安错误信封 {"code": -1121, "msg": "Invalid symbol."}
            detail = ""
            try:
                payload = resp.json()
                detail = f": {payload.get('code')} {payload.get('msg', '')}"
            except ValueError:
                pass
            raise BinanceError(f"HTTP {resp.status_code}{detail} ({market} {path})")
        try:
            return resp.json()
        except ValueError as e:
            raise BinanceError(f"响应不是 JSON: {market} {path}") from e

    def klines(
        self,
        market: str,
        symbol: str,
        interval: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = _KLINE_LIMIT,
    ) -> list[list]:
        """拉取一页 K 线 (数组数组口径, 行结构由 provider 解析)。"""
        params: dict = {"symbol": symbol, "interval": interval, "limit": min(limit, _KLINE_LIMIT)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        data = self._get(market, _KLINE_PATHS[market], params)
        if not isinstance(data, list):
            raise BinanceError(f"klines 响应结构异常: {market} {symbol}")
        return data

    def iter_klines(
        self,
        market: str,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        max_requests: int = 5000,
    ):
        """按 startTime 游标分页产出 K 线, 直到覆盖 end_ms。"""
        cursor = start_ms
        for _ in range(max_requests):
            rows = self.klines(market, symbol, interval, start_ms=cursor, end_ms=end_ms)
            if not rows:
                return
            yield from rows
            last_open = int(rows[-1][0])
            if len(rows) < _KLINE_LIMIT or last_open >= end_ms:
                return
            cursor = last_open + 1

    def exchange_info(self, market: str) -> dict:
        data = self._get(market, _EXCHANGE_INFO_PATHS[market], {})
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise BinanceError(f"exchangeInfo 响应结构异常: {market}")
        return data

    def ticker_24hr(self, market: str) -> list[dict]:
        data = self._get(market, _TICKER_PATHS[market], {})
        if not isinstance(data, list):
            raise BinanceError(f"ticker 响应结构异常: {market}")
        return data

    def premium_index(self, symbol: str | None = None) -> list[dict]:
        """U 本位合约标记价格/最新资金费率 (仅 futures 有此端点)。"""
        if _PREMIUM_INDEX_PATHS["futures"] is None:
            raise BinanceError("premiumIndex 仅存在于 U 本位合约 (futures)")
        params = {"symbol": symbol} if symbol else {}
        data = self._get("futures", _PREMIUM_INDEX_PATHS["futures"], params)
        if not isinstance(data, list):
            raise BinanceError("premiumIndex 响应结构异常: futures")
        return data
