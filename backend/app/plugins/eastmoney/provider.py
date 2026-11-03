"""东方财富(push2.eastmoney.com)公开行情 provider。

方法签名对齐 custom.GenericHTTPProvider(service 分流点按这套签名调用),
注入 custom loader 注册表后, 各 service 无需改动即可路由到本 provider。

实现数据集:
  - realtime  A 股全市场快照(沪深京, clist 接口分页, 沪深京 A 股一个目录接口全覆盖)
  - daily     A 股日K, 单标的 kline 接口, fqt=0 锁定不复权
  - minute    A 股 1 分钟K, 同 kline 接口 klt=1 (近端约 5 个交易日, 东财限频
              解除后需实测深度; 深度上限由 minute_history_days=5 声明)
未声明 adj_factor/financial → provider_has_dataset 为 False, 自动回退 tickflow。

单位与口径 (CONTRIBUTING §3.1, 不可凭字段名推断, 全部实测 2026-11):
  - clist: fltt=2 时 f2 现价 / f3 涨跌幅(百分数 20.04=+20.04%) / f4 涨跌额 /
    f5 成交量(手) / f6 成交额(元) / f7 振幅(百分数) / f8 换手率(百分数) /
    f15 high / f16 low / f17 open / f18 昨收; 缺值返回 "-", 不伪造。
  - kline: fields2=f51..f61 顺序为 日期,开,收,高,低,量(手),额(元),振幅%,涨跌幅%,涨跌额,换手率%。
  - change_pct / turnover_rate / amplitude 契约为小数制 → 显式 / 100。
  - 日K为原始不复权价; 前复权由 indicators.pipeline 用本地因子计算。

限频: 免费接口对连续请求敏感(实测连续数秒内多请求即被断连数分钟),
clist 分页与单标的日K均内置节流; 全市场快照一轮 ~60 页,
realtime 路由到本源时轮询间隔建议 ≥60s。
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
import polars as pl

from app.data_providers.base import ProviderRequestError
from app.data_providers.normalizer import DAILY_COLS, normalize_daily

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "daily", "minute")

_BASE = "https://push2.eastmoney.com"
_CLIST_PATH = "/api/qt/clist/get"
# 沪深 A 股 + 创业板/科创板 + 北交所 (实测 total ≈ 5900, 2026-11)
_FS_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
_KLINE_PATH = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}
_PAGE_SIZE = 100
_PAGE_INTERVAL_S = 0.5  # 页间隔; 免费接口限频敏感, 宁慢勿封
_DAILY_INTERVAL_S = 0.15
_KLINE_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测一次公开接口连通性。不抛异常。"""
    try:
        resp = httpx.get(
            f"{_BASE}{_CLIST_PATH}",
            params={"pn": 1, "pz": 1, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f3", "fs": _FS_A, "fields": "f12"},
            headers=_HEADERS,
            timeout=5.0,
            follow_redirects=True,
        )
    except httpx.HTTPError as e:
        return False, f"公开接口不可达: {e}"
    if resp.status_code != 200:
        return False, f"接口返回 HTTP {resp.status_code}"
    data = resp.json().get("data") if resp.content else None
    if not data or not data.get("total"):
        return False, "接口返回空数据, 结构可能已变化"
    return True, "ok"


@dataclass
class _Config:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "eastmoney"
    display_name: str = "东方财富"
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


def _pct_to_decimal(value) -> float | None:
    """百分数数值 (3.49 = 3.49%) → 小数制 (0.0349)。缺失返回 None。"""
    v = _to_float(value)
    return v / 100.0 if v is not None else None


def _secid(symbol: str) -> str | None:
    """600519.SH → 东财 secid (沪 1./深 0.)。无法识别返回 None。"""
    code = symbol.split(".")[0]
    suffix = symbol.split(".", 1)[1].upper() if "." in symbol else ""
    if suffix == "SH" or (not suffix and code.startswith(("6", "9"))):
        return f"1.{code}"
    if suffix in ("SZ", "BJ") or (not suffix and code.startswith(("0", "2", "3", "4", "8"))):
        return f"0.{code}"
    return None


def _symbol_of_code(code: str) -> str | None:
    """东财 6 位代码 → 内部 symbol (带交易所后缀)。

    北交所: 43/83/87/88/920 开头; 沪: 6/9 开头; 其余归深。
    """
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    return f"{code}.SZ"


def _snapshot_record(row: dict, fetched_ms: int) -> dict | None:
    """clist 行 → 内部 realtime record。价格类字段缺失时按契约置 None。"""
    code = row.get("f12")
    if not code:
        return None
    symbol = _symbol_of_code(str(code))
    if symbol is None:
        return None
    return {
        "symbol": symbol,
        "name": row.get("f14"),
        "last_price": _to_float(row.get("f2")),
        "prev_close": _to_float(row.get("f18")),
        "open": _to_float(row.get("f17")),
        "high": _to_float(row.get("f15")),
        "low": _to_float(row.get("f16")),
        "volume": _to_float(row.get("f5")),  # 手
        "amount": _to_float(row.get("f6")),  # 元
        "change_pct": _pct_to_decimal(row.get("f3")),
        "change_amount": _to_float(row.get("f4")),
        "amplitude": _pct_to_decimal(row.get("f7")),
        "turnover_rate": _pct_to_decimal(row.get("f8")),
        "timestamp": fetched_ms,
        "session": None,
    }


class EastmoneyProvider:
    """东方财富数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "eastmoney"
    builtin = True
    # klt=1 分钟K接口近端深度(东财常用口径约 5 个交易日, 待限频解除后实测校准)
    minute_history_days = 5

    def __init__(self) -> None:
        self.config = _Config()
        self._http: httpx.Client | None = None

    def close(self) -> None:  # loader.load_all 重建注册表时会对每个 provider 调 close
        if self._http is not None:
            with contextlib.suppress(Exception):
                self._http.close()
            self._http = None

    def _get_http(self) -> httpx.Client:
        if self._http is None:
            # follow_redirects: 东财限流/风控时偶发 302, 跟随后通常恢复或明确失败;
            # 不跟随时表现为误导性的 "HTTP 302" 错误
            self._http = httpx.Client(headers=_HEADERS, timeout=15.0, follow_redirects=True)
        return self._http

    def _get_json(self, url: str, params: dict) -> dict:
        try:
            resp = self._get_http().get(url, params=params)
        except httpx.HTTPError as e:
            raise ProviderRequestError(f"东方财富接口网络失败: {e}") from e
        if resp.status_code != 200:
            raise ProviderRequestError(f"东方财富接口 HTTP {resp.status_code}: {url}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderRequestError(f"东方财富接口响应不是 JSON: {url}") from e
        if not payload or payload.get("data") is None:
            raise ProviderRequestError(f"东方财富接口返回空数据: {url}")
        return payload["data"]

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """A 股全市场快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。

        clist 单页上限 100, 全市场 ~60 页; 内置 0.5s 页间隔, 一轮约 30s,
        轮询间隔建议 ≥60s (见模块 docstring)。
        """
        fetched_ms = int(time.time() * 1000)
        records: list[dict] = []
        page = 1
        total = None
        try:
            while True:
                data = self._get_json(f"{_BASE}{_CLIST_PATH}", {
                    "pn": page, "pz": _PAGE_SIZE, "po": 1, "np": 1, "fltt": 2,
                    "invt": 2, "fid": "f12", "fs": _FS_A,
                    "fields": "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18",
                })
                rows = data.get("diff") or []
                if not rows:
                    break
                for row in rows:
                    rec = _snapshot_record(row, fetched_ms)
                    if rec is not None:
                        records.append(rec)
                if total is None:
                    total = int(data.get("total") or 0)
                if total and len(records) >= total:
                    break
                page += 1
                time.sleep(_PAGE_INTERVAL_S)
        except ProviderRequestError as e:
            logger.warning("东方财富实时行情拉取失败(已得 %d 条): %s", len(records), e)
            return []
        if not records:
            logger.warning("东方财富快照 0 条, 疑似接口结构变化")
        else:
            logger.info("东方财富实时行情拉取完成: %d 条", len(records))
        return records

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股日K → 内部契约。原始价(fqt=0)、volume 手、amount 元。

        单标的接口, 逐标的节流; 任一标的失败即抛 ProviderRequestError (fail-closed),
        由 kline_sync 标记失败, 不返回半段数据冒充完整。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        beg = start_dt.strftime("%Y%m%d")
        end = end_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            secid = _secid(symbol)
            if secid is None:
                raise ProviderRequestError(f"东方财富日K: 无法识别标的 {symbol}")
            data = self._get_json(_KLINE_PATH, {
                "secid": secid, "klt": 101, "fqt": 0,
                "beg": beg, "end": end,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": _KLINE_FIELDS2,
            })
            rows = []
            for line in data.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 11:
                    continue
                rows.append({
                    "symbol": symbol,
                    "date": date.fromisoformat(parts[0]),
                    "open": _to_float(parts[1]),
                    "close": _to_float(parts[2]),
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "volume": _to_float(parts[5]),  # 手
                    "amount": _to_float(parts[6]),  # 元
                })
            df = normalize_daily(rows, source=self.name)
            if not df.is_empty():
                frames.append(df)
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return (
            pl.concat(frames, how="diagonal_relaxed")
            if frames
            else pl.DataFrame()
        )

    # ---- minute ----
    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股 1 分钟K (klt=1) → 内部契约。北京墙钟 naive、volume 手。

        与日K同一 kline 端点, 行结构一致; 分钟行首列为 "YYYY-MM-DD HH:MM"
        北京墙钟 (接口即北京时间, 无时区换算)。窗口超出近端深度时由
        klines 天然截断, 返回可得范围 (minute_history_days=5 声明浅源)。
        """
        if not symbols or asset_type != "stock" or freq != "1m":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=5))
        beg = start_dt.strftime("%Y%m%d")
        end = end_dt.strftime("%Y%m%d")

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            secid = _secid(symbol)
            if secid is None:
                raise ProviderRequestError(f"东方财富分钟K: 无法识别标的 {symbol}")
            data = self._get_json(_KLINE_PATH, {
                "secid": secid, "klt": 1, "fqt": 0,
                "beg": beg, "end": end,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": _KLINE_FIELDS2,
            })
            rows = []
            for line in data.get("klines") or []:
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                try:
                    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                rows.append({
                    "symbol": symbol,
                    "datetime": dt,  # 北京墙钟 naive (接口即北京时间)
                    "open": _to_float(parts[1]),
                    "close": _to_float(parts[2]),
                    "high": _to_float(parts[3]),
                    "low": _to_float(parts[4]),
                    "volume": _to_float(parts[5]),  # 手
                    "amount": _to_float(parts[6]),  # 元
                })
            df = pl.DataFrame(rows) if rows else pl.DataFrame()
            if not df.is_empty():
                frames.append(df.sort("datetime"))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "realtime":
            try:
                data = self._get_json(f"{_BASE}{_CLIST_PATH}", {
                    "pn": 1, "pz": 5, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                    "fid": "f12", "fs": _FS_A,
                    "fields": "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18",
                })
            except ProviderRequestError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = [
                r for r in (_snapshot_record(row, int(time.time() * 1000))
                            for row in (data.get("diff") or []))
                if r
            ][:5]
            return {
                "provider": self.name, "dataset": dataset,
                "rows": int(data.get("total") or len(head)),
                "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset in ("daily", "minute"):
            syms = [s for s in (symbols or [])][:3] or ["600519.SH"]
            try:
                if dataset == "daily":
                    df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
                    preview_rows = df.head(5).to_dicts()
                    for row in preview_rows:
                        for k, v in list(row.items()):
                            if isinstance(v, (date, datetime)):
                                row[k] = v.isoformat()
                    cols = [c for c in DAILY_COLS if c in df.columns]
                else:
                    df = self.get_minute(syms, datetime.now() - timedelta(days=2), datetime.now())
                    preview_rows = df.head(5).to_dicts()
                    for row in preview_rows:
                        if isinstance(row.get("datetime"), datetime):
                            row["datetime"] = row["datetime"].isoformat()
                    cols = list(df.columns)
            except ProviderRequestError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            return {
                "provider": self.name, "dataset": dataset,
                "rows": df.height, "columns": cols,
                "preview": preview_rows,
            }
        return {
            "provider": self.name, "dataset": dataset, "rows": 0,
            "error": f"东方财富插件未接入 {dataset} 数据集(自动回退 TickFlow)",
        }
