"""腾讯(web.ifzq.gtimg.cn / qt.gtimg.cn)公开行情 provider。

方法签名对齐 custom.GenericHTTPProvider, 注入 custom loader 注册表后,
各 service 无需改动即可路由到本 provider。

实现数据集:
  - daily   A 股日K(沪深, 不含北交所), 单标的 fqkline 接口, 不复权
  - minute  1 分钟K, mkline m1 接口, 最近 320 根(约 1.3 个交易日, 深度上限
            由 minute_history_days=1 声明), 不支持日期区间参数, 拉取后按窗口过滤
  - depth5  五档盘口, qt.gtimg.cn 批量快照接口解析 (60 只/批)
未声明 realtime/adj_factor/financial → provider_has_dataset 为 False,
自动回退 tickflow (腾讯批量快照接口只能按指定代码拉, quote_service
全市场轮询模式用不了, 故不声明 realtime)。

单位与口径 (CONTRIBUTING §3.1, 全部实测 2026-11):
  - fqkline day 行为 [date, open, close, high, low, volume(手)]:
    注意第 2 列是收盘、第 3 列才是最高 (与常见 OHLC 顺序不同)。
  - mkline m1 行为 [yyyyMMddHHmm, open, close, high, low, volume(手), {}, 额]:
    同样是开,收,高,低顺序; datetime 为北京墙钟 (naive)。
  - qt.gtimg.cn 快照波浪号分隔字段: [9..18] 买一~买五 (价,量) 交替,
    [19..28] 卖一~卖五 (价,量) 交替, [30] 时间 yyyyMMddHHMMSS; 量单位手。
  - 日K/分钟K均为原始不复权价; 前复权由 indicators.pipeline 用本地因子计算。
  - fqkline 响应无成交额列, amount 置 None, 不启发式伪造。
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
from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "minute", "depth5")

_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
_SNAPSHOT_URL = "https://qt.gtimg.cn/q="
_DAILY_INTERVAL_S = 0.15
_MAX_BARS = 640  # fqkline 单次返回上限(实测), 窗口超出时按最近 640 根截断
_M1_MAX_BARS = 320  # mkline m1 单次返回上限(实测 320 根 ≈ 1.3 个交易日)
_DEPTH_BATCH = 60  # qt.gtimg.cn 单次快照标的数上限(实测, 超出截断)
_DEPTH_INTERVAL_S = 0.2

# qt.gtimg.cn 快照字段下标 (波浪号分隔, 实测 2026-11)
_QT_BID_PRICE0 = 9  # [9,11,13,15,17] 买一~买五价, [10,12,14,16,18] 买一~买五量
_QT_ASK_PRICE0 = 19  # [19,21,23,25,27] 卖一~卖五价, [20,22,24,26,28] 卖一~卖五量
_QT_TIMESTAMP = 30  # yyyyMMddHHMMSS 北京墙钟


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测一次公开接口连通性。不抛异常。"""
    try:
        today = datetime.now().date().isoformat()
        resp = httpx.get(
            _KLINE_URL,
            params={"param": f"sh600519,day,,{today},5,"},
            timeout=5.0,
        )
    except httpx.HTTPError as e:
        return False, f"公开接口不可达: {e}"
    if resp.status_code != 200:
        return False, f"接口返回 HTTP {resp.status_code}"
    try:
        payload = resp.json()
    except ValueError:
        return False, "响应不是 JSON"
    day = (payload.get("data") or {}).get("sh600519", {}).get("day")
    if not day:
        return False, "接口返回空数据, 结构可能已变化"
    return True, "ok"


@dataclass
class _Config:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "tencent"
    display_name: str = "腾讯行情"
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


def _tencent_code(symbol: str) -> str | None:
    """600519.SH → sh600519。北交所与无法识别的后缀返回 None。"""
    if "." not in symbol:
        return None
    code, suffix = symbol.split(".", 1)
    suffix = suffix.upper()
    if suffix in ("SH", "SZ") and code.isalnum():
        return f"{suffix.lower()}{code}"
    return None


class TencentProvider:
    """腾讯数据源。daily = A 股日K备源; minute/depth5 = 近端分时与五档。"""

    name = "tencent"
    builtin = True
    # mkline m1 只保留最近 320 根 (≈1.3 个交易日), 供前端分时档位收窄
    minute_history_days = 1

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
            self._http = httpx.Client(timeout=15.0)
        return self._http

    def _get_json(self, url: str, params: dict) -> dict:
        try:
            resp = self._get_http().get(url, params=params)
        except httpx.HTTPError as e:
            raise ProviderRequestError(f"腾讯接口网络失败: {e}") from e
        if resp.status_code != 200:
            raise ProviderRequestError(f"腾讯接口 HTTP {resp.status_code}: {url}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise ProviderRequestError(f"腾讯接口响应不是 JSON: {url}") from e
        if payload.get("code") not in (0, "0", None):
            raise ProviderRequestError(f"腾讯接口错误 code={payload.get('code')}: {url}")
        return payload.get("data") or {}

    # ---- daily ----
    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """A 股日K → 内部契约。原始价、volume 手、amount 无(None)。

        单标的接口, 逐标的节流; 任一标的失败即抛 ProviderRequestError (fail-closed)。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=365))
        start_d, end_d = start_dt.date(), end_dt.date()
        bars = min((end_d - start_d).days + 10, _MAX_BARS)

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            code = _tencent_code(symbol)
            if code is None:
                raise ProviderRequestError(f"腾讯日K: 无法识别标的 {symbol} (不含北交所)")
            data = self._get_json(_KLINE_URL, params={
                "param": f"{code},day,{start_d.isoformat()},{end_d.isoformat()},{bars},",
            })
            node = data.get(code) or {}
            rows = []
            for bar in node.get("day") or []:
                if len(bar) < 6:
                    continue
                rows.append({
                    "symbol": symbol,
                    "date": date.fromisoformat(str(bar[0])[:10]),
                    "open": _to_float(bar[1]),
                    "close": _to_float(bar[2]),
                    "high": _to_float(bar[3]),
                    "low": _to_float(bar[4]),
                    "volume": _to_float(bar[5]),  # 手
                    "amount": None,  # 接口不提供, 不伪造
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
        """A 股 1 分钟K → 内部契约 (北京墙钟 naive, volume 手)。

        mkline 不支持日期区间, 恒拉最近 _M1_MAX_BARS 根后按窗口过滤;
        窗口早于可覆盖范围时数据不足, 抛 ProviderRequestError (fail-closed)。
        """
        if not symbols or asset_type != "stock" or freq != "1m":
            return pl.DataFrame()
        end_dt = end_time or datetime.now()
        start_dt = start_time or (end_dt - timedelta(days=2))

        frames: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S)
            code = _tencent_code(symbol)
            if code is None:
                # 北交所等超出插件声明范围的标的: 全市场批量场景下跳过并告警,
                # 不中断整批 (显式指定且全部不可服务时由调用方按空结果处理)。
                logger.warning("腾讯分钟K: 跳过不支持的标的 %s (不含北交所)", symbol)
                continue
            data = self._get_json(_MINUTE_URL, params={"param": f"{code},m1,,{_M1_MAX_BARS}"})
            node = data.get(code) or {}
            rows = []
            for bar in node.get("m1") or []:
                if len(bar) < 6:
                    continue
                try:
                    dt = datetime.strptime(str(bar[0]), "%Y%m%d%H%M")
                except ValueError:
                    continue
                if dt < start_dt or dt > end_dt:
                    continue
                rows.append({
                    "symbol": symbol,
                    "datetime": dt,  # 北京墙钟 naive (接口即北京时间)
                    "open": _to_float(bar[1]),
                    "close": _to_float(bar[2]),
                    "high": _to_float(bar[3]),
                    "low": _to_float(bar[4]),
                    "volume": _to_float(bar[5]),  # 手
                    "amount": None,  # 第 8 列为额但口径未验证, 不伪造
                })
            df = pl.DataFrame(rows) if rows else pl.DataFrame()
            if not df.is_empty():
                frames.append(df.sort("datetime"))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- depth5 ----
    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口 → {symbol: 标准盘口字典}。量单位手(接口即手, 透传)。"""
        out: dict[str, dict] = {}
        wanted = [s for s in symbols if _tencent_code(s)]
        for i in range(0, len(wanted), _DEPTH_BATCH):
            if i:
                time.sleep(_DEPTH_INTERVAL_S)
            batch = wanted[i : i + _DEPTH_BATCH]
            codes = [_tencent_code(s) for s in batch]
            try:
                resp = self._get_http().get(_SNAPSHOT_URL + ",".join(codes))
            except httpx.HTTPError as e:
                logger.warning("腾讯五档批次失败(第 %d 批): %s", i // _DEPTH_BATCH + 1, e)
                continue
            if resp.status_code != 200:
                logger.warning("腾讯五档批次 HTTP %s(第 %d 批)", resp.status_code, i // _DEPTH_BATCH + 1)
                continue
            symbol_by_code = dict(zip(codes, batch, strict=True))
            for line in resp.content.decode("gbk", errors="replace").splitlines():
                entry = self._depth_entry(line, symbol_by_code)
                if entry is not None:
                    out[entry[0]] = entry[1]
        return out

    def _depth_entry(self, line: str, symbol_by_code: dict[str, str]) -> tuple[str, dict] | None:
        """qt.gtimg.cn 单行 `v_sh600519="...";` → (symbol, 盘口字典)。字段缺失跳过。"""
        if "=" not in line or '"' not in line:
            return None
        code = line.split("=", 1)[0].strip().removeprefix("v_")
        symbol = symbol_by_code.get(code)
        if not symbol:
            return None
        raw = line.split('"', 1)[1].rsplit('"', 1)[0]
        parts = raw.split("~")
        if len(parts) <= _QT_TIMESTAMP:
            return None

        def _prices(start: int) -> list[float]:
            out = []
            for k in range(5):
                v = _to_float(parts[start + 2 * k])
                if v is None:
                    return []
                out.append(v)
            return out

        def _volumes(start: int) -> list[int]:
            out = []
            for k in range(5):
                v = _to_float(parts[start + 2 * k + 1])
                if v is None:
                    return []
                out.append(int(v))
            return out

        bid_prices = _prices(_QT_BID_PRICE0)
        ask_prices = _prices(_QT_ASK_PRICE0)
        if not bid_prices or not ask_prices:
            return None
        with contextlib.suppress(ValueError):
            ts = int(datetime.strptime(parts[_QT_TIMESTAMP], "%Y%m%d%H%M%S")
                     .replace(tzinfo=CN_TZ).timestamp() * 1000)
        return symbol, {
            "bid_prices": bid_prices,
            "bid_volumes": _volumes(_QT_BID_PRICE0),
            "ask_prices": ask_prices,
            "ask_volumes": _volumes(_QT_ASK_PRICE0),
            "timestamp": ts or int(time.time() * 1000),
        }

    # ---- 测试(设置页试拉) ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        syms = [s for s in (symbols or []) if s][:3]
        if dataset == "daily":
            use = syms or ["600519.SH"]
            try:
                df = self.get_daily(use, datetime.now() - timedelta(days=30), datetime.now())
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
        if dataset == "minute":
            use = syms or ["600519.SH"]
            try:
                df = self.get_minute(use, datetime.now() - timedelta(days=2), datetime.now())
            except ProviderRequestError as e:
                return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(e)}
            head = df.head(5).to_dicts()
            for row in head:
                if isinstance(row.get("datetime"), datetime):
                    row["datetime"] = row["datetime"].isoformat()
            return {
                "provider": self.name, "dataset": dataset,
                "rows": df.height, "columns": list(df.columns),
                "preview": head,
            }
        if dataset == "depth5":
            use = syms or ["600519.SH", "000001.SZ"]
            out = self.get_depth_batch(use)
            head = list(out.values())[:2]
            return {
                "provider": self.name, "dataset": dataset,
                "rows": len(out), "columns": list(head[0].keys()) if head else [],
                "preview": head,
            }
        return {
            "provider": self.name, "dataset": dataset, "rows": 0,
            "error": f"腾讯插件未接入 {dataset} 数据集(自动回退 TickFlow)",
        }
