"""factor_panel_eval 适配器测试：鸭子引擎合成面板覆盖正常/全 NaN/日期越界/消毒路径。

模仿 tests/backtest/test_factor_batch.py 的鸭子引擎模式：price_loader 注入合成
enriched 面板，不触真实数据目录。
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

import polars as pl
import pytest

from app.backtest.factor_panel import (
    EvalError,
    evaluate_factor_values,
    normalize_factor_values,
    sanitize_json,
)

EVAL_START = date(2026, 6, 1)
EVAL_END = date(2026, 6, 30)
N_SYMBOLS = 6


def _synthetic_prices(start_offset_days: int = 0) -> pl.DataFrame:
    """45 个自然日 × N symbol 的合成 enriched 面板（覆盖评估窗口 + 前后扩展）。"""
    rows = []
    for index in range(N_SYMBOLS):
        for day in range(start_offset_days, start_offset_days + 45):
            current = date(2026, 5, 20) + timedelta(days=day)
            rows.append(
                {
                    "symbol": f"60000{index}.SH",
                    "date": current,
                    "open": 10.0 + index + day * 0.05,
                    "high": 10.5 + index + day * 0.05,
                    "low": 9.5 + index + day * 0.05,
                    # 让不同 symbol 的收益差异确定（index 驱动斜率），IC 可断言方向
                    "close": 10.0 + index * 2 + day * (0.1 + index * 0.05),
                    "volume": 1000.0 + index * 100 + day,
                    "amount": 1e6 + index * 1000 + day,
                    "turnover_rate": 0.02 + 0.001 * (day % 5),
                }
            )
    return pl.DataFrame(rows)


def _loader_for(panel: pl.DataFrame):
    def loader(start, end, columns, asset_type):
        assert asset_type == "stock"
        selected = [c for c in columns if c in panel.columns]
        return panel.filter((pl.col("date") >= start) & (pl.col("date") <= end)).select(selected)

    return loader


def _factor_values(values_by_index: dict[int, float] | None = None) -> pl.DataFrame:
    """与价格面板同覆盖的因子值（默认: 因子 = symbol index，方向已知）。"""
    rows = []
    for index in range(N_SYMBOLS):
        for day in range(12, 42):  # 覆盖 6/1..6/30 评估窗口
            current = date(2026, 5, 20) + timedelta(days=day)
            value = float(index) if values_by_index is None else values_by_index.get(index)
            rows.append({"symbol": f"60000{index}.SH", "date": current, "value": value})
    return pl.DataFrame(rows)


class TestHappyPath:
    def test_full_metrics_with_aliases(self):
        payload = evaluate_factor_values(
            _factor_values(),
            start=EVAL_START,
            end=EVAL_END,
            price_loader=_loader_for(_synthetic_prices()),
        )
        assert payload["status"] == "ok"
        metrics = payload["metrics"]
        assert metrics["IC"] is not None and math.isfinite(metrics["IC"])
        assert -1.0 <= metrics["IC"] <= 1.0
        # 合成数据 IC 恒定（std=0）时 IR 允许为 None，非恒定序列才有 IR
        if metrics["IC_IR"] is not None:
            assert math.isfinite(metrics["IC_IR"])
        alias_return = metrics["1day.excess_return_with_cost.annualized_return"]
        alias_dd = metrics["1day.excess_return_with_cost.max_drawdown"]
        assert alias_return is not None and math.isfinite(alias_return)
        assert alias_dd is not None and alias_dd <= 0
        # 语义说明必须随行，防下游误读绝对/超额口径
        assert "绝对" in payload["metric_semantics"]["1day.excess_return_with_cost.annualized_return"]
        # 诊断: 分层/多空/覆盖
        diag = payload["diagnostics"]
        assert len(diag["group_stats"]) == 5
        assert diag["coverage"] is not None and diag["coverage"] > 0.9
        assert diag["long_short_stats"]
        # meta 汇总
        assert payload["meta"]["n_symbols"] == N_SYMBOLS

    def test_top_group_is_highest_factor_bucket(self):
        # 合成数据: symbol index 越大收益斜率越高且因子值=index → Qn(最高因子组)
        # 年化收益应显著优于 Q1（低因子组），验证"最高组"方向语义
        payload = evaluate_factor_values(
            _factor_values(),
            start=EVAL_START,
            end=EVAL_END,
            price_loader=_loader_for(_synthetic_prices()),
        )
        groups = {int(row["group"]): row for row in payload["diagnostics"]["group_stats"]}
        assert groups[5]["annual_return"] > groups[1]["annual_return"]


class TestFailClosed:
    def test_all_nan_factor_errors(self):
        # 因子日期覆盖窗口（过掉前置守卫），但窗口内值全为空 → 服务级"过滤后无有效数据"
        rows = []
        for index in range(N_SYMBOLS):
            for day in range(5, 50):
                current = date(2026, 5, 20) + timedelta(days=day)
                in_eval_window = EVAL_START <= current <= EVAL_END
                value = None if in_eval_window else float(index)
                rows.append({"symbol": f"60000{index}.SH", "date": current, "value": value})
        payload = evaluate_factor_values(
            pl.DataFrame(rows),
            start=EVAL_START,
            end=EVAL_END,
            price_loader=_loader_for(_synthetic_prices()),
        )
        assert payload["status"] == "error"
        assert "无有效数据" in payload["error"]

    def test_date_mismatch_errors(self):
        fv = _factor_values()
        fv = fv.with_columns(pl.col("date").dt.replace(year=2025))
        with pytest.raises(EvalError, match="无交集"):
            evaluate_factor_values(
                fv,
                start=EVAL_START,
                end=EVAL_END,
                price_loader=_loader_for(_synthetic_prices()),
            )

    def test_all_nan_input_raises_before_service(self):
        fv = _factor_values()
        fv = fv.with_columns(pl.lit(None, dtype=pl.Float64).alias("value"))
        with pytest.raises(EvalError, match="全为空"):
            evaluate_factor_values(
                fv,
                start=EVAL_START,
                end=EVAL_END,
                price_loader=_loader_for(_synthetic_prices()),
            )

    def test_bad_schema(self):
        with pytest.raises(EvalError, match="bad_schema"):
            normalize_factor_values(pl.DataFrame({"foo": [1]}), qlib_instruments=False)

    def test_empty_price_data_errors(self):
        with pytest.raises(EvalError, match="无 enriched"):
            evaluate_factor_values(
                _factor_values(),
                start=EVAL_START,
                end=EVAL_END,
                price_loader=lambda start, end, columns, asset_type: pl.DataFrame(),
            )


class TestQlibInstruments:
    def test_mapping_applied(self):
        fv = _factor_values().with_columns(
            pl.concat_str(
                pl.col("symbol").str.split(".").list.get(1),
                pl.col("symbol").str.split(".").list.get(0),
            ).alias("symbol")
        )
        payload = evaluate_factor_values(
            fv,
            start=EVAL_START,
            end=EVAL_END,
            price_loader=_loader_for(_synthetic_prices()),
            qlib_instruments=True,
        )
        assert payload["status"] == "ok"
        assert payload["meta"]["unknown_symbol_count"] == 0

    def test_unmappable_instrument_raises(self):
        fv = _factor_values().with_columns(pl.lit("XX123456").alias("symbol"))
        with pytest.raises(EvalError, match="无法映射"):
            evaluate_factor_values(
                fv,
                start=EVAL_START,
                end=EVAL_END,
                price_loader=_loader_for(_synthetic_prices()),
                qlib_instruments=True,
            )


class TestSanitize:
    def test_nan_inf_become_null(self):
        cleaned = sanitize_json(
            {"a": float("nan"), "b": float("inf"), "c": [1.5, float("-inf")], "d": {"e": date(2026, 1, 1)}}
        )
        assert cleaned["a"] is None
        assert cleaned["b"] is None
        assert cleaned["c"] == [1.5, None]
        assert cleaned["d"]["e"] == "2026-01-01"
        json.dumps(cleaned, allow_nan=False)  # 严格 JSON 可序列化

    def test_finite_values_pass_through(self):
        assert sanitize_json(0.25) == 0.25
        assert sanitize_json("文本") == "文本"
