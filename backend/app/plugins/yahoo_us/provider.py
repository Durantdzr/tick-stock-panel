"""雅虎财经美股 (NASDAQ/NYSE/AMEX) 公开行情 provider。

专用市场数据源: plugin.yaml 声明 asset_types: [us], 不进入 A 股 7 能力路由
候选; 美股同步服务 (services/us_sync.py) 直接经 custom.loader 取数, fail-closed。

实现数据集 (仅 asset_type="us"):
  - daily        日K (v8 chart, 原始价 = 反向还原雅虎"按至今回溯调整拆股"的序列)
  - adj_factor   除权因子 (拆股 to/from + 分红 → ex_factor 单事件 pre/post 比值)
  - get_instruments("us") 显式拒绝 (雅虎无全市场目录端点, 维表由 eastmoney_us 承载)

反向还原 (parse_chart, 数学期见设计文档):
  - 载荷 quote 序列已按"系列末端"回溯调整拆股; period2 恒为 now ⇒ 事件日之后的
    全部调整拆股必在载荷 events 内, 用载荷新闻事件即可精确还原:
      later[t] = ∏(拆股 to/from, 拆股日严格晚于 t)
      ohlc_raw = ohlc_displayed × later[t];  volume_raw = volume_displayed / later[t]
      d_as_traded = displayed_amount × later[t] × 同日 to/from
  - 黄金夹具 (tests/fixtures/yahoo/) 钉死方向与数值; 合成夹具 (_synthetic) 待
    真机复核 (设计文档实施决定 2/5)。

symbol 映射: 内部 ticker → 雅虎格式 ('.'/'_' → '-', 大写, 如 BRK.B → BRK-B)。
旧 ticker 陷阱: 雅虎历史代码可能现属其他公司 (FB/BBT 类), 映射后拉空即
fail-closed 上浮, 不猜不换。

单位口径 (CONTRIBUTING §3.1, 与 eastmoney_us 一致): volume=股 (不换算);
amount 雅虎无对应字段 → None 不伪造; date = 美东交易日 (naive, 接口原生)。
当日未收盘 bar 丢弃 (美东当日且 now < 16:00 ET)。
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_CLOSE_CUTOFF = time(16, 0)  # 美东收盘缓冲: 当日 16:00 前视为未收盘
_DEFAULT_TZ = ZoneInfo("America/New_York")


def _event_date(entry: dict, key: str, tz: ZoneInfo) -> date | None:
    """事件日期: entry['date'] 优先, 缺失回退 dict key (均为 epoch 秒)。"""
    raw = entry.get("date")
    if raw is None:
        try:
            raw = int(key)
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromtimestamp(int(raw), tz).date()
    except (OverflowError, OSError, ValueError):
        return None


def _mul(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def _div(value: float | None, factor: float) -> float | None:
    return None if value is None else value / factor


def parse_chart(
    payload: dict, symbol: str, now_et: datetime | None = None,
) -> tuple[list[dict], list[tuple[date, float]], list[tuple[date, float]]]:
    """v8 chart 载荷 → (原始价日K行, 拆股事件, 分红事件)。

    - 拆股事件 = (ex_date, to/from); 分红事件 = (ex_date, as-traded 每股金额);
      均按日期升序。
    - 原始价: 雅虎 displayed 序列被"按至今回溯调整拆股", 用载荷 events 反向还原
      (later = 严格晚于该 bar 的拆股累积倍数; OHLC ×later, volume /later)。
    - 未完成 bar 丢弃: 交易所当日且 now < 16:00 (美东)。
    """
    result = payload["chart"]["result"][0]
    meta = result.get("meta") or {}
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    if now_et is None:
        now_et = datetime.now(tz)

    events = result.get("events") or {}
    split_by_date: dict[date, float] = {}
    for key, entry in (events.get("splits") or {}).items():
        d = _event_date(entry, key, tz)
        if d is None:
            continue
        ratio = float(entry.get("numerator", 1.0)) / float(entry.get("denominator", 1.0))
        split_by_date[d] = split_by_date.get(d, 1.0) * ratio
    div_by_date: dict[date, float] = {}
    for key, entry in (events.get("dividends") or {}).items():
        d = _event_date(entry, key, tz)
        if d is None:
            continue
        div_by_date[d] = div_by_date.get(d, 0.0) + float(entry.get("amount") or 0.0)

    ts_list = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0] or {}
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []

    intraday = now_et.time() < _CLOSE_CUTOFF
    today = now_et.date()
    split_days = sorted(split_by_date)

    def _later(d: date) -> float:
        factor = 1.0
        for sd in split_days:
            if sd > d:
                factor *= split_by_date[sd]
        return factor

    rows: list[dict] = []
    for i, ts in enumerate(ts_list):
        d = datetime.fromtimestamp(int(ts), tz).date()
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue  # 停牌/缺失 bar
        if intraday and d == today:
            continue  # 未完成 bar
        later = _later(d)
        rows.append({
            "symbol": symbol,
            "date": d,
            "open": _mul(opens[i] if i < len(opens) else None, later),
            "high": _mul(highs[i] if i < len(highs) else None, later),
            "low": _mul(lows[i] if i < len(lows) else None, later),
            "close": close * later,
            "volume": _div(volumes[i] if i < len(volumes) else None, later),
        })

    split_events = sorted(split_by_date.items())
    dividend_events = []
    for d in sorted(div_by_date):
        amount = div_by_date[d]
        same_day = split_by_date.get(d, 1.0)
        dividend_events.append((d, amount * _later(d) * same_day))
    return rows, split_events, dividend_events


# ================================================================ provider
import random  # noqa: E402
import time  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402

import polars as pl  # noqa: E402

from app.data_providers.base import ProviderRequestError  # noqa: E402
from app.data_providers.normalizer import normalize_daily  # noqa: E402

from .client import YahooClient, YahooError  # noqa: E402

_DATASETS = ("daily", "adj_factor")
_DAILY_INTERVAL_S = 0.3  # 标的间节流基线 (雅虎 ~2000 请求/小时/IP)
_SYMBOL_JITTER_S = 0.15


@dataclass
class _YahooUSConfig:
    """轻量 config shim, 让 custom loader 的 provider_has_dataset 能识别本 provider。"""

    name: str = "yahoo_us"
    display_name: str = "雅虎财经·美股"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _require_us(asset_type: str) -> None:
    if asset_type != "us":
        raise ProviderRequestError(
            f"雅虎美股数据源仅提供美股(us)数据, 收到 asset_type={asset_type!r}"
        )


class YahooUSProvider:
    """雅虎美股数据源 (us 专用市场源)。

    维表边界: 雅虎无全市场目录端点, get_instruments 显式拒绝 (ProviderRequestError),
    instruments_us 维表由 eastmoney_us 承载 — us_sync 负责回退编排。
    """

    name = "yahoo_us"
    builtin = True

    def __init__(self, client: YahooClient | None = None) -> None:
        self.config = _YahooUSConfig()
        self._client = client if client is not None else YahooClient()
        self._symbol_memo: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def _to_yahoo_symbol(self, symbol: str) -> str:
        """内部 ticker → 雅虎格式 ('.'/'_' → '-', 大写, 如 BRK.B → BRK-B)。

        旧 ticker 陷阱: 雅虎历史代码可能现属其他公司 (FB/BBT 类), 映射后拉空
        会以失败上浮 (见 get_daily 的 fail-closed 收集), 不猜不换。
        """
        key = symbol.strip().upper()
        ys = self._symbol_memo.get(key)
        if ys is None:
            ys = key.replace(".", "-").replace("_", "-")
            self._symbol_memo[key] = ys
        return ys

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "us",
        on_chunk_done=None,
    ) -> pl.DataFrame:
        """美股日K → 内部契约。原始价 (反向还原), volume 股, date=美东交易日。

        start_time=None → period1=0 全量历史 (雅虎全史与 1 年同为一次请求);
        end_time 恒用真实 now (period2=now 不变量: 事件日之后的全部调整拆股
        必在载荷 events 内, parse_chart 才能自洽还原)。任一标的失败 → 整体
        ProviderRequestError (fail-closed), 不返回部分数据。
        """
        _require_us(asset_type)
        if not symbols:
            return pl.DataFrame()
        period2 = int(datetime.now().timestamp())
        period1 = int(start_time.timestamp()) if start_time is not None else 0
        frames: list[pl.DataFrame] = []
        failed: list[str] = []
        for i, symbol in enumerate(symbols):
            if i:
                time.sleep(_DAILY_INTERVAL_S + random.uniform(0, _SYMBOL_JITTER_S))
            try:
                payload = self._client.chart(self._to_yahoo_symbol(symbol), period1, period2)
                rows, _splits, _dividends = parse_chart(payload, symbol)
            except YahooError as e:
                failed.append(f"{symbol}: {e}")
                continue
            df = normalize_daily(rows, source=self.name)
            if not df.is_empty():
                frames.append(df.with_columns(pl.lit(self.name).alias("source")))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        if failed:
            raise ProviderRequestError(f"雅虎美股日K拉取失败: {'; '.join(failed)}")
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_instruments(self, asset_type: str = "us") -> pl.DataFrame:
        """维表不支持: 雅虎无全市场目录端点 (显式拒绝, 不返空伪装)。"""
        _require_us(asset_type)
        raise ProviderRequestError(
            "雅虎无全市场目录端点: 美股维表由 eastmoney_us 承载, 本源不提供 get_instruments"
        )


def availability() -> tuple[bool, str]:
    """loader 启动自检: 探测 AAPL 近端 chart 连通性。不抛异常。

    429/401/999 证明接口可达 (限频上下文), 注册放行 — 同步轮次自带退避,
    共享代理出口持续 429 时不会让插件永久不可用; 404/403/网络异常按不可达处理。
    """
    try:
        probe = YahooUSProvider()
        now_s = int(datetime.now().timestamp())
        payload = probe._client.chart("AAPL", now_s - 10 * 86400, now_s)
        rows, _splits, _dividends = parse_chart(payload, "AAPL")
    except YahooError as e:
        if e.status_code in (429, 401, 999):
            return True, f"接口可达但限频 (HTTP {e.status_code}), 同步轮次按退避重试"
        return False, f"公开接口不可达: {e}"
    except Exception as e:  # noqa: BLE001
        return False, f"公开接口不可达: {e}"
    if not rows:
        return False, "接口返回空数据, 结构可能已变化"
    return True, "ok"
