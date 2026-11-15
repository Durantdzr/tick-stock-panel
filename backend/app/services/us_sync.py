"""美股 (us) 数据同步服务。

专用市场专用源: 数据经美股数据源 provider (默认 eastmoney_us) 拉取,
存储/指标/筛选复用现有 asset_type 管道 (kline_us_* / enriched / screener)。
与 crypto 同步的差异:
  - instruments 维表落全市场目录 (约 1.4 万只, 供搜索/名称解析),
    白名单仅约束 K 线同步范围;
  - 交易日门控用 XNYS 日历 (services/us_trading_day), 每日 16:35 美东定时任务
    在任务体内判节假日跳过;
  - 日线 date=美东交易日 (provider 层已归一), 分钟 datetime=北京墙钟;
  - 无复权/股本/涨跌停: enriched 用空 factors/instruments 计算,
    turnover_rate 与 consecutive_limit_* 恒缺失 (与 crypto 同口径)。

单位口径: volume=股, amount=美元 (trading_rules volume_unit=share)。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.services import us_trading_day, us_universe

logger = logging.getLogger(__name__)

_sync_lock = threading.Lock()
_last_status: dict | None = None


def last_status() -> dict | None:
    """最近一次同步的结果摘要 (供 API 展示)。"""
    return _last_status


def is_running() -> bool:
    """是否正有同步在执行。"""
    return _sync_lock.locked()


def _us_provider():
    """按偏好解析美股数据源 provider (默认 eastmoney_us), 校验能力后返回。"""
    from app.services import preferences

    name = preferences.get_us_data_provider()
    from app.data_providers import custom as custom_sources

    provider = custom_sources.get_provider(name)
    if provider is None:
        raise ProviderRequestError(f"美股数据源 {name!r} 未注册或不可用")
    if not custom_sources.provider_has_dataset(name, "daily"):
        raise ProviderRequestError(f"美股数据源 {name!r} 未声明 daily 数据集")
    return provider


def _us_daily_dir(repo):
    return repo.store.data_dir / "kline_us_daily"


def _latest_daily_dates(repo) -> dict[str, object]:
    """已落盘的每 symbol 最新日线日期 (增量同步起点)。"""
    base = _us_daily_dir(repo)
    if not base.exists() or not any(base.glob("date=*/*.parquet")):
        return {}
    try:
        df = pl.scan_parquet(str(base / "date=*" / "*.parquet")).select(
            pl.col("symbol"), pl.col("date").max().over("symbol").alias("latest"),
        ).unique(subset=["symbol"]).collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("美股日线增量起点读取失败, 走全量: %s", e)
        return {}
    return dict(zip(df["symbol"].to_list(), df["latest"].to_list(), strict=True))


def _dir_cache_dir(repo):
    return repo.store.data_dir / "cache" / "eastmoney_us" / "dir_pages"


_INSTRUMENTS_TTL_S = 12 * 3600.0  # 维表变化极少 (新上市/下架), 12h 内不重扫


def _instruments_fresh(repo) -> bool:
    """instruments_us 落盘在 TTL 内且覆盖全部白名单成员 → 跳过重扫。

    覆盖校验防半份维表 (上游接口半失败时曾写入单市场子集) 长期驻留。
    """
    path = repo.store.data_dir / "instruments_us" / "instruments_us.parquet"
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
    wanted = set(us_universe.load_universe(repo.store.data_dir))
    return wanted <= have


def sync_us_instruments(repo) -> int:
    """同步美股 instruments 维表 (全市场目录, 约 1.4 万只 / 139 页)。

    东财 clist 对连续分页有累计限频 (按 IP), 单次全量扫描可能中途被断 —
    分页结果逐页落盘缓存 + 水位标记: 失败后下次同步从断点续拉,
    多轮尝试内必然完成 (缓存 24h 过期重扫)。
    """
    if _instruments_fresh(repo):
        logger.info("美股 instruments 维表 12h 内已刷新, 跳过重扫")
        return repo.get_us_instruments().height

    stale_path = repo.store.data_dir / "instruments_us" / "instruments_us.parquet"
    have_stale = stale_path.exists()

    from app.data_providers.normalizer import INSTRUMENT_COLS

    provider = _us_provider()
    cache_dir = _dir_cache_dir(repo)
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"

    manifest: dict = {"done": [], "total": None}
    if manifest_path.exists():
        try:
            import json as _json

            manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
            import time as _time

            if _time.time() - manifest_path.stat().st_mtime > 86_400:
                manifest = {"done": [], "total": None}  # 缓存过期重扫
        except (OSError, ValueError):
            manifest = {"done": [], "total": None}

    done: list[int] = list(manifest.get("done") or [])
    rows: list[dict] = []
    for pg in done:
        part = cache_dir / f"page_{pg:03d}.parquet"
        if part.exists():
            rows.extend(pl.read_parquet(part).to_dicts())
    total = manifest.get("total")

    page = 1
    try:
        while True:
            if page in done:
                page += 1
                continue
            page_rows, page_total = provider.fetch_instruments_page(page)
            if page_total:
                total = page_total
            if not page_rows:
                break  # 翻到末页
            rows.extend(page_rows)
            done.append(page)
            part = cache_dir / f"page_{page:03d}.parquet"
            pl.DataFrame(page_rows).write_parquet(part)
            manifest.update({"done": sorted(set(done)), "total": total})
            import json as _json

            manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")
            logger.info("美股目录断点续传: 第 %d 页完成 (累计 %d 只)", page, len(rows))
            page += 1
            time.sleep(1.0)  # 限频保护
            if total and len(done) * 100 >= total:
                break
    except ProviderRequestError as e:
        # 部分网络环境屏蔽 push2 目录域名 (K线端点 push2his 不受影响):
        # 中途失败走 fail-open, 沿用既有维表继续 K 线同步。
        logger.warning("美股目录分页中断 (%s), 已累计 %d 只", e, len(rows))

    if not rows:
        # 目录接口不可达时 fail-open: 沿用既有维表继续 K 线同步 (维表仅供
        # 搜索/名称解析, 非 K 线同步前置)。部分 NAS ISP 屏蔽 push2 域名,
        # K线端点 (push2his) 不受影响。
        if have_stale:
            stale_count = repo.get_us_instruments().height
            logger.warning(
                "美股目录拉取失败, 沿用既有维表 (%d 只) 继续 K 线同步", stale_count)
            return stale_count
        logger.warning("美股 instruments 拉取为空, 保留既有维表")
        return 0
    all_inst = pl.DataFrame(rows).select(INSTRUMENT_COLS).unique(
        subset=["symbol"], keep="last").sort("symbol")
    repo.save_us_instruments(all_inst)
    # 完成后清理页缓存
    import shutil as _shutil

    _shutil.rmtree(cache_dir, ignore_errors=True)
    logger.info("美股 instruments 已同步: %d 只 (分页缓存已清理)", all_inst.height)
    return all_inst.height


def sync_us_daily(repo, *, incremental: bool = True, on_progress=None) -> dict:
    """同步白名单标的日K并重算 enriched。返回状态摘要。"""
    provider = _us_provider()
    symbols = us_universe.load_universe(repo.store.data_dir)
    if not symbols:
        return {"ok": False, "error": "美股白名单为空"}

    existing_max: dict = _latest_daily_dates(repo) if incremental else {}
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
                    # 本地已持有最新美东交易日 bar: 无缺口可拉 (同 crypto 的 fapi -1023 边界)
                    continue
                start_time = candidate
            except (TypeError, ValueError):
                start_time = None
        try:
            df = provider.get_daily([symbol], start_time, None, asset_type="us")
        except ProviderRequestError as e:
            logger.warning("美股日K拉取失败 %s: %s", symbol, e)
            errors.append(f"{symbol}: {e}")
            continue
        if not df.is_empty():
            frames.append(df)
        if on_progress:
            on_progress(i + 1, total)
    if not frames:
        if existing_max:
            logger.info("美股日K无需更新 (%d 个标的已是最新, 失败 %d)", len(existing_max), len(errors))
            return {"ok": True, "rows": 0, "updated": 0, "symbols": total, "errors": errors}
        raise ProviderRequestError(f"美股日K全部拉取失败: {errors[:3]}")

    raw = pl.concat(frames, how="diagonal_relaxed").unique(
        subset=["symbol", "date"], keep="last",
    ).sort(["symbol", "date"])
    repo.append_daily_asset("us", raw)
    since_date = raw["date"].min() if existing_max else None
    enriched_rows = _recompute_us_enriched(repo, since_date=since_date)
    repo.invalidate_enriched_asset("us")
    return {
        "ok": True,
        "rows": raw.height,
        "enriched_rows": enriched_rows,
        "symbols": total,
        "errors": errors,
    }


def _recompute_us_enriched(repo, *, since_date=None) -> int:
    """重算美股 enriched 并落盘 (白名单规模小, 全量算保证指标口径一致)。

    since_date 非空时只落盘该日期及之后的分区 (增量同步尾部场景)。
    """
    from app.indicators.pipeline import compute_enriched

    base = _us_daily_dir(repo)
    if not base.exists():
        return 0
    raw = pl.scan_parquet(str(base / "date=*" / "*.parquet")).collect()
    if raw.is_empty():
        return 0
    # 美股无复权/股本/涨跌停: 空 factors/instruments/shares, 相关列自然缺失
    enriched = compute_enriched(raw)
    if enriched.is_empty():
        return 0
    if since_date is not None:
        enriched = enriched.filter(pl.col("date") >= since_date)
        if enriched.is_empty():
            return 0
    # append 语义 = 同分区 read-modify-write upsert, 行级 unique 幂等
    repo.append_enriched_asset("us", enriched)
    return enriched.height


def sync_us_minute(repo, days: int = 3, on_progress=None) -> dict:
    """同步白名单标的近 N 天 1 分钟K (可选步骤)。

    分区口径: 北京自然日 (kline_us_minute date=*, 美股 session 跨北京午夜,
    同一交易日分钟K分布跨两个分区 — 查询按 datetime 范围拼接, 见 docs/us-market.md)。
    """
    from app.services.kline_sync import _write_minute_partition

    provider = _us_provider()
    symbols = us_universe.load_universe(repo.store.data_dir)
    if not symbols:
        return {"ok": False, "error": "美股白名单为空"}
    end = datetime.now()
    start = end - timedelta(days=max(1, days))
    minute_dir = repo.store.data_dir / "kline_us_minute"
    written = 0
    errors: list[str] = []
    for i, symbol in enumerate(symbols):
        try:
            df = provider.get_minute([symbol], start, end, asset_type="us")
        except ProviderRequestError as e:
            logger.warning("美股分钟K拉取失败 %s: %s", symbol, e)
            errors.append(f"{symbol}: {e}")
            continue
        if not df.is_empty():
            written += _write_minute_partition(df, minute_dir)
        if on_progress:
            on_progress(i + 1, len(symbols))
    repo.invalidate_enriched_asset("us")
    return {"ok": True, "rows": written, "symbols": len(symbols), "errors": errors}


def is_us_trading_session_day() -> bool:
    """今天 (美东) 是否美股交易日 (定时任务节假日门控)。"""
    return us_trading_day.is_us_trading_day(us_trading_day.us_today())


def run_us_sync(repo, *, with_minute: bool = False, skip_non_trading_day: bool = False,
                on_progress=None) -> dict:
    """完整美股同步入口 (instruments → daily → enriched → 可选 minute)。

    加锁串行 (重复触发直接返回进行中状态); 结果缓存在 _last_status。
    skip_non_trading_day=True 时美东节假日直接跳过 (定时任务路径)。
    on_progress: (stage, pct, msg) 阶段回调, 供任务库跟踪 (enriched 在
    sync_us_daily 内部完成, 回调在 daily 之后)。None 时零开销。
    """
    global _last_status
    if not _sync_lock.acquire(blocking=False):
        return {"ok": False, "running": True, "error": "美股同步已在进行中"}
    started = time.monotonic()
    _last_status = {"ok": None, "running": True,
                    "started_at": datetime.now().isoformat(timespec="seconds")}

    def _report(stage: str, pct: int, msg: str) -> None:
        if on_progress:
            on_progress(stage, pct, msg)

    try:
        if skip_non_trading_day and not is_us_trading_session_day():
            _last_status = {
                "ok": True, "skipped": "us_holiday",
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            return _last_status
        instruments = sync_us_instruments(repo)
        _report("instruments", 10, f"维表 {instruments} 只")
        daily = sync_us_daily(repo)
        _report("daily", 50, f"日K {daily.get('rows', 0)} 行")
        _report("enriched", 80, f"指标 {daily.get('enriched_rows', 0)} 行")
        minute = sync_us_minute(repo) if with_minute else None
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
