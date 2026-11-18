"""沙箱执行器测试: AST 拦截面 + 受限子进程执行 + 输出契约。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import polars as pl
import pytest

from app.ai_mining.sandbox import (
    FACTOR_VALUES_FILE,
    PANEL_DATA_FILE,
    FactorSandboxError,
    load_factor_values,
    run_factor_script,
    validate_factor_code,
)

VALID_FACTOR_CODE = '''
import pandas as pd

def calculate_mom5():
    df = pd.read_parquet("panel.parquet")
    df = df.sort_values(["symbol", "date"])
    close = df.set_index(["symbol", "date"])["close"].unstack(level="symbol")
    mom = close / close.shift(5) - 1.0
    out = mom.stack().rename("value").reset_index()
    out.to_parquet("factor_values.parquet", index=False)
    print("rows:", len(out))

if __name__ == "__main__":
    calculate_mom5()
'''


def _write_panel(workdir: Path, n_days: int = 30, symbols: tuple[str, ...] = ("600001.SH", "600002.SH")) -> None:
    rows = []
    for index, symbol in enumerate(symbols):
        base = pd.Timestamp("2026-06-01")
        for day in range(n_days):
            rows.append(
                {
                    "symbol": symbol,
                    "date": base + pd.Timedelta(days=day),
                    "open": 10.0 + index,
                    "high": 10.5 + index,
                    "low": 9.5 + index,
                    "close": 10.0 + index + day * (0.1 + index * 0.05),
                    "volume": 1000.0 + day,
                    "amount": 1e6 + day,
                    "turnover_rate": 0.02,
                }
            )
    pd.DataFrame(rows).to_parquet(workdir / PANEL_DATA_FILE, index=False)


class TestValidateFactorCode:
    def test_valid_contract_passes(self):
        validate_factor_code(VALID_FACTOR_CODE)

    def test_from_import_parquet_io_passes(self):
        code = "from pandas import read_parquet\nx = read_parquet\n"
        validate_factor_code(code)

    @pytest.mark.parametrize(
        "bad",
        [
            "import subprocess\n",
            "import socket\n",
            "import os\n",
            "from pandas import read_pickle\n",
            "from pandas import read_csv\n",
            "import pandas as pd\npd.read_pickle('x')\n",
            "import pandas as pd\npd.read_csv('x')\n",
            "eval('1+1')\n",
            "__import__('os')\n",
            "open('panel.parquet')\n",
            "import numpy as np\nnp.save('x.npy', np.ones(3))\n",
        ],
    )
    def test_dangerous_patterns_rejected(self, bad: str):
        with pytest.raises(FactorSandboxError, match=r"validation|syntax"):
            validate_factor_code(bad)

    def test_syntax_error_code(self):
        with pytest.raises(FactorSandboxError, match="syntax"):
            validate_factor_code("def broken(:\n")


class TestRunFactorScript:
    def test_happy_path_end_to_end(self, tmp_path: Path):
        _write_panel(tmp_path)
        (tmp_path / "factor.py").write_text(VALID_FACTOR_CODE)
        result = run_factor_script(tmp_path, timeout=60)
        assert result.ok, f"stderr={result.stderr}"
        values = load_factor_values(tmp_path / FACTOR_VALUES_FILE)
        assert values.columns == ["symbol", "date", "value"]
        assert values["value"].null_count() < values.height
        # pandas stack() 保留 NaN（future_stack 语义），预热段为 null 行；首个有效值在预热后
        first_valid = values.filter(pl.col("value").is_not_null())["date"].min()
        assert first_valid == date(2026, 6, 6)  # 5 日预热后首日

    def test_timeout_kills_busy_loop(self, tmp_path: Path):
        (tmp_path / "factor.py").write_text("while True:\n    pass\n")
        result = run_factor_script(tmp_path, timeout=3)
        assert result.timed_out
        assert not result.ok

    def test_runtime_error_captured(self, tmp_path: Path):
        (tmp_path / "factor.py").write_text('raise ValueError("boom")\n')
        result = run_factor_script(tmp_path, timeout=30)
        assert not result.ok
        assert "boom" in result.stderr
        assert "boom" in result.error_feedback()

    def test_missing_script_raises(self, tmp_path: Path):
        with pytest.raises(FactorSandboxError, match="no_script"):
            run_factor_script(tmp_path)


class TestLoadFactorValues:
    def test_missing_output_raises(self, tmp_path: Path):
        with pytest.raises(FactorSandboxError, match="no_output"):
            load_factor_values(tmp_path / "none.parquet")

    def test_missing_column_rejected(self, tmp_path: Path):
        df = pl.DataFrame({"symbol": ["a"], "date": [date(2026, 1, 1)]})
        df.write_parquet(tmp_path / "v.parquet")
        with pytest.raises(FactorSandboxError, match="缺少必需列"):
            load_factor_values(tmp_path / "v.parquet")

    def test_all_null_rejected(self, tmp_path: Path):
        df = pl.DataFrame({"symbol": ["a"], "date": [date(2026, 1, 1)], "value": [None]}, schema={"symbol": pl.String, "date": pl.Date, "value": pl.Float64})
        df.write_parquet(tmp_path / "v.parquet")
        with pytest.raises(FactorSandboxError, match="全为空"):
            load_factor_values(tmp_path / "v.parquet")

    def test_bad_date_type_rejected(self, tmp_path: Path):
        df = pl.DataFrame({"symbol": ["a"], "date": ["not-a-date"], "value": [1.0]})
        df.write_parquet(tmp_path / "v.parquet")
        with pytest.raises(FactorSandboxError, match="bad_output"):
            load_factor_values(tmp_path / "v.parquet")

    def test_null_symbol_rejected(self, tmp_path: Path):
        df = pl.DataFrame(
            {"symbol": ["a", None], "date": [date(2026, 1, 1), date(2026, 1, 2)], "value": [1.0, 2.0]}
        )
        df.write_parquet(tmp_path / "v.parquet")
        with pytest.raises(FactorSandboxError, match="symbol"):
            load_factor_values(tmp_path / "v.parquet")

    def test_date_range_and_nulls_kept(self, tmp_path: Path):
        rows = []
        base = date(2026, 1, 1)
        for i in range(6):
            rows.append({"symbol": "600001.SH", "date": base + timedelta(days=i), "value": float(i) if i else None})
        df = pl.DataFrame(rows)
        df.write_parquet(tmp_path / "v.parquet")
        out = load_factor_values(tmp_path / "v.parquet")
        assert out.height == 6  # 允许部分缺失（未覆盖日），仅拒绝全空
