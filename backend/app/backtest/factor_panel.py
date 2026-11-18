"""外部因子值面板评估（Phase B ai_mining 的评估内核）。

把外部生成的因子值（parquet: symbol/instrument + date/datetime + value）接入
FactorBacktestService：鸭子引擎零注册——服务只要求 panel 上存在因子列，
registry 白名单仅在 HTTP API 层。输出 RD-Agent 反馈契约的三指标别名 + 全量诊断。

语义口径（下游解读必读）:
- ``IC`` = 日截面 Rank IC 均值（方向: 因子值越大得分越高）
- ``annualized_return`` / ``max_drawdown`` = 因子值最高组（Qn）等权组合的
  **绝对**年化收益/回撤，非对基准的超额收益（本仓库引擎无基准组合概念）。

来源: Phase A 桥接验证的 scripts/factor_panel_eval.py 核心下沉（B-T2）；
scripts 侧保留薄 CLI 供外部子进程调用。
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.backtest.factor import FactorBacktestService, FactorConfig

FACTOR_COL = "f_ext"
# 评估窗口向前多取的自然日价格行（让评估窗口末端日也有次日收益可挂接）
_PRICE_EXTENSION_DAYS = 30
_WARMUP_DAYS = 120  # 与 FactorBacktestService._load_factor_panel 的预热窗口一致

PriceLoader = Callable[[date, date, list[str], str], pl.DataFrame]


class EvalError(Exception):
    """结构化评估失败（fail-closed），code 用于上层错误映射。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ── symbol 双向映射（仓库 `600901.SH` ↔ Qlib 风格 `SH600901`）──────────────

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
        raise ValueError(f"unexpected qlib instrument format: {instrument!r} (expect SH600901)")
    exchange, code = matched.group(1), matched.group(2)
    return f"{code}.{exchange}"


# ── 因子值输入归一化 ────────────────────────────────────────────────────

# 输入 parquet 的列名变体：仓库风格 (symbol, date) 或 RD-Agent 风格 (instrument, datetime)
_SYMBOL_COL_VARIANTS = ("symbol", "instrument")
_DATE_COL_VARIANTS = ("date", "datetime")
_VALUE_COL = "value"


def _resolve_column(columns: list[str], variants: tuple[str, ...]) -> str | None:
    for variant in variants:
        if variant in columns:
            return variant
    return None


def normalize_factor_values(
    factor_values: pl.DataFrame,
    *,
    qlib_instruments: bool,
) -> pl.DataFrame:
    """统一输入 schema 为 (symbol, date, f_ext)；qlib instrument 反向映射。"""
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
    """评估外部因子值并输出指标 dict（含三指标别名与全量诊断）。

    price_loader(start, end, columns, asset_type) 返回 enriched 价格面板；
    生产环境由 BacktestEngine.load_panel 闭包提供（make_price_loader），测试注入合成面板。
    """
    if start > end:
        raise EvalError("bad_window", f"评估窗口非法: start={start} > end={end}")
    fv = normalize_factor_values(factor_values, qlib_instruments=qlib_instruments)
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
                "因子值最高组(Qn)等权组合年化收益；绝对收益口径，非对基准超额收益"
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
    """最小鸭子引擎：返回预载联合面板，列按请求裁剪并附加因子列。

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
    """递归消毒为严格 JSON：非有限浮点 → None，date/Path → ISO 字符串。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, Path)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    return value


def make_price_loader(data_dir: Path, asset_type: str) -> PriceLoader:
    """构造走 BacktestEngine.load_panel 正规 enriched 路径的价格加载器。"""
    from app.backtest.engine import BacktestEngine
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(data_dir)
    repo = KlineRepository(store)
    engine = BacktestEngine(repo)

    def loader(start: date, end: date, columns: list[str], loader_asset_type: str) -> pl.DataFrame:
        return engine.load_panel(None, start, end, columns=columns, asset_type=loader_asset_type)

    return loader
