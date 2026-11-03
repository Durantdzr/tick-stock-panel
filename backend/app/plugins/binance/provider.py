"""币安(api.binance.com / fapi.binance.com)加密货币数据 provider。

专用市场数据源: plugin.yaml 声明 asset_types: [crypto], 不进入 A 股 7 能力路由
候选 (capabilities._declared_sources 过滤); crypto 同步服务直接经
custom.loader.get_provider("binance") 取数, fail-closed。

实现数据集 (仅 asset_type="crypto"):
  - daily     日K(现货+U本位合约), 1d K 线分页, date=UTC 开盘时间的北京日期
  - minute    1 分钟K, datetime=北京墙钟 naive (UTC+8h)
  - realtime  24hr 全量快照(现货+合约合并), 合约标的 .P 后缀

symbol 命名空间 (现货与合约 symbol 同名, 必须隔离):
  - 现货  BTCUSDT       (wire: BTCUSDT,  市场 spot)
  - 合约  BTCUSDT.P     (wire: BTCUSDT,  市场 futures)

单位与口径 (CONTRIBUTING §3.1, 与股票契约的差异全部显式声明):
  - volume = 基础币数量 (BTC 等), amount = 计价币数量 (USDT)。
    二者均不是股票"手/元"口径; 策略/回测消费时按相对量使用, 绝对换算无意义。
  - change_pct 契约小数制 (priceChangePercent 为百分数 → /100)。
  - prev_close 现货取 prevClosePrice; 合约 24hr 无昨收列, 由 last-priceChange 推导
    (币安 priceChange 即 相对24hr前价格差, 推导值仅供涨跌幅显示)。
  - 日K不复权概念不存在 (crypto 无分红/拆股), OHLC 直接透传。
  - 日线 date: 币安日线以 UTC 00:00 开盘 (= 北京 08:00), 开盘时间的北京日期
    与 UTC 日期相同, 直接取 UTC 日期, 不做 +1 天错位。
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import DAILY_COLS, normalize_daily
from app.plugins.binance.client import BinanceClient, BinanceError

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "minute", "realtime")

CN_TZ = timezone(timedelta(hours=8))
_KLINE_INTERVAL_S = 0.15  # 单标的请求节流 (币安按权重限频, 公共限额 6000/min 足够)

_PERP_SUFFIX = ".P"
_MARKETS = ("spot", "futures")
_QUOTE_SUFFIX = "USDT"  # 只收录 USDT 计价交易对 (白名单约束的先决条件)


def get_api_key() -> str:
    """可选 Key: 环境变量 BINANCE_API_KEY (提升限额, 公共行情不强制)。"""
    return os.environ.get("BINANCE_API_KEY", "").strip()


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测现货+合约各一次公共端点连通性。不抛异常。"""
    try:
        probe = BinanceClient(api_key=get_api_key(), timeout=6.0)
    except Exception as e:  # noqa: BLE001
        return False, f"客户端初始化失败: {e}"
    try:
        probe.klines("spot", "BTCUSDT", "1d", limit=1)
        probe.klines("futures", "BTCUSDT", "1d", limit=1)
    except BinanceError as e:
        return False, f"币安接口不可达(部分地区需代理): {e}"
    finally:
        with contextlib.suppress(Exception):
            probe.close()
    return True, "ok"


@dataclass
class _BinanceConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "binance"
    display_name: str = "币安 Binance"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text or text == "-":
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def split_symbol(symbol: str) -> tuple[str, str] | None:
    """内部 symbol → (market, wire symbol)。BTCUSDT.P → ("futures","BTCUSDT")。"""
    if not symbol:
        return None
    if symbol.endswith(_PERP_SUFFIX):
        wire = symbol[: -len(_PERP_SUFFIX)]
        return ("futures", wire) if wire else None
    return ("spot", symbol) if symbol.isupper() and symbol.isalnum() else None


def join_symbol(market: str, wire: str) -> str:
    """(market, wire symbol) → 内部 symbol。"""
    return f"{wire}{_PERP_SUFFIX}" if market == "futures" else wire


def _beijing_date_of_ms(ms: int) -> date:
    """UTC epoch ms → 北京日期。币安日线以 UTC 00:00 开盘 (= 北京 08:00 同日)。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=CN_TZ).date()


def _beijing_naive_of_ms(ms: int) -> datetime:
    """UTC epoch ms → 北京墙钟 naive datetime (分钟 datetime 存储口径)。"""
    return datetime.fromtimestamp(ms / 1000.0, tz=CN_TZ).replace(tzinfo=None)


def _market_of(asset_type: str) -> tuple[str, ...]:
    if asset_type != "crypto":
        raise ProviderRequestError(
            f"币安数据源仅提供加密货币(crypto)数据, 收到 asset_type={asset_type!r}"
        )
    return _MARKETS


class BinanceProvider:
    """币安数据源 (crypto 专用)。"""

    name = "binance"
    builtin = True

    def __init__(self) -> None:
        self.config = _BinanceConfig()
        self._client: BinanceClient | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> BinanceClient:
        if self._client is None:
            self._client = BinanceClient(api_key=get_api_key())
        return self._client

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "crypto") -> pl.DataFrame:
        """全部 TRADING 状态的 USDT 交易对 (现货+合约), 含 market 列。

        调用方 (crypto_sync) 按白名单过滤后写入 instruments_crypto。
        """
        _market_of(asset_type)
        rows: list[dict] = []
        for market in _MARKETS:
            try:
                info = self._get_client().exchange_info(market)
            except BinanceError as e:
                logger.warning("币安 exchangeInfo(%s) 失败: %s", market, e)
                continue
            for item in info.get("symbols") or []:
                wire = item.get("symbol")
                status = item.get("status")
                quote = item.get("quoteAsset")
                if not wire or status != "TRADING" or quote != _QUOTE_SUFFIX:
                    continue
                rows.append({
                    "symbol": join_symbol(market, wire),
                    "name": item.get("baseAsset") or wire,
                    "code": wire,
                    "exchange": "BINANCE",
                    "asset_type": "crypto",
                    "source": self.name,
                    "market": market,
                })
        return pl.DataFrame(rows).unique(subset=["symbol"], keep="last").sort("symbol")

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "crypto",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """crypto 日K → 内部契约。volume=基础币数量, amount=USDT, date=北京日期。"""
        _market_of(asset_type)
        if not symbols:
            return pl.DataFrame()
        end_ms = int((end_time or datetime.now()).timestamp() * 1000)
        start_ms = int((start_time or datetime.fromtimestamp(end_ms / 1000) - timedelta(days=365)).timestamp() * 1000)

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_KLINE_INTERVAL_S)
            parsed = split_symbol(symbol)
            if parsed is None:
                raise ProviderRequestError(f"币安日K: 无法识别标的 {symbol}")
            market, wire = parsed
            client = self._get_client()
            rows = []
            try:
                for bar in client.iter_klines(market, wire, "1d", start_ms, end_ms):
                    row = self._daily_row(symbol, bar)
                    if row is not None:
                        rows.append(row)
            except BinanceError as e:
                raise ProviderRequestError(f"币安日K拉取失败 {symbol}: {e}") from e
            df = normalize_daily(rows, source=self.name)
            if not df.is_empty():
                # normalize_daily 只保留 DAILY_COLS, 补 source 列供维表/审计
                frames.append(df.with_columns(pl.lit(self.name).alias("source")))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return (
            pl.concat(frames, how="diagonal_relaxed")
            if frames
            else pl.DataFrame()
        )

    @staticmethod
    def _daily_row(symbol: str, bar: list) -> dict | None:
        """币安 kline 行: [openTime, open, high, low, close, volume, closeTime,
        quoteVolume, trades, takerBase, takerQuote, ignore]。"""
        if len(bar) < 8:
            return None
        try:
            open_ms = int(bar[0])
        except (TypeError, ValueError):
            return None
        return {
            "symbol": symbol,
            "date": _beijing_date_of_ms(open_ms),
            "open": _to_float(bar[1]),
            "high": _to_float(bar[2]),
            "low": _to_float(bar[3]),
            "close": _to_float(bar[4]),
            "volume": _to_float(bar[5]),      # 基础币数量 (BTC 等)
            "amount": _to_float(bar[7]),      # 计价币数量 (USDT)
        }

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "crypto",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """crypto 1 分钟K → 内部契约。datetime=北京墙钟 naive (UTC+8h)。"""
        _market_of(asset_type)
        if not symbols or freq != "1m":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=2))
        end_ms = int(end_dt.timestamp() * 1000)
        start_ms = int(start_dt.timestamp() * 1000)

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_KLINE_INTERVAL_S)
            parsed = split_symbol(symbol)
            if parsed is None:
                raise ProviderRequestError(f"币安分钟K: 无法识别标的 {symbol}")
            market, wire = parsed
            client = self._get_client()
            rows = []
            try:
                for bar in client.iter_klines(market, wire, "1m", start_ms, end_ms):
                    if len(bar) < 8:
                        continue
                    try:
                        open_ms = int(bar[0])
                    except (TypeError, ValueError):
                        continue
                    rows.append({
                        "symbol": symbol,
                        "datetime": _beijing_naive_of_ms(open_ms),
                        "open": _to_float(bar[1]),
                        "high": _to_float(bar[2]),
                        "low": _to_float(bar[3]),
                        "close": _to_float(bar[4]),
                        "volume": _to_float(bar[5]),
                        "amount": _to_float(bar[7]),
                    })
            except BinanceError as e:
                raise ProviderRequestError(f"币安分钟K拉取失败 {symbol}: {e}") from e
            if rows:
                frames.append(pl.DataFrame(rows).sort("datetime"))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime ----
    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """24hr 快照 (现货+合约全量) → 内部 realtime records。失败软返回空列表。

        返回行数 ≈ 700 (现货+合约 USDT 对), 调用方按需过滤;
        24hr 滚动窗口口径与 A 股"当日快照"不同, 仅作参考价展示。
        """
        fetched_ms = int(time.time() * 1000)
        wanted = None
        if symbols:
            wanted = set(symbols)
        records: list[dict] = []
        for market in _MARKETS:
            try:
                rows = self._get_client().ticker_24hr(market)
            except BinanceError as e:
                logger.warning("币安 24hr 快照(%s)拉取失败: %s", market, e)
                continue
            for row in rows:
                wire = row.get("symbol")
                if not wire or not str(wire).endswith(_QUOTE_SUFFIX):
                    continue
                symbol = join_symbol(market, wire)
                if wanted is not None and symbol not in wanted:
                    continue
                rec = self._ticker_record(market, wire, row, fetched_ms)
                if rec is not None:
                    records.append(rec)
        if not records:
            logger.warning("币安 24hr 快照 0 条, 疑似接口结构变化或网络受限")
        else:
            logger.info("币安 24hr 快照拉取完成: %d 条", len(records))
        return records

    @staticmethod
    def _ticker_record(market: str, wire: str, row: dict, fetched_ms: int) -> dict | None:
        last = _to_float(row.get("lastPrice"))
        change_amount = _to_float(row.get("priceChange"))
        pct = _to_float(row.get("priceChangePercent"))
        prev_close = _to_float(row.get("prevClosePrice"))
        if prev_close is None and last is not None and change_amount is not None:
            prev_close = last - change_amount  # 合约无昨收列, 由 24hr 涨跌额推导
        return {
            "symbol": join_symbol(market, wire),
            "name": wire.removesuffix(_QUOTE_SUFFIX) if wire.endswith(_QUOTE_SUFFIX) else wire,
            "last_price": last,
            "prev_close": prev_close,
            "open": _to_float(row.get("openPrice")),
            "high": _to_float(row.get("highPrice")),
            "low": _to_float(row.get("lowPrice")),
            "volume": _to_float(row.get("volume")),      # 基础币数量
            "amount": _to_float(row.get("quoteVolume")),  # USDT
            "change_pct": pct / 100.0 if pct is not None else None,  # 百分数 → 小数制
            "change_amount": change_amount,
            "amplitude": None,
            "turnover_rate": None,  # crypto 无流通股本口径
            "timestamp": fetched_ms,
            "session": None,
        }

    # ---- 测试(设置页试拉) ----
    def get_funding_rates(self) -> list[dict]:
        """U 本位合约最新资金费率快照 (全部 USDT 永续) → 内部记录。

        资金费率是合约持仓成本的核心决策参考: 正值=多头付空头 (多头拥挤),
        负值反之。24h 快照口径外的新增只读端点, 供外部情绪面板使用。
        """
        try:
            rows = self._get_client().premium_index()
        except BinanceError as e:
            logger.warning("币安资金费率快照拉取失败: %s", e)
            return []
        fetched_ms = int(time.time() * 1000)
        records = []
        for row in rows:
            wire = row.get("symbol")
            if not wire or not str(wire).endswith(_QUOTE_SUFFIX):
                continue
            records.append({
                "symbol": join_symbol("futures", wire),
                "name": wire.removesuffix(_QUOTE_SUFFIX),
                "mark_price": _to_float(row.get("markPrice")),
                "funding_rate": _to_float(row.get("lastFundingRate")),
                "next_funding_time": row.get("nextFundingTime"),
                "timestamp": fetched_ms,
            })
        return records

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"币安插件未接入 {dataset} 数据集",
            }
        now = datetime.now()
        try:
            if dataset == "realtime":
                head = self.get_realtime(symbols=(symbols or ["BTCUSDT", "BTCUSDT.P"])[:5])[:5]
                return {
                    "provider": self.name, "dataset": dataset,
                    "rows": len(head), "columns": list(head[0].keys()) if head else [],
                    "preview": head,
                }
            syms = [s for s in (symbols or []) if s][:3] or ["BTCUSDT", "BTCUSDT.P"]
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
                preview_rows = df.head(5).to_dicts()
                for row in preview_rows:
                    for k, v in list(row.items()):
                        if isinstance(v, (date, datetime)):
                            row[k] = v.isoformat()
            else:
                df = self.get_minute(syms, now - timedelta(hours=2), now)
                preview_rows = df.head(5).to_dicts()
                for row in preview_rows:
                    if isinstance(row.get("datetime"), datetime):
                        row["datetime"] = row["datetime"].isoformat()
            return {
                "provider": self.name, "dataset": dataset,
                "rows": df.height,
                "columns": [c for c in (DAILY_COLS if dataset == "daily" else
                                        ["symbol", "datetime", "open", "high", "low",
                                         "close", "volume", "amount"]) if c in df.columns],
                "preview": preview_rows,
            }
        except (ProviderRequestError, BinanceError) as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
