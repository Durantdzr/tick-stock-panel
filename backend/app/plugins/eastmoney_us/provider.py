"""东方财富美股 (NASDAQ/NYSE/AMEX) 公开行情 provider。

专用市场数据源: plugin.yaml 声明 asset_types: [us], 不进入 A 股 7 能力路由
候选; 美股同步服务直接经 custom.loader.get_provider("eastmoney_us") 取数,
fail-closed。子类化 A 股 eastmoney provider 复用 HTTP 层与 kline 解析,
仅覆盖市场目录 / secid 映射 / symbol 归一。

实现数据集 (仅 asset_type="us"):
  - realtime  全市场快照 (clist 分页, fs=m:105,m:106,m:107, 约 1.4 万只)
  - daily     日K (klt=101, fqt=0), date=美东交易日 (接口原生)
  - minute    1 分钟K (klt=1), datetime=北京墙钟 naive (接口原生, 符合 §3.3)
  - get_instruments("us") 全市场维表 (含 exchange 列: NASDAQ/NYSE/AMEX)

symbol 与 secid 映射:
  - 内部 symbol = 美股代码原文 (如 AAPL / TSLA, 大写字母, 无交易所后缀);
    交易所归属由 instruments_us.exchange 列承载。
  - kline 端点 secid 需市场前缀 (105=NASDAQ / 106=NYSE / 107=AMEX):
    provider 内置 ticker→市场 memo, 未知标的按 105→106→107 探测
    (探测 = 一次小窗口 kline 请求, 命中后缓存), 避免为单标的拉全市场目录。

单位与口径 (CONTRIBUTING §3.1, 与 A 股契约的差异全部显式声明):
  - volume = 股 (非"手", 不换算); amount = 美元 (非"元") — trading_rules
    volume_unit=share 声明, 下游按相对量消费。
  - change_pct / amplitude / turnover_rate 契约小数制 (clist 百分数 → /100);
    美股换手率若上游未提供则置 None, 不伪造。
  - 日K fqt=0 原始价; 美股有拆合股但一期不建模复权 (东财美股无 adj_factor
    端点), 拆股日连续性缺口属已知限制 (见 docs/us-market.md)。
  - 分钟 datetime 接口原生北京墙钟 (美东 09:31 ≡ 北京 22:31), 直接透传。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import DAILY_COLS, INSTRUMENT_COLS, normalize_daily
from app.plugins.eastmoney.provider import (
    _CLIST_PATH,
    _DAILY_INTERVAL_S,
    _KLINE_FIELDS2,
    _KLINE_PATH,
    _PAGE_INTERVAL_S,
    _PAGE_SIZE,
    EastmoneyProvider,
    _pct_to_decimal,
    _to_float,
)

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "daily", "minute")

_FS_US = "m:105,m:106,m:107"  # NASDAQ / NYSE / AMEX
_MARKET_LABELS = {105: "NASDAQ", 106: "NYSE", 107: "AMEX"}
_MARKET_PROBE_ORDER = (105, 106, 107)
_CLIST_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f13,f14,f15,f16,f17,f18"
_ULIST_PATH = "https://push2.eastmoney.com/api/qt/ulist.np/get"

_MINUTE_HISTORY_DAYS = 5  # klt=1 近端深度 (东财常用口径, 与 A 股同源)


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测一次美股 kline 连通性。不抛异常。"""
    try:
        probe = EastmoneyUSProvider()
        data = probe._fetch_kline("105.AAPL", "101", "20260101", "20260110")
    except Exception as e:  # noqa: BLE001
        return False, f"公开接口不可达: {e}"
    if not isinstance(data, dict) or not data.get("klines"):
        return False, "接口返回空数据, 结构可能已变化"
    return True, "ok"


@dataclass
class _USConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "eastmoney_us"
    display_name: str = "东方财富·美股"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _require_us(asset_type: str) -> None:
    if asset_type != "us":
        raise ProviderRequestError(
            f"东方财富美股数据源仅提供美股(us)数据, 收到 asset_type={asset_type!r}"
        )


class EastmoneyUSProvider(EastmoneyProvider):
    """东方财富美股数据源 (us 专用市场源)。"""

    name = "eastmoney_us"
    builtin = True

    def __init__(self) -> None:
        super().__init__()
        self.config = _USConfig()
        self._market_memo: dict[str, int] = {}  # ticker → 105/106/107

    # ---- instruments ----
    def _get_json_retry(self, url: str, params: dict, *, attempts: int = 3) -> dict:
        """页级重试: 免费接口限频页偶发空响应, 退避后通常恢复 (宁慢勿失败)。"""
        last_err: ProviderRequestError | None = None
        for attempt in range(attempts):
            try:
                return self._get_json(url, params)
            except ProviderRequestError as e:
                last_err = e
                time.sleep(2.0 * (attempt + 1))  # 2s / 4s / 6s 退避
        raise last_err  # type: ignore[misc]

    def fetch_instruments_page(self, page: int) -> tuple[list[dict], int]:
        """拉取一页全市场目录 (主机轮换 + 页级重试) → (标准化行, 全市场总数)。"""
        host = f"{(page % 20) + 1}.push2.eastmoney.com"
        data = self._get_json_retry(f"https://{host}{_CLIST_PATH}", {
            "pn": page, "pz": _PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
            "invt": 2, "fid": "f12", "fs": _FS_US, "fields": _CLIST_FIELDS,
        })
        diff = data.get("diff") or []
        rows: list[dict] = []
        for row in diff:
            ticker = str(row.get("f12") or "").strip()
            if not ticker or not ticker.replace("-", "").replace(".", "").isalnum():
                continue
            market = row.get("f13")
            rows.append({
                "symbol": ticker,
                "name": row.get("f14"),
                "code": ticker,
                "exchange": _MARKET_LABELS.get(market),
                "asset_type": "us",
                "source": self.name,
            })
            if market in _MARKET_LABELS:
                self._market_memo[ticker.upper()] = market
        total = int(data.get("total") or 0)
        return rows, total

    def get_instruments(self, asset_type: str = "us") -> pl.DataFrame:
        """全市场美股维表 (单次全量扫描; 长扫描易触发限频, 同步服务走断点续传)。"""
        _require_us(asset_type)
        rows: list[dict] = []
        page = 1
        total = None
        while True:
            page_rows, page_total = self.fetch_instruments_page(page)
            if page_total:
                total = page_total
            if not page_rows:
                break
            rows.extend(page_rows)
            if total and len(rows) >= total:
                break
            page += 1
            time.sleep(1.0)  # 139 页长扫描, 1s 间隔降低限频概率
        if not rows:
            raise ProviderRequestError("东方财富美股目录为空, 疑似接口结构变化")
        logger.info("东方财富美股目录拉取完成: %d 只", len(rows))
        return pl.DataFrame(rows).select(INSTRUMENT_COLS).unique(
            subset=["symbol"], keep="last").sort("symbol")

    # ---- secid 解析 (探测式, 与 A 股前缀规则不同) ----
    def _secid_for(self, ticker: str) -> str:
        memo = self._market_memo.get(ticker.upper())
        if memo:
            return f"{memo}.{ticker.upper()}"
        for market in _MARKET_PROBE_ORDER:
            probe = self._fetch_kline(
                f"{market}.{ticker.upper()}", "101", "20260101", "20260110", fail_open=True,
            )
            if probe is not None:
                self._market_memo[ticker.upper()] = market
                return f"{market}.{ticker.upper()}"
        raise ProviderRequestError(f"东方财富美股: 无法定位 {ticker} 的市场 (105/106/107 均无数据)")

    def _fetch_kline(self, secid: str, klt: str, beg: str, end: str, *, fail_open: bool = False):
        try:
            return self._get_json(_KLINE_PATH, {
                "secid": secid, "klt": klt, "fqt": 0, "beg": beg, "end": end,
                "fields1": "f1,f2,f3,f4,f5,f6", "fields2": _KLINE_FIELDS2,
            })
        except ProviderRequestError:
            if fail_open:
                return None
            raise

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "us",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """美股日K → 内部契约。原始价(fqt=0)、volume 股、amount 美元、date=美东交易日。"""
        _require_us(asset_type)
        if not symbols:
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        beg, end = start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            secid = self._secid_for(symbol)
            data = self._fetch_kline(secid, "101", beg, end)
            rows = []
            for line in data.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                rows.append({
                    "symbol": symbol,
                    "date": date.fromisoformat(parts[0]),  # 美东交易日 (接口原生)
                    "open": _to_float(parts[1]),
                    "close": _to_float(parts[2]),
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "volume": _to_float(parts[5]),  # 股 (美股口径, 不换算)
                    "amount": _to_float(parts[6]),  # 美元
                })
            df = normalize_daily(rows, source=self.name)
            if not df.is_empty():
                frames.append(df.with_columns(pl.lit(self.name).alias("source")))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "us",
        freq: str = "1m",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """美股 1 分钟K → 内部契约。datetime=北京墙钟 naive (接口原生)。"""
        _require_us(asset_type)
        if not symbols or freq != "1m":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=_MINUTE_HISTORY_DAYS))
        beg, end = start_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            secid = self._secid_for(symbol)
            data = self._fetch_kline(secid, "1", beg, end)
            rows = []
            for line in data.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                try:
                    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if dt < start_dt or dt > end_dt:
                    continue
                rows.append({
                    "symbol": symbol,
                    "datetime": dt,  # 北京墙钟 naive (接口原生, 美东+12/13h)
                    "open": _to_float(parts[1]),
                    "close": _to_float(parts[2]),
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "volume": _to_float(parts[5]),  # 股
                    "amount": _to_float(parts[6]),  # 美元
                })
            if rows:
                frames.append(pl.DataFrame(rows).sort("datetime"))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- realtime ----
    def get_realtime(self, universes=None, symbols=None):
        """全市场快照 (clist 分页) → 内部 realtime records。失败软返回空列表。

        symbols 指定时优先走单点端点逐只补拉 (clist 按代码排序分页,
        目标分散时全量扫描效率低); 无 symbols 时全市场分页。
        """
        fetched_ms = int(time.time() * 1000)
        wanted = {s.upper() for s in symbols} if symbols else None
        records: list[dict] = []
        if wanted is None:
            page = 1
            total = None
            try:
                while True:
                    host = f"{(page % 20) + 1}.push2.eastmoney.com"
                    data = self._get_json(f"https://{host}{_CLIST_PATH}", {
                        "pn": page, "pz": _PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
                        "invt": 2, "fid": "f12", "fs": _FS_US, "fields": _CLIST_FIELDS,
                    })
                    diff = data.get("diff") or []
                    if not diff:
                        break
                    for row in diff:
                        rec = self._snapshot_record(row, fetched_ms)
                        if rec is not None:
                            records.append(rec)
                    if total is None:
                        total = int(data.get("total") or 0)
                    if total and len(records) >= total:
                        break
                    page += 1
                    time.sleep(_PAGE_INTERVAL_S)
            except ProviderRequestError as e:
                logger.warning("东方财富美股快照拉取失败(已得 %d 条): %s", len(records), e)
                return []
        else:
            secids: list[str] = []
            for ticker in sorted(wanted):
                try:
                    secids.append(self._secid_for(ticker))
                except ProviderRequestError as e:
                    logger.warning("东方财富美股快照: %s", e)
            # ulist.np 批量端点, 字段表与 clist 同构, 每批 ~50 只
            for i in range(0, len(secids), 50):
                if i:
                    time.sleep(_PAGE_INTERVAL_S)
                batch = secids[i : i + 50]
                try:
                    data = self._get_json(_ULIST_PATH, {
                        "secids": ",".join(batch), "invt": 2, "fltt": 2,
                        "fields": _CLIST_FIELDS,
                    })
                    for row in data.get("diff") or []:
                        rec = self._snapshot_record(row, fetched_ms)
                        if rec is not None:
                            records.append(rec)
                except ProviderRequestError as e:
                    logger.warning("东方财富美股批量快照失败(第 %d 批): %s", i // 50 + 1, e)
        if not records:
            logger.warning("东方财富美股快照 0 条, 疑似接口结构变化或网络受限")
        else:
            logger.info("东方财富美股快照拉取完成: %d 条", len(records))
        return records

    def _snapshot_record(self, row: dict, fetched_ms: int) -> dict | None:
        ticker = str(row.get("f12") or "").strip()
        if not ticker:
            return None
        return {
            "symbol": ticker,
            "name": row.get("f14"),
            "last_price": _to_float(row.get("f2")),
            "prev_close": _to_float(row.get("f18")),
            "open": _to_float(row.get("f17")),
            "high": _to_float(row.get("f15")),
            "low": _to_float(row.get("f16")),
            "volume": _to_float(row.get("f5")),  # 股 (美股口径, 不换算)
            "amount": _to_float(row.get("f6")),  # 美元
            "change_pct": _pct_to_decimal(row.get("f3")),
            "change_amount": _to_float(row.get("f4")),
            "amplitude": _pct_to_decimal(row.get("f7")),
            "turnover_rate": _pct_to_decimal(row.get("f8")),
            "timestamp": fetched_ms,
            "session": None,
        }

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "error": f"东方财富美股插件未接入 {dataset} 数据集(自动回退 TickFlow)",
            }
        now = datetime.now()
        try:
            if dataset == "realtime":
                head = self.get_realtime(symbols=(symbols or ["AAPL", "MSFT"])[:5])[:5]
                return {
                    "provider": self.name, "dataset": dataset,
                    "rows": len(head), "columns": list(head[0].keys()) if head else [],
                    "preview": head,
                }
            syms = [s for s in (symbols or []) if s][:3] or ["AAPL"]
            if dataset == "daily":
                df = self.get_daily(syms, now - timedelta(days=30), now)
                preview_rows = df.head(5).to_dicts()
                for row in preview_rows:
                    for k, v in list(row.items()):
                        if isinstance(v, (date, datetime)):
                            row[k] = v.isoformat()
                cols = [c for c in DAILY_COLS if c in df.columns]
            else:
                df = self.get_minute(syms, now - timedelta(days=2), now)
                preview_rows = df.head(5).to_dicts()
                for row in preview_rows:
                    if isinstance(row.get("datetime"), datetime):
                        row["datetime"] = row["datetime"].isoformat()
                cols = list(df.columns)
            return {
                "provider": self.name, "dataset": dataset,
                "rows": df.height, "columns": cols, "preview": preview_rows,
            }
        except ProviderRequestError as e:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
