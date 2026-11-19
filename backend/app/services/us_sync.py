"""美股 (us) 数据同步服务。

专用市场专用源: 数据经美股数据源 provider (默认 eastmoney_us) 拉取,
存储/指标/筛选复用现有 asset_type 管道 (kline_us_* / enriched / screener)。
与 crypto 同步的差异:
  - instruments 维表落全市场目录 (约 1.4 万只, 供搜索/名称解析),
    白名单仅约束 K 线同步范围;
  - 交易日门控用 XNYS 日历 (services/us_trading_day), 每日 16:35 美东定时任务
    在任务体内判节假日跳过;
  - 日线 date=美东交易日 (provider 层已归一), 分钟 datetime=北京墙钟;
  - 复权因子: 路由源声明 adj_factor 数据集 (如 yahoo_us) 时落 adj_factor_us/
    (拆股/分红 → ex_factor 事件); enriched 复权应用属 Phase 2 — 本期
    enriched 仍按空 factors 计算, 股本/涨跌停恒缺失 (turnover_rate 同口径)。

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


_OVERLAP_CALENDAR_DAYS = 14      # 日线重叠连续性校验窗口 (自然日)
_SPLIT_RATIO_TOLERANCE = 0.005   # 重叠比值偏离 1 超 0.5% 才视为拆股/调整 (防厂商分位噪声)


def _us_daily_closes(repo, symbols: set[str] | None = None,
                     since=None) -> dict[str, dict]:
    """扫描美股日线分区 → {symbol: {date: close}} (连续性校验/因子 prev_close 用)。"""
    base = _us_daily_dir(repo)
    if not base.exists() or not any(base.glob("date=*/*.parquet")):
        return {}
    try:
        lf = pl.scan_parquet(str(base / "date=*" / "*.parquet")).select("symbol", "date", "close")
        if symbols:
            lf = lf.filter(pl.col("symbol").is_in(sorted(symbols)))
        if since is not None:
            lf = lf.filter(pl.col("date") >= since)
        df = lf.collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("美股日线扫描失败 (连续性校验退化为跳过): %s", e)
        return {}
    out: dict[str, dict] = {}
    for sym, d, c in zip(df["symbol"].to_list(), df["date"].to_list(),
                         df["close"].to_list(), strict=True):
        out.setdefault(sym, {})[d] = c
    return out


def _check_overlap(new_df: pl.DataFrame, stored: dict) -> tuple[str, float]:
    """重叠区收盘价连续性判定 → ("ok"|"split"|"mismatch", 代表比值)。

    重叠 = 新帧中日期 ≤ 存量最大日期的公共日期。全部比值≈1 → ok;
    比值一致 (极差≤0.1%) 且明显偏离 1 (>0.5%) → split (雅虎按"至今"回溯
    预调整的签名, 全史重取覆盖); 其余 → mismatch (两源口径冲突/坏数据,
    fail-closed 拒绝落盘)。
    """
    if not stored or new_df.is_empty():
        return ("ok", 1.0)
    stored_max = max(stored)
    common = new_df.filter(pl.col("date") <= stored_max).select("date", "close")
    ratios = []
    for d, c in zip(common["date"].to_list(), common["close"].to_list(), strict=True):
        sc = stored.get(d)
        if sc:
            ratios.append(c / sc)
    if not ratios:
        return ("ok", 1.0)
    mean = sum(ratios) / len(ratios)
    if all(abs(r - 1.0) <= 1e-6 for r in ratios):
        return ("ok", 1.0)
    consistent = max(ratios) / min(ratios) <= 1.001
    if consistent and abs(mean - 1.0) > _SPLIT_RATIO_TOLERANCE:
        return ("split", mean)
    if consistent:
        # 一致的小口径差 (≤0.5%, 厂商分位噪声/换源价差): 视为一致放行,
        # 重叠行经 upsert 覆盖为新源值 (幂等) — 否则换源场景会永久拒收 (审查 I-2)
        return ("ok", mean)
    return ("mismatch", mean)


def _latest_factor_dates(repo) -> dict:
    """adj_factor_us 已存的每 symbol 最新因子日期 (增量锚点)。"""
    path = repo.store.data_dir / "adj_factor_us" / "all.parquet"
    if not path.exists():
        return {}
    try:
        df = pl.read_parquet(path)
        if df.is_empty():
            return {}
        df = (df.select(pl.col("symbol"), pl.col("trade_date").max().over("symbol").alias("latest"))
              .unique(subset=["symbol"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("美股因子增量锚点读取失败, 走全量: %s", e)
        return {}
    return dict(zip(df["symbol"].to_list(), df["latest"].to_list(), strict=True))


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
    if getattr(provider, "fetch_instruments_page", None) is None:
        logger.warning(
            "美股数据源 %s 不支持维表分页 (fetch_instruments_page), 回退 eastmoney_us",
            getattr(provider, "name", provider),
        )
        from app.data_providers import custom as custom_sources

        fallback = custom_sources.get_provider("eastmoney_us")
        if fallback is None:
            logger.warning("eastmoney_us 不可用, 跳过维表刷新 (保留存量)")
            return repo.get_us_instruments().height
        provider = fallback
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
    completed = False
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
        completed = True
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
    if not completed and have_stale:
        # 分页中断: 不用部分数据覆盖完整维表 (防缩水), 沿用既有维表继续。
        # 已拉取的页缓存保留, 下次同步从断点续拉。
        stale_count = repo.get_us_instruments().height
        logger.warning(
            "美股目录分页中断 (页 %d), 沿用既有维表 (%d 只) 继续 K 线同步",
            page, stale_count,
        )
        return stale_count
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

    # 垃圾标的防护: 白名单条目必须存在于全市场维表 (如误输入 "US" 这类
    # 非上市代码), 否则剔除并告警 — 防止单条垃圾导致整轮同步全失败。
    have = set(repo.get_us_instruments()["symbol"].cast(pl.Utf8).to_list()) \
        if not repo.get_us_instruments().is_empty() else set()
    if have:
        unknown = [s for s in symbols if s not in have]
        if unknown:
            logger.warning("美股白名单剔除不在全市场维表中的条目: %s", unknown)
            symbols = [s for s in symbols if s in have]
    if not symbols:
        return {"ok": False, "error": "美股白名单全部条目都不在全市场维表中"}

    existing_max: dict = _latest_daily_dates(repo) if incremental else {}
    frames: list[pl.DataFrame] = []
    errors: list[str] = []
    total = len(symbols)
    for i, symbol in enumerate(symbols):
        latest = existing_max.get(symbol)
        start_time = None
        overlap_since = None
        if latest is not None:
            try:
                candidate = datetime.combine(latest, datetime.min.time()) + timedelta(days=1)
                if candidate > datetime.now():
                    # 本地已持有最新美东交易日 bar: 无缺口可拉 (同 crypto 的 fapi -1023 边界)
                    continue
                overlap_since = candidate - timedelta(days=_OVERLAP_CALENDAR_DAYS)
                start_time = overlap_since
            except (TypeError, ValueError):
                start_time = None
        try:
            df = provider.get_daily([symbol], start_time, None, asset_type="us")
        except ProviderRequestError as e:
            logger.warning("美股日K拉取失败 %s: %s", symbol, e)
            errors.append(f"{symbol}: {e}")
            continue
        if latest is not None and not df.is_empty():
            stored = _us_daily_closes(repo, {symbol}, overlap_since).get(symbol, {})
            verdict, ratio = _check_overlap(df, stored)
            if verdict == "split":
                logger.warning(
                    "美股 %s 检出回溯预调整 (重叠比值≈%.4f), 以全史重取覆盖", symbol, ratio)
                df = provider.get_daily([symbol], None, None, asset_type="us")
            elif verdict == "mismatch":
                errors.append(
                    f"{symbol}: 重叠区收盘价不连续 (比值≈{ratio:.4f}, 既非 1 也非拆股比), 拒绝落盘")
                if on_progress:
                    on_progress(i + 1, total)
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
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services.kline_sync import _write_minute_partition

    if not custom_sources.provider_has_dataset(preferences.get_us_data_provider(), "minute"):
        name = preferences.get_us_data_provider()
        logger.warning("美股数据源 %s 未声明 minute 数据集, 跳过分钟K同步", name)
        return {"ok": True, "skipped": "minute_not_declared",
                "rows": 0, "symbols": 0, "errors": []}

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


def sync_us_adj_factors(repo, on_progress=None) -> dict:
    """美股除权因子同步 (路由源声明 adj_factor 数据集时生效, 如 yahoo_us)。

    因子落 adj_factor_us/all.parquet (拆股/分红 → ex_factor 单事件比值),
    供回测/查询侧消费; enriched 复权应用属 Phase 2。eastmoney_us 路由时
    未声明 → skipped (日志留痕)。增量锚点 = 已存 max(trade_date)+1d;
    任一白名单标的无因子史则全量重取 (merge-upsert 幂等, 重放无害)。
    """
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    name = preferences.get_us_data_provider()
    if not custom_sources.provider_has_dataset(name, "adj_factor"):
        logger.info("美股数据源 %s 未声明 adj_factor 数据集, 跳过因子同步", name)
        return {"ok": True, "skipped": "adj_factor_not_declared",
                "rows": 0, "symbols": 0, "errors": []}
    provider = _us_provider()
    symbols = us_universe.load_universe(repo.store.data_dir)
    if not symbols:
        return {"ok": False, "error": "美股白名单为空"}
    anchors = _latest_factor_dates(repo)
    if anchors and all(s in anchors for s in symbols):
        # 锚点 = 已存 max(trade_date); provider 过滤为排他 (trade_date > start_time),
        # 已存 max 事件不会重放, max+1d 落点的新事件不会被吞掉 (审查 I-1)
        min_latest = min(anchors[s] for s in symbols)
        anchor = datetime.combine(min_latest, datetime.min.time())
    else:
        anchor = None
    prev_closes = _us_daily_closes(repo)
    df = provider.get_adj_factors(symbols, anchor, None, asset_type="us",
                                  prev_closes=prev_closes)
    if df.is_empty():
        logger.info("美股因子无需更新 (%d 标的)", len(symbols))
        return {"ok": True, "rows": 0, "symbols": len(symbols), "errors": []}
    from app.services.kline_sync import _merge_adj_factor_store

    added, affected = _merge_adj_factor_store(
        repo.store.data_dir / "adj_factor_us" / "all.parquet", df)
    logger.info("美股因子合并: +%d 行, 变化 %d 标的 (共 %d 标的)",
                added, len(affected), len(symbols))
    if on_progress:
        on_progress(len(symbols), len(symbols))
    return {"ok": True, "rows": df.height, "added": added,
            "symbols": len(symbols), "errors": []}


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
        factors = sync_us_adj_factors(repo)
        _report("factors", 85, f"因子 {factors.get('rows', 0)} 行")
        minute = sync_us_minute(repo) if with_minute else None
        if minute is not None:
            _report("minute", 90, f"分钟K {minute.get('rows', 0)} 行")
        _last_status = {
            "ok": True,
            "instruments": instruments,
            "daily": daily,
            "factors": factors,
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
