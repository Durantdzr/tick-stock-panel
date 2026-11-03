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


def sync_crypto_instruments(repo) -> int:
    """同步 crypto instruments 维表 (白名单 ∩ 交易所 TRADING 状态)。"""
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
                start_time = datetime.combine(latest, datetime.min.time()) + timedelta(days=1)
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


def run_crypto_sync(repo, *, with_minute: bool = False) -> dict:
    """完整 crypto 同步入口 (instruments → daily → enriched → 可选 minute)。

    加锁串行 (重复触发直接返回进行中状态); 结果缓存在 _last_status。
    """
    global _last_status
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "running": True, "error": "crypto 同步已在进行中"}
    started = time.monotonic()
    _last_status = {"ok": None, "running": True,
                    "started_at": datetime.now().isoformat(timespec="seconds")}
    try:
        instruments = sync_crypto_instruments(repo)
        daily = sync_crypto_daily(repo)
        minute = sync_crypto_minute(repo) if with_minute else None
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
    finally:
        _sync_lock.release()
