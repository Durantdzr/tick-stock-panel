"""RD-Agent 数据导出适配器（outbound）：enriched 面板 → 因子代码消费的 data.parquet。

把本仓库 enriched 日线面板转成 RD-Agent（Qlib 风格）因子代码期望的 price-volume
数据文件，供桥接环境（~/PycharmProjects/rdagent-bridge）的因子实现循环使用。

设计文档: docs/superpowers/specs/2026-11-18-rdagent-bridge-design.md (F1)

symbol 映射（2026-11-18 实测确认）:
- 仓库 enriched/instruments 格式: ``600901.SH``（code.exchange 后缀）
- RD-Agent/Qlib 风格: ``SH600901``（exchange 前缀 + code）
双向映射 = 按 ``.`` 拆分换序，可逆；非法格式 fail-closed 抛 ValueError。

输出 parquet 契约（长表，供 pandas 侧 set_index）:
- 列: ``datetime``(date) / ``instrument``(str) / ``$open`` / ``$close`` / ``$high`` /
  ``$low`` / ``$volume``(float) / ``$factor``(float, 恒 1.0)
- enriched 的 open..close 为前复权价（CONTRIBUTING §3.2），故 ``$factor`` 恒 1.0；
  绝不二次复权。
- 因子代码消费: ``df.set_index(["datetime", "instrument"]).sort_index()``。

CLI 示例（桥接侧调用）::

    python scripts/export_daily_pv.py \
        --data-dir <主检出>/data --start 2025-11-03 --end 2026-11-16 \
        --output ~/PycharmProjects/rdagent-bridge/data/data.parquet
"""
from __future__ import annotations

import argparse
import contextlib
import re
import sys
from datetime import date
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.engine import BacktestEngine
from app.services.mining_preflight import enriched_partition_dates
from app.tickflow.repository import DataStore, KlineRepository

# 输出列（顺序对齐 Qlib daily_pv 约定 $open/$close/$high/$low/$volume/$factor）
_DAILY_PV_COLUMNS = ("$open", "$close", "$high", "$low", "$volume")
_EXPORT_COLUMNS = ("datetime", "instrument", *_DAILY_PV_COLUMNS, "$factor")
_SOURCE_REQUIRED = ("symbol", "date", "open", "high", "low", "close", "volume")

_QLIB_INSTRUMENT_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")


def to_qlib_symbol(symbol: str) -> str:
    """仓库格式 ``600901.SH`` → Qlib 风格 ``SH600901``；非法格式 fail-closed。"""
    parts = symbol.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"unexpected repo symbol format: {symbol!r} (expect code.exchange)")
    code, exchange = parts
    if exchange not in {"SH", "SZ", "BJ"} or not code.isdigit() or len(code) != 6:
        raise ValueError(f"unexpected repo symbol format: {symbol!r}")
    return f"{exchange}{code}"


def from_qlib_symbol(instrument: str) -> str:
    """Qlib 风格 ``SH600901`` → 仓库格式 ``600901.SH``；非法格式 fail-closed。"""
    matched = _QLIB_INSTRUMENT_RE.match(instrument)
    if matched is None:
        raise ValueError(f"unexpected qlib instrument format: {instrument!r} (expect SH\\d{6})")
    exchange, code = matched.group(1), matched.group(2)
    return f"{code}.{exchange}"


def panel_to_daily_pv(panel: pl.DataFrame) -> pl.DataFrame:
    """enriched 面板 → RD-Agent 因子数据长表（列契约见模块 docstring）。"""
    if panel.is_empty():
        raise ValueError("panel is empty, nothing to export")
    missing = [col for col in _SOURCE_REQUIRED if col not in panel.columns]
    if missing:
        raise ValueError(f"panel missing required columns: {missing}")

    out = panel.select(
        pl.col("date").alias("datetime"),
        pl.col("symbol")
        .map_elements(to_qlib_symbol, return_dtype=pl.String)
        .alias("instrument"),
        pl.col("open").cast(pl.Float64).alias("$open"),
        pl.col("close").cast(pl.Float64).alias("$close"),
        pl.col("high").cast(pl.Float64).alias("$high"),
        pl.col("low").cast(pl.Float64).alias("$low"),
        pl.col("volume").cast(pl.Float64).alias("$volume"),
    )
    # 前复权口径下复权因子恒 1（不二次复权，口径见模块 docstring）
    out = out.with_columns(pl.lit(1.0, dtype=pl.Float64).alias("$factor"))
    return out.select(_EXPORT_COLUMNS).sort(["datetime", "instrument"])


def warmup_start(
    data_dir: Path,
    asset_type: str,
    start: date,
    warmup_bars: int,
) -> date:
    """按实际交易日轴回退 warmup_bars 根，得到数据加载起点（禁自然日替代）。"""
    trading_dates = enriched_partition_dates(data_dir, asset_type)
    if not trading_dates:
        raise ValueError(f"no enriched partitions under {data_dir} for {asset_type!r}")
    first_in_window = next((d for d in trading_dates if d >= start), trading_dates[-1])
    idx = trading_dates.index(first_in_window)
    return trading_dates[max(0, idx - warmup_bars)]


def export_daily_pv(
    data_dir: Path,
    start: date,
    end: date,
    output: Path,
    *,
    asset_type: str = "stock",
    warmup_bars: int = 120,
    symbols: list[str] | None = None,
) -> pl.DataFrame:
    """加载 enriched 面板（含预热段）并写出 data.parquet，返回导出结果供 CLI 汇报。"""
    load_start = warmup_start(data_dir, asset_type, start, warmup_bars)
    store = DataStore(data_dir)
    repo = KlineRepository(store)
    engine = BacktestEngine(repo)
    panel = engine.load_panel(symbols, load_start, end, asset_type=asset_type)
    daily_pv = panel_to_daily_pv(panel)
    output.parent.mkdir(parents=True, exist_ok=True)
    daily_pv.write_parquet(output)
    with contextlib.suppress(Exception):  # DuckDB 关闭失败不影响导出产物
        store.db.close()
    return daily_pv


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="评估窗口起始交易日 YYYY-MM-DD（不含预热）")
    parser.add_argument("--end", required=True, help="评估窗口结束交易日 YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="输出 parquet 路径")
    parser.add_argument("--asset-type", default="stock", choices=["stock", "etf"])
    parser.add_argument("--warmup-bars", type=int, default=120, help="预热根数（按交易日回退）")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认走 settings.data_dir）")
    args = parser.parse_args(argv)

    data_dir = args.data_dir
    if data_dir is None:
        from app.config import settings

        data_dir = settings.data_dir

    daily_pv = export_daily_pv(
        data_dir=data_dir,
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        output=Path(args.output),
        asset_type=args.asset_type,
        warmup_bars=args.warmup_bars,
    )
    n_instruments = daily_pv["instrument"].n_unique()
    n_days = daily_pv["datetime"].n_unique()
    print(
        f"exported rows={len(daily_pv)} instruments={n_instruments} "
        f"days={n_days} range={daily_pv['datetime'].min()}..{daily_pv['datetime'].max()} "
        f"output={args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
