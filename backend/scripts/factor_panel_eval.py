"""RD-Agent 因子评估适配器（inbound）：外部因子值面板 → 本仓库因子评估 → JSON。

Phase A 桥接的评估内核（Phase B 直接复用）：把 RD-Agent 生成的因子值
（parquet: symbol/instrument + date/datetime + value）接入 FactorBacktestService，
输出 RD-Agent 反馈契约要求的三指标别名 + 全量诊断（IC 序列/分层/多空/覆盖率/换手）。

设计文档: docs/superpowers/specs/2026-11-18-rdagent-bridge-design.md (F2)

指标别名契约（对齐 rdagent scenarios/qlib/developer/feedback.py IMPORTANT_METRICS）:
- ``IC`` = 日截面 Rank IC 均值
- ``1day.excess_return_with_cost.annualized_return`` = 因子值最高组（Qn）等权组合
  年化收益。⚠️ 口径差异：Qlib 侧是对基准的**超额**收益，本适配器是**绝对**收益
  （本仓库引擎无基准组合概念），下游反馈解读必须带此语义（metric_semantics 字段）。
- ``1day.excess_return_with_cost.max_drawdown`` = Qn 组最大回撤（同上，绝对口径）。

进程契约（供 RD-Agent Runner 以子进程调用）:
- 入参: ``--factor-values <parquet> --start --end [--asset-type stock] [--n-groups 5]
  [--rebalance daily] [--qlib-instruments] [--data-dir <path>]``
- stdout 末行: JSON（NaN/Inf 已消毒为 null, 日期为 ISO 字符串）
- stderr: 进度/诊断行；exit 0 成功，exit 1 失败（JSON 内 status=error）

fail-closed 语义: 因子日期与评估窗口无交集、schema 缺列、qlib instrument 无法
映射时显式报错，绝不静默返回指标。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.factor import FactorBacktestService, FactorConfig
from scripts.export_daily_pv import from_qlib_symbol

FACTOR_COL = "f_ext"
# 评估窗口向前多取的自然日价格行（让评估窗口末端日也有次日收益可挂接）
_PRICE_EXTENSION_DAYS = 30
_WARMUP_DAYS = 120  # 与 FactorBacktestService._load_factor_panel 的预热窗口一致


PriceLoader = Callable[[date, date, list[str], str], pl.DataFrame]


@dataclass
class EvalError(Exception):
    """结构化评估失败（fail-closed）。"""

    code: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - 展示用途
        return f"{self.code}: {self.message}"


# 输入 parquet 的列名变体：仓库风格 (symbol, date) 或 RD-Agent 风格 (instrument, datetime)
_SYMBOL_COL_VARIANTS = ("symbol", "instrument")
_DATE_COL_VARIANTS = ("date", "datetime")
_VALUE_COL = "value"


def _resolve_column(columns: list[str], variants: tuple[str, ...]) -> str | None:
    for variant in variants:
        if variant in columns:
            return variant
    return None


def _normalize_factor_values(
    factor_values: pl.DataFrame,
    *,
    qlib_instruments: bool,
) -> pl.DataFrame:
    """统一输入 schema 为 (symbol, date, value)；qlib instrument 反向映射。"""
    columns = factor_values.columns
    symbol_col = _resolve_column(columns, _SYMBOL_COL_VARIANTS)
    date_col = _resolve_column(columns, _DATE_COL_VARIANTS)
    if symbol_col is None or date_col is None or _VALUE_COL not in columns:
        raise EvalError(
            "bad_schema",
            f"因子值 parquet 需要 ({'|'.join(_SYMBOL_COL_VARIANTS)}, {'|'.join(_DATE_COL_VARIANTS)}, value) 列，实际: {columns}",
        )
    out = factor_values.select(
        pl.col(symbol_col).cast(pl.String).alias("symbol"),
        pl.col(date_col).cast(pl.Date).alias("date"),
        pl.col(_VALUE_COL).cast(pl.Float64, strict=False).alias(FACTOR_COL),
    )
    if qlib_instruments:
        mapped: dict[str, str] = {}
        for instrument in out["symbol"].unique().to_list():
            try:
                mapped[instrument] = from_qlib_symbol(instrument)
            except ValueError as exc:
                raise EvalError("bad_symbol", f"qlib instrument 无法映射: {exc}") from exc
        out = out.with_columns(pl.col("symbol").replace_strict(mapped))
    return out


def evaluate_factor_values(
    factor_values: pl.DataFrame,
    *,
    start: date,
    end: date,
    price_loader: PriceLoader,
    asset_type: str = "stock",
    n_groups: int = 5,
    rebalance: str = "daily",
    qlib_instruments: bool = False,
) -> dict[str, Any]:
    """评估外部因子值并输出指标 dict（含 RD-Agent 三指标别名与全量诊断）。

    price_loader(start, end, columns, asset_type) 返回 enriched 价格面板；
    生产环境由 BacktestEngine.load_panel 闭包提供，测试注入合成面板。
    """
    if start > end:
        raise EvalError("bad_window", f"评估窗口非法: start={start} > end={end}")
    fv = _normalize_factor_values(factor_values, qlib_instruments=qlib_instruments)
    if fv.is_empty():
        raise EvalError("empty_factor", "因子值面板为空")
    if fv[FACTOR_COL].null_count() == fv.height:
        raise EvalError("all_nan", "因子值全为空，无法评估")
    f_min, f_max = fv["date"].min(), fv["date"].max()
    if f_max < start or f_min > end:
        raise EvalError(
            "date_mismatch",
            f"因子日期范围 [{f_min}, {f_max}] 与评估窗口 [{start}, {end}] 无交集",
        )

    # 预载价格面板：向前预热、向后扩展（供末端评估日的次日收益挂接）。
    # 鸭子引擎不裁剪窗口：FactorBacktestService 在内部按 [start, end] 过滤，
    # 超出窗口的价格行只参与前向收益计算，不进入指标分母。
    load_start = start - timedelta(days=_WARMUP_DAYS)
    serve_end = end + timedelta(days=_PRICE_EXTENSION_DAYS)
    columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
    prices = price_loader(load_start, serve_end, columns, asset_type)
    if prices is None or prices.is_empty():
        raise EvalError("no_price_data", f"评估窗口 [{load_start}, {serve_end}] 内无 enriched 价格数据")

    joined = prices.join(fv, on=["symbol", "date"], how="left")
    factor_symbols = set(fv["symbol"].unique().to_list())
    price_symbols = set(prices["symbol"].unique().to_list())
    unknown_symbols = sorted(factor_symbols - price_symbols)

    engine = _DuckPanelEngine(joined)
    service = FactorBacktestService(engine)  # type: ignore[arg-type]
    config = FactorConfig(
        factor_name=FACTOR_COL,
        symbols=None,
        start=start,
        end=end,
        n_groups=n_groups,
        rebalance=rebalance,  # type: ignore[arg-type]
        asset_type=asset_type,
    )
    result = service.run(config)

    payload: dict[str, Any] = {
        "status": "ok" if result.error is None else "error",
        "metrics": {},
        "metric_semantics": {
            "IC": "日截面 Rank IC 均值（方向: 因子值越大得分越高）",
            "1day.excess_return_with_cost.annualized_return": (
                "因子值最高组(Qn)等权组合年化收益；绝对收益口径，"
                "非 Qlib 的对基准超额收益"
            ),
            "1day.excess_return_with_cost.max_drawdown": "因子值最高组(Qn)组合最大回撤（绝对口径）",
        },
        "diagnostics": {},
        "meta": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "asset_type": asset_type,
            "n_groups": n_groups,
            "rebalance": rebalance,
            "unknown_symbols": unknown_symbols[:20],
            "unknown_symbol_count": len(unknown_symbols),
        },
    }
    if result.error is not None:
        payload["error"] = result.error
        return payload

    top_group = _top_group_stats(result.group_stats, n_groups)
    payload["metrics"] = {
        "IC": result.ic_mean,
        "IC_IR": result.ir,
        "IC_win_rate": result.ic_win_rate,
        "1day.excess_return_with_cost.annualized_return": top_group.get("annual_return"),
        "1day.excess_return_with_cost.max_drawdown": top_group.get("max_drawdown"),
        # 多空组合为理论价差组合（不做多/做空撮合），只有区间总收益无年化
        "long_short_total_return": (result.long_short_stats or {}).get("total_return"),
        "long_short_max_drawdown": (result.long_short_stats or {}).get("max_drawdown"),
        "long_short_sharpe": result.long_short_sharpe,
    }
    payload["diagnostics"] = asdict(result)
    payload["meta"].update(
        {
            "n_symbols": result.n_symbols,
            "n_dates": result.n_dates,
            "coverage": result.coverage,
            "turnover": result.turnover,
            "elapsed_ms": result.elapsed_ms,
        }
    )
    return payload


def _top_group_stats(group_stats: list[dict], n_groups: int) -> dict[str, Any]:
    """因子值最高组 = 分组序号最大的组（Qn，见 FactorBacktestService._add_groups）。"""
    if not group_stats:
        return {}
    target = max((int(row.get("group", 0)) for row in group_stats), default=0)
    for row in group_stats:
        if int(row.get("group", 0)) == target:
            return row
    return {}


class _DuckPanelEngine:
    """最小鸭子引擎：返回预载联合面板，列按请求裁剪。

    FactorBacktestService 只依赖 load_panel（其余 repo/data_generation 为软依赖）；
    它内部完成预热窗口请求、[start, end] 过滤与前向收益挂接。
    """

    def __init__(self, panel: pl.DataFrame) -> None:
        self._panel = panel

    def load_panel(self, symbols, start, end, columns, asset_type="stock", **_: Any) -> pl.DataFrame:
        # 服务请求列不含外部因子列，必须主动附加（联合面板已含 f_ext）
        names = list(dict.fromkeys([*(columns or []), FACTOR_COL]))
        selected = [c for c in names if c in self._panel.columns]
        return self._panel.select(selected)


def sanitize_json(value: Any) -> Any:
    """递归消毒为严格 JSON：非有限浮点 → None，date → ISO 字符串。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, Path)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


def _make_price_loader(data_dir: Path, asset_type: str) -> PriceLoader:
    from app.backtest.engine import BacktestEngine
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(data_dir)
    repo = KlineRepository(store)
    engine = BacktestEngine(repo)

    def loader(start: date, end: date, columns: list[str], loader_asset_type: str) -> pl.DataFrame:
        return engine.load_panel(None, start, end, columns=columns, asset_type=loader_asset_type)

    return loader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--factor-values", required=True, help="因子值 parquet (symbol/instrument, date/datetime, value)")
    parser.add_argument("--start", required=True, help="评估窗口起始 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="评估窗口结束 YYYY-MM-DD")
    parser.add_argument("--asset-type", default="stock", choices=["stock", "etf"])
    parser.add_argument("--n-groups", type=int, default=5)
    parser.add_argument("--rebalance", default="daily", choices=["daily", "weekly", "monthly"])
    parser.add_argument("--qlib-instruments", action="store_true", help="输入 instrument 为 Qlib 风格 (SH600901) 时开启")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据目录（默认走 settings.data_dir）")
    args = parser.parse_args(argv)

    def _progress(msg: str) -> None:
        print(f"PROGRESS {msg}", file=sys.stderr)

    try:
        factor_values = pl.read_parquet(args.factor_values)
        _progress(f"factor_values rows={len(factor_values)}")

        data_dir = args.data_dir
        if data_dir is None:
            from app.config import settings

            data_dir = settings.data_dir

        payload = evaluate_factor_values(
            factor_values,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            price_loader=_make_price_loader(data_dir, args.asset_type),
            asset_type=args.asset_type,
            n_groups=args.n_groups,
            rebalance=args.rebalance,
            qlib_instruments=args.qlib_instruments,
        )
    except EvalError as exc:
        payload = {"status": "error", "error_code": exc.code, "error": exc.message}
    except Exception as exc:  # noqa: BLE001 - 子进程契约: 任何失败都输出结构化 JSON
        payload = {"status": "error", "error_code": "internal", "error": f"{type(exc).__name__}: {exc}"}

    _progress(f"status={payload.get('status')}")
    print(json.dumps(sanitize_json(payload), ensure_ascii=False, allow_nan=False))
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
