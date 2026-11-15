"""加密货币 (crypto) 数据同步服务。

专用市场专用源: 数据经 crypto 数据源 provider (默认 binance) 拉取,
存储/指标/筛选复用现有 asset_type 管道 (kline_crypto_* / enriched / screener)。
与 A 股同步的差异:
  - 7x24 无交易日/交易时段门控 (不依赖 trading_day / market_time 会话);
  - 无复权/股本/涨跌停: enriched 用空 factors/instruments 计算,
    turnover_rate 与 consecutive_limit_* 恒缺失 (存储列按存在性裁剪);
  - 日线 date = UTC 开盘时间的北京日期 (provider 层已归一, 本服务不做时区换算)。

单位口径: volume = 基础币数量, amount = USDT 计价 (非股票手/元口径)。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.services import crypto_universe

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()
_last_status: dict | None = None


def last_status() -> dict | None:
    """最近一次同步的结果摘要 (供 API 展示)。"""
    return _last_status


def is_running() -> bool:
    """是否正有同步在执行。"""
    return _sync_lock.locked()


def _crypto_provider():
    """按偏好解析 crypto 数据源 provider (默认 binance), 校验能力后返回。"""
    from app.services import preferences

    name = preferences.get_crypto_data_provider()
    from app.data_providers import custom as custom_sources

    provider = custom_sources.get_provider(name)
    if provider is None:
        raise ProviderRequestError(f"crypto 数据源 {name!r} 未注册或不可用")
    if not custom_sources.provider_has_dataset(name, "daily"):
        raise ProviderRequestError(f"crypto 数据源 {name!r} 未声明 daily 数据集")
    return provider


def _crypto_daily_dir(repo):
    return repo.store.data_dir / "kline_crypto_daily"


def _latest_daily_dates(repo, symbols: list[str]) -> dict[str, object]:
    """已落盘的每 symbol 最新日线日期 (增量同步起点)。"""
    base = _crypto_daily_dir(repo)
    if not base.exists() or not any(base.glob("date=*/*.parquet")):
        return {}
    try:
        df = pl.scan_parquet(str(base / "date=*" / "*.parquet")).select(
            pl.col("symbol"), pl.col("date").max().over("symbol").alias("latest"),
        ).unique(subset=["symbol"]).collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("crypto 日线增量起点读取失败, 走全量: %s", e)
        return {}
    return dict(zip(df["symbol"].to_list(), df["latest"].to_list(), strict=True))


_INSTRUMENTS_TTL_S = 12 * 3600.0  # 维表变化极少 (新上市/下架), 12h 内不重扫


def _instruments_fresh(repo) -> bool:
    """instruments_crypto 落盘在 TTL 内且覆盖全部白名单成员 → 跳过重扫。

    覆盖校验防半份维表 (上游 exchangeInfo 半失败时曾写入单市场子集) 长期驻留。
    """
    path = repo.store.data_dir / "instruments_crypto" / "instruments_crypto.parquet"
    try:
        import time as _time

        if (_time.time() - path.stat().st_mtime) >= _INSTRUMENTS_TTL_S:
            return False
    except OSError:
        return False
    try:
        have = set(pl.read_parquet(path)["symbol"].cast(pl.Utf8).to_list())
    except Exception:  # noqa: BLE001
        return False
    wanted = set(crypto_universe.internal_symbols(
        crypto_universe.load_universe(repo.store.data_dir)))
    return wanted <= have


def sync_crypto_instruments(repo) -> int:
    """同步 crypto instruments 维表 (白名单 ∩ 交易所 TRADING 状态)。"""
    if _instruments_fresh(repo):
        logger.info("crypto instruments 维表 12h 内已刷新, 跳过重扫")
        return repo.get_crypto_instruments().height
    provider = _crypto_provider()
    universe = crypto_universe.load_universe(repo.store.data_dir)
    wanted = set(crypto_universe.internal_symbols(universe))
    all_inst = provider.get_instruments("crypto")
    if all_inst.is_empty():
        logger.warning("crypto instruments 拉取为空, 保留既有维表")
        return 0
    filtered = all_inst.filter(pl.col("symbol").is_in(sorted(wanted)))
    if filtered.is_empty():
        logger.warning("crypto 白名单 %d 个标的全都不在交易所列表中", len(wanted))
        return 0
    repo.save_crypto_instruments(filtered)
    logger.info("crypto instruments 已同步: %d 个标的", filtered.height)
    return filtered.height


def sync_crypto_daily(repo, *, incremental: bool = True, on_progress=None) -> dict:
    """同步白名单标的日K并重算 enriched。返回状态摘要。"""
    provider = _crypto_provider()
    universe = crypto_universe.load_universe(repo.store.data_dir)
    symbols = crypto_universe.internal_symbols(universe)
    if not symbols:
        return {"ok": False, "error": "crypto 白名单为空"}

    existing_max: dict = _latest_daily_dates(repo, symbols) if incremental else {}
    frames: list[pl.DataFrame] = []
    errors: list[str] = []
    total = len(symbols)
    for i, symbol in enumerate(symbols):
        latest = existing_max.get(symbol)
        start_time = None
        if latest is not None:
            try:
                candidate = datetime.combine(latest, datetime.min.time()) + timedelta(days=1)
                if candidate > datetime.now():
                    # 本地已持有最新 UTC 日 bar (币安日线 date=UTC 日): 无缺口可拉。
                    # 不钳制会把 start 传到明天 — 现货返空数组, 合约 fapi 返 -1023 错误。
                    continue
                start_time = candidate
            except (TypeError, ValueError):
                start_time = None
        try:
            df = provider.get_daily([symbol], start_time, None, asset_type="crypto")
        except ProviderRequestError as e:
            logger.warning("crypto 日K拉取失败 %s: %s", symbol, e)
            errors.append(f"{symbol}: {e}")
            continue
        if not df.is_empty():
            frames.append(df)
        if on_progress:
            on_progress(i + 1, total)
    if not frames:
        if existing_max:
            logger.info("crypto 日K无需更新 (%d 个标的已是最新, 失败 %d)", len(existing_max), len(errors))
            return {"ok": True, "rows": 0, "updated": 0, "symbols": total, "errors": errors}
        raise ProviderRequestError(f"crypto 日K全部拉取失败: {errors[:3]}")

    raw = pl.concat(frames, how="diagonal_relaxed").unique(
        subset=["symbol", "date"], keep="last",
    ).sort(["symbol", "date"])
    repo.append_daily_asset("crypto", raw)
    # 增量同步只追加了尾部日期; enriched 指标计算需要完整历史窗口,
    # 但落盘只需重写受影响日期之后的分区 (更早分区内容不变)。
    since_date = raw["date"].min() if existing_max else None
    enriched_rows = _recompute_crypto_enriched(repo, since_date=since_date)
    repo.invalidate_enriched_asset("crypto")
    return {
        "ok": True,
        "rows": raw.height,
        "enriched_rows": enriched_rows,
        "symbols": total,
        "errors": errors,
    }


def _recompute_crypto_enriched(repo, *, since_date=None) -> int:
    """重算 crypto enriched 并落盘 (标的少, 全量算保证指标口径一致)。

    since_date 非空时只落盘该日期及之后的分区 (增量同步尾部场景)。
    """
    from app.indicators.pipeline import compute_enriched

    base = _crypto_daily_dir(repo)
    if not base.exists():
        return 0
    raw = pl.scan_parquet(str(base / "date=*" / "*.parquet")).collect()
    if raw.is_empty():
        return 0
    # crypto 无复权/股本/涨跌停: 空 factors/instruments/shares, 相关列自然缺失
    enriched = compute_enriched(raw)
    if enriched.is_empty():
        return 0
    if since_date is not None:
        enriched = enriched.filter(pl.col("date") >= since_date)
        if enriched.is_empty():
            return 0
    # append 语义 = 同分区 read-modify-write upsert, 行级 unique 幂等
    repo.append_enriched_asset("crypto", enriched)
    return enriched.height


def sync_crypto_minute(repo, days: int = 3, on_progress=None) -> dict:
    """同步白名单标的近 N 天 1 分钟K (可选步骤)。"""
    from app.services.kline_sync import _write_minute_partition

    provider = _crypto_provider()
    universe = crypto_universe.load_universe(repo.store.data_dir)
    symbols = crypto_universe.internal_symbols(universe)
    if not symbols:
        return {"ok": False, "error": "crypto 白名单为空"}
    end = datetime.now()
    start = end - timedelta(days=max(1, days))
    minute_dir = repo.store.data_dir / "kline_crypto_minute"
    written = 0
    errors: list[str] = []
    for i, symbol in enumerate(symbols):
        try:
            df = provider.get_minute([symbol], start, end, asset_type="crypto")
        except ProviderRequestError as e:
            logger.warning("crypto 分钟K拉取失败 %s: %s", symbol, e)
            errors.append(f"{symbol}: {e}")
            continue
        if not df.is_empty():
            written += _write_minute_partition(df, minute_dir)
        if on_progress:
            on_progress(i + 1, len(symbols))
    repo.invalidate_enriched_asset("crypto")
    return {"ok": True, "rows": written, "symbols": len(symbols), "errors": errors}


def run_crypto_sync(repo, *, with_minute: bool = False, on_progress=None) -> dict:
    """完整 crypto 同步入口 (instruments → daily → enriched → 可选 minute)。

    加锁串行 (重复触发直接返回进行中状态); 结果缓存在 _last_status。
    on_progress: (stage, pct, msg) 阶段回调, 供任务库跟踪 (enriched 在
    sync_crypto_daily 内部完成, 回调在 daily 之后)。None 时零开销。
    """
    global _last_status
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "running": True, "error": "crypto 同步已在进行中"}
    started = time.monotonic()
    _last_status = {"ok": None, "running": True,
                    "started_at": datetime.now().isoformat(timespec="seconds")}

    def _report(stage: str, pct: int, msg: str) -> None:
        if on_progress:
            on_progress(stage, pct, msg)

    try:
        instruments = sync_crypto_instruments(repo)
        _report("instruments", 10, f"维表 {instruments} 只")
        daily = sync_crypto_daily(repo)
        _report("daily", 50, f"日K {daily.get('rows', 0)} 行")
        _report("enriched", 80, f"指标 {daily.get('enriched_rows', 0)} 行")
        minute = sync_crypto_minute(repo) if with_minute else None
        if minute is not None:
            _report("minute", 90, f"分钟K {minute.get('rows', 0)} 行")
        _last_status = {
            "ok": True,
            "instruments": instruments,
            "daily": daily,
            "minute": minute,
            "elapsed_s": round(time.monotonic() - started, 1),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        return _last_status
    except ProviderRequestError as e:
        _last_status = {"ok": False, "error": str(e), "finished_at": datetime.now().isoformat(timespec="seconds")}
        return _last_status
    except Exception as e:
        logger.exception("同步线程异常")
        _last_status = {"ok": False, "error": f"{type(e).__name__}: {e}", "finished_at": datetime.now().isoformat(timespec="seconds")}
        return _last_status
    finally:
        _sync_lock.release()
