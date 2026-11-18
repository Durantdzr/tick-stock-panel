"""export_daily_pv 适配器测试：symbol 映射可逆性 + daily_pv 列契约。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.backtest.factor_panel import from_qlib_symbol, to_qlib_symbol
from scripts.export_daily_pv import panel_to_daily_pv, warmup_start


def _synthetic_panel(rows: list[dict] | None = None) -> pl.DataFrame:
    if rows is None:
        rows = [
            {"symbol": "600901.SH", "date": date(2026, 11, 13), "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000.0},
            {"symbol": "600901.SH", "date": date(2026, 11, 16), "open": 10.5, "high": 11.5, "low": 10.0, "close": 11.2, "volume": 1200.0},
            {"symbol": "002434.SZ", "date": date(2026, 11, 13), "open": 5.0, "high": 5.4, "low": 4.8, "close": 5.2, "volume": 3000.0},
            {"symbol": "002434.SZ", "date": date(2026, 11, 16), "open": 5.2, "high": 5.6, "low": 5.1, "close": 5.5, "volume": 2800.0},
        ]
    return pl.DataFrame(rows)


class TestSymbolMapping:
    def test_to_qlib(self):
        assert to_qlib_symbol("600901.SH") == "SH600901"
        assert to_qlib_symbol("002434.SZ") == "SZ002434"
        assert to_qlib_symbol("830001.BJ") == "BJ830001"

    def test_from_qlib(self):
        assert from_qlib_symbol("SH600901") == "600901.SH"
        assert from_qlib_symbol("SZ002434") == "002434.SZ"

    def test_roundtrip_reversible(self):
        for symbol in ("600901.SH", "002434.SZ", "830001.BJ", "000001.SZ"):
            assert from_qlib_symbol(to_qlib_symbol(symbol)) == symbol

    @pytest.mark.parametrize("bad", ["600901", "600901.", ".SH", "XXXXXX.SH", "600901.XX", "60090.SH"])
    def test_to_qlib_malformed_fail_closed(self, bad: str):
        with pytest.raises(ValueError, match="repo symbol"):
            to_qlib_symbol(bad)

    @pytest.mark.parametrize("bad", ["600901", "SH60090", "sh600901", "600901.SH", "XX600901"])
    def test_from_qlib_malformed_fail_closed(self, bad: str):
        with pytest.raises(ValueError, match="qlib instrument"):
            from_qlib_symbol(bad)


class TestPanelToDailyPv:
    def test_columns_and_rename(self):
        out = panel_to_daily_pv(_synthetic_panel())
        assert out.columns == ["datetime", "instrument", "$open", "$close", "$high", "$low", "$volume", "$factor"]
        # 排序键 (datetime, instrument)，"SH600901" < "SZ002434" 字典序
        assert out["instrument"].to_list() == ["SH600901", "SZ002434", "SH600901", "SZ002434"]
        assert out["datetime"].to_list() == [date(2026, 11, 13), date(2026, 11, 13), date(2026, 11, 16), date(2026, 11, 16)]

    def test_factor_constant_and_prices_passthrough(self):
        out = panel_to_daily_pv(_synthetic_panel())
        assert out["$factor"].unique().to_list() == [1.0]
        assert out["$close"].to_list() == [10.5, 5.2, 11.2, 5.5]
        assert out["$volume"].dtype == pl.Float64

    def test_empty_panel_raises(self):
        with pytest.raises(ValueError, match="empty"):
            panel_to_daily_pv(_synthetic_panel(rows=[]))

    def test_missing_column_raises(self):
        panel = _synthetic_panel().drop("volume")
        with pytest.raises(ValueError, match="missing required columns"):
            panel_to_daily_pv(panel)


class TestWarmupStart:
    def test_falls_back_by_trading_days(self, tmp_path: Path):
        dates = [date(2026, 1, i) for i in range(1, 6)]
        for d in dates:
            partition = tmp_path / "kline_daily_enriched" / f"date={d.isoformat()}"
            partition.mkdir(parents=True)
            (partition / "part.parquet").write_bytes(b"")
        start = date(2026, 1, 5)
        assert warmup_start(tmp_path, "stock", start, warmup_bars=2) == date(2026, 1, 3)
        assert warmup_start(tmp_path, "stock", start, warmup_bars=50) == dates[0]

    def test_no_partitions_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="no enriched partitions"):
            warmup_start(tmp_path, "stock", date(2026, 1, 1), 10)

    def test_start_before_range_clamps_to_first(self, tmp_path: Path):
        partition = tmp_path / "kline_daily_enriched" / "date=2026-03-02"
        partition.mkdir(parents=True)
        (partition / "part.parquet").write_bytes(b"")
        assert warmup_start(tmp_path, "stock", date(2025, 1, 1), 10) == date(2026, 3, 2)
