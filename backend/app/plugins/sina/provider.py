"""新浪财经(vip.stock.finance.sina.com.cn)公开行情 provider。

方法签名对齐 custom.GenericHTTPProvider, 注入 custom loader 注册表后,
各 service 无需改动即可路由到本 provider。

实现数据集:
  - realtime  A 股全市场快照(沪深京, Market_Center.getHQNodeData hs_a 节点分页)
  - daily     A 股日K, CN_MarketData.getKLineData 单标的接口(不复权, 深度 ~1023 根)
未声明 minute/adj_factor/financial → provider_has_dataset 为 False, 自动回退 tickflow。

单位与口径 (CONTRIBUTING §3.1, 全部实测 2026-11):
  - hs_a 行: trade 现价 / settlement 昨收 / changepercent 百分数(-2.271=-2.271%) /
    volume 股 / amount 元 / turnoverratio 百分数; open/high/low 同名字段。
    无振幅字段 → amplitude 置 None, 不启发式伪造。
  - getKLineData: {day, open, high, low, close, volume(股)}; 无成交额列 → amount None;
    volume 股 → 手 (floor /100); datalen 上限 ~1023 根(约 4 年), 更深窗口不支持。
  - change_pct / turnover_rate 契约为小数制 → 显式 / 100。
  - 日K为原始不复权价; 前复权由 indicators.pipeline 用本地因子计算。

限频: 免费接口对连续请求敏感, 分页与单标的日K均内置节流;
全市场快照一轮 ~60 页, realtime 路由到本源时轮询间隔建议 ≥60s。
realtime 请求必须携带 Referer (hq.sinajs.cn 系接口的防盗链要求)。
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

_DATASETS = ("realtime", "daily")

_HEADERS = {"Referer": "https://finance.sina.com.cn/"}
_SNAPSHOT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/Market_Center.getHQNodeData"
)
_DAILY_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"

_PAGE_SIZE = 80
_PAGE_INTERVAL_S = 0.5
_DAILY_INTERVAL_S = 0.15
_DAILY_MAX_BARS = 1023  # getKLineData datalen 上限(约 4 年)


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测一次公开接口连通性。不抛异常。"""
    try:
        resp = httpx.get(
            _SNAPSHOT_URL,
            params={"page": 1, "num": 1, "sort": "symbol", "asc": 1,
                    "node": "hs_a", "symbol": "", "_s_r_a": "page"},
            headers=_HEADERS,
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        return False, f"公开接口不可达: {e}"
    if resp.status_code != 200:
        return False, f"接口返回 HTTP {resp.status_code}"
    if not resp.content or resp.text.strip() in ("null", ""):
        return False, "接口返回空数据(可能被限频), 稍后重试"
    return True, "ok"


@dataclass
class _Config:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "sina"
    display_name: str = "新浪财经"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct_to_decimal(value) -> float | None:
    """百分数数值 (2.04 = 2.04%) → 小数制 (0.0204)。缺失返回 None。"""
    v = _to_float(value)
    return v / 100.0 if v is not None else None


def _sina_symbol(symbol: str) -> str | None:
    """600519.SH → sh600519; 支持 sh/sz/bj 后缀。无法识别返回 None。"""
    if "." not in symbol:
        return None
    code, suffix = symbol.split(".", 1)
    suffix = suffix.upper()
    if suffix in ("SH", "SZ", "BJ") and code.isalnum():
        return f"{suffix.lower()}{code}"
    return None


def _snapshot_record(row: dict, fetched_ms: int) -> dict | None:
    """hs_a 行 → 内部 realtime record。字段缺失按契约置 None。

    row['symbol'] 为前缀直连格式(如 sh600519 / sz000001 / bj920000, 无分隔符)。
    """
    raw_symbol = row.get("symbol")
    if not raw_symbol or len(raw_symbol) < 3:
        return None
    prefix, code = raw_symbol[:2].upper(), raw_symbol[2:]
    if prefix not in ("SH", "SZ", "BJ") or not code.isalnum():
        return None
    volume = _to_float(row.get("volume"))
    return {
        "symbol": f"{code}.{prefix}",
        "name": row.get("name"),
        "last_price": _to_float(row.get("trade")),
        "prev_close": _to_float(row.get("settlement")),
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "volume": int(volume // 100) if volume is not None else None,  # 股 → 手
        "amount": _to_float(row.get("amount")),  # 元
        "change_pct": _pct_to_decimal(row.get("changepercent")),
        "change_amount": _to_float(row.get("pricechange")),
        "amplitude": None,  # 快照未提供, 不启发式计算
        "turnover_rate": _pct_to_decimal(row.get("turnoverratio")),
        "timestamp": fetched_ms,
        "session": None,
    }


class SinaProvider:
    """新浪财经数据源。realtime = A 股全市场快照(quote_service 全市场模式轮询调用)。"""

    name = "sina"
    builtin = True

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
            self._http = httpx.Client(headers=_HEADERS, timeout=15.0)
        return self._http

    def _get_json(self, url: str, params: dict) -> object:
        try:
            resp = self._get_http().get(url, params=params)
        except httpx.HTTPError as e:
            raise ProviderRequestError(f"新浪接口网络失败: {e}") from e
        if resp.status_code != 200:
            raise ProviderRequestError(f"新浪接口 HTTP {resp.status_code}: {url}")
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderRequestError(f"新浪接口响应不是 JSON(可能被限频): {url}") from e

    # ---- realtime ----
    def get_realtime(self) -> list[dict]:
        """A 股全市场快照 → 内部 realtime records。失败软返回空列表(不阻断轮询)。

        hs_a 节点单页上限 ~100, 内置 0.5s 页间隔, 一轮约 30s,
        轮询间隔建议 ≥60s (见模块 docstring)。
        """
        fetched_ms = int(time.time() * 1000)
        records: list[dict] = []
        page = 1
        try:
            while True:
                rows = self._get_json(_SNAPSHOT_URL, {
                    "page": page, "num": _PAGE_SIZE, "sort": "symbol", "asc": 1,
                    "node": "hs_a", "symbol": "", "_s_r_a": "page",
                })
                if not isinstance(rows, list) or not rows:
                    if page == 1:
                        # 首页即空: 大概率被限频(接口返回字面 null)或节点结构变化
                        logger.warning("新浪快照首页为空(可能被限频或接口结构变化)")
                        return []
                    break  # 正常翻到末页
                for row in rows:
                    rec = _snapshot_record(row, fetched_ms)
                    if rec is not None:
                        records.append(rec)
                page += 1
                time.sleep(_PAGE_INTERVAL_S)
        except ProviderRequestError as e:
            logger.warning("新浪实时行情拉取失败(已得 %d 条): %s", len(records), e)
            return []
        if not records:
            logger.warning("新浪快照 0 条, 疑似接口结构变化")
        else:
            logger.info("新浪实时行情拉取完成: %d 条", len(records))
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
        """A 股日K → 内部契约。原始价、volume 股→手(floor /100)、amount 无(None)。

        getKLineData 只支持 datalen 限量拉取(≈4 年), 请求窗口早于可覆盖范围时
        抛 ProviderRequestError (fail-closed), 不返回半段数据冒充完整。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            code = _sina_symbol(symbol)
            if code is None:
                raise ProviderRequestError(f"新浪日K: 无法识别标的 {symbol}")
            rows = self._daily_bars(symbol, code, start_d, end_d)
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

    def _daily_bars(self, symbol: str, code: str, start_d: date, end_d: date) -> list[dict]:
        span = (end_d - start_d).days + 5
        if span > _DAILY_MAX_BARS:
            raise ProviderRequestError(
                f"新浪日K: {symbol} 窗口 {start_d}~{end_d} 超出接口深度上限"
                f"(~{_DAILY_MAX_BARS} 根), 请缩小窗口或改用其他数据源"
            )
        data = self._get_json(_DAILY_URL, {
            "symbol": code, "scale": 240, "ma": "no", "datalen": span,
        })
        rows: list[dict] = []
        for bar in data or []:
            try:
                d = date.fromisoformat(str(bar.get("day"))[:10])
            except (TypeError, ValueError):
                continue
            if d < start_d or d > end_d:
                continue
            v = _to_float(bar.get("volume"))
            rows.append({
                "symbol": symbol,
                "date": d,
                "open": _to_float(bar.get("open")),
                "high": _to_float(bar.get("high")),
                "low": _to_float(bar.get("low")),
                "close": _to_float(bar.get("close")),
                "volume": int(v // 100) if v is not None else None,  # 股 → 手
                "amount": None,  # 接口不提供, 不伪造
            })
        return rows

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset == "realtime":
            try:
                rows = self._get_json(_SNAPSHOT_URL, {
                    "page": 1, "num": 5, "sort": "symbol", "asc": 1,
                    "node": "hs_a", "symbol": "", "_s_r_a": "page",
                })
            except ProviderRequestError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = [
                r for r in (_snapshot_record(row, int(time.time() * 1000))
                            for row in (rows if isinstance(rows, list) else []))
                if r
            ][:5]
            return {
                "provider": self.name, "dataset": dataset,
                "rows": len(head), "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        if dataset == "daily":
            syms = [s for s in (symbols or []) if s][:3] or ["600519.SH"]
            try:
                df = self.get_daily(syms, datetime.now() - timedelta(days=30), datetime.now())
            except ProviderRequestError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:
                for k, v in list(row.items()):
                    if isinstance(v, (date, datetime)):
                        row[k] = v.isoformat()
            return {
                "provider": self.name, "dataset": dataset,
                "rows": df.height, "columns": [c for c in DAILY_COLS if c in df.columns],
                "preview": head,
            }
        return {
            "provider": self.name, "dataset": dataset, "rows": 0,
            "error": f"新浪插件未接入 {dataset} 数据集(自动回退 TickFlow)",
        }
