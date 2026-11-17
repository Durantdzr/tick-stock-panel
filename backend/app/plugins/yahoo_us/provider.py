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
