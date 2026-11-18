"""SOTA 因子 ext_data 落地测试: upsert→逐日分区→注册可见→可评估。"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.ai_mining.ext_landing import land_sota_factors
from app.ai_mining.memory import EvolutionMemory, SotaFactor, load_memory
from app.factors import ext_factors
from app.factors.registry import all_factors
from app.services.ext_data import _load_all_cache


@pytest.fixture(autouse=True)
def _clean_caches(tmp_path, monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    _load_all_cache.clear()
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    yield
    _load_all_cache.clear()
    ext_factors._frame_cache.clear()
    ext_factors._sync_state = None
    from app.factors import registry as _registry

    for fid in [k for k in list(_registry._REGISTRY) if k.startswith(ext_factors.EXT_PREFIX)]:
        _registry.unregister_factor(fid)


def _prepare_run(tmp_path: Path) -> Path:
    values = pl.DataFrame(
        {
            "symbol": [f"6000{i:02d}.SH" for i in range(12) for _ in range(3)],
            "date": [d for _ in range(12) for d in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5))],
            "value": [float(i + day_offset) for i in range(12) for day_offset in range(3)],
        }
    )
    factor_dir = tmp_path / "rounds" / "1" / "LOWVOL20"
    factor_dir.mkdir(parents=True)
    values.write_parquet(factor_dir / "factor_values.parquet")
    memory = EvolutionMemory(
        sota_factors=[
            SotaFactor(
                id="LOWVOL20", name="LOWVOL20", description="低波动", formulation="-std", round=1,
                ic=0.09, ic_ir=0.4, values_path="rounds/1/LOWVOL20/factor_values.parquet",
            )
        ]
    )
    land_target = tmp_path / "run"
    land_target.mkdir(parents=True)
    from app.ai_mining.memory import save_memory

    save_memory(land_target, memory)
    # values 实际文件位置要与 memory.values_path 对齐
    (land_target / "rounds" / "1" / "LOWVOL20").mkdir(parents=True, exist_ok=True)
    values.write_parquet(land_target / "rounds" / "1" / "LOWVOL20" / "factor_values.parquet")
    return land_target


def test_land_registers_factor(tmp_path: Path):
    run_dir = _prepare_run(tmp_path)
    memory = load_memory(run_dir)
    landed = land_sota_factors(run_dir, memory, tmp_path)
    assert landed == ["llmevo_lowvol20"]

    # ext_data 分区落盘
    partition = tmp_path / "ext_data" / "llmevo_lowvol20" / "timeseries" / "date=2026-08-03" / "part.parquet"
    assert partition.is_file()

    # 注册表可见 (惰性同步)
    ids = {spec.id for spec in all_factors()}
    assert "ext_llmevo_lowvol20_value" in ids


def test_latest_round_wins_on_duplicate_id(tmp_path: Path):
    run_dir = _prepare_run(tmp_path)
    memory = load_memory(run_dir)
    # 同 id 第二轮: 值全为 99 (可区分)
    factor_dir2 = run_dir / "rounds" / "2" / "LOWVOL20"
    factor_dir2.mkdir(parents=True)
    values2 = pl.DataFrame(
        {
            "symbol": ["600000.SH"] * 3,
            "date": [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)],
            "value": [99.0, 99.0, 99.0],
        }
    )
    values2.write_parquet(factor_dir2 / "factor_values.parquet")
    memory.sota_factors.append(
        SotaFactor(
            id="LOWVOL20", name="LOWVOL20", description="低波动v2", formulation="-std2", round=2,
            values_path="rounds/2/LOWVOL20/factor_values.parquet",
        )
    )
    landed = land_sota_factors(run_dir, memory, tmp_path)
    assert landed == ["llmevo_lowvol20"]
    partition = pl.read_parquet(tmp_path / "ext_data" / "llmevo_lowvol20" / "timeseries" / "date=2026-08-03" / "part.parquet")
    assert partition["value"].to_list() == [99.0]  # 最新一轮覆盖
