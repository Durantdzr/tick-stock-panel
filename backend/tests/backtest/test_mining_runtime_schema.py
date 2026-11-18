"""folds 产物帧的显式 schema 契约 — daily_returns 列必须声明 List(Float64)。

折行混合 None/list 时依赖 Polars 推断: 超过默认 infer_schema_length=100 且前
100 行全为 None (大面积 skip 的运行) 会推断成 Null 后抛 SchemaError, 整次
挖掘失败。声明进 schema_overrides 常量, 此测试钉死该声明存在。
"""

from __future__ import annotations

import polars as pl

from app.backtest.mining_runtime import _FOLDS_SCHEMA_OVERRIDES


def test_folds_schema_declares_daily_returns_list_column():
    assert _FOLDS_SCHEMA_OVERRIDES["daily_returns"] == pl.List(pl.Float64)


def test_mixed_none_and_list_rows_infer_list_dtype_under_declared_override():
    # 101 行前段全 None + 尾段 list: 显式 override 下推断稳定为 List(Float64)
    rows = [
        {"total_return": 0.1, "daily_returns": None} for _ in range(101)
    ]
    rows.append({"total_return": 0.2, "daily_returns": [0.01, -0.01]})
    frame = pl.DataFrame(rows, schema_overrides=_FOLDS_SCHEMA_OVERRIDES)
    assert frame.schema["daily_returns"] == pl.List(pl.Float64)
