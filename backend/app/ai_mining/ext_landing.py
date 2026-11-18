"""SOTA 因子 ext_data 落地: 进化产物注册为面板可用的 ext_ 因子。

经既有 ext_data 通道（write_ext_parquet 逐日分区 + 自动缓存失效），
``GET /api/factors`` / 挖掘 / 因子回测零改动即可消费 ``ext_llmevo_<id>_value``。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.ai_mining.memory import EvolutionMemory, SotaFactor
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, write_ext_parquet

EXT_CONFIG_PREFIX = "llmevo_"


def land_sota_factors(run_dir: Path, memory: EvolutionMemory, data_dir: Path) -> list[str]:
    """把 run 的 SOTA 因子值逐日写入 ext_data 并注册；返回落地的 config id 列表。

    同名因子多轮出现时取最后一轮的值（最新最优）。失败不中断主流程:
    单个因子落地失败记录后继续（产物仍在 run 目录，可手工恢复）。
    """
    store = ExtConfigStore(data_dir)
    by_id: dict[str, tuple[SotaFactor, Path]] = {}
    for sota in memory.sota_factors:
        values_path = run_dir / sota.values_path
        if values_path.is_file():
            by_id[sota.id] = (sota, values_path)  # 后轮覆盖前轮

    landed: list[str] = []
    for factor_id, (sota, values_path) in sorted(by_id.items()):
        config_id = f"{EXT_CONFIG_PREFIX}{factor_id.lower()}"
        try:
            config = ExtConfig(
                id=config_id,
                label=f"AI进化·{factor_id} (R{sota.round})",
                mode="timeseries",
                fields=[ExtField(name="value", dtype="float", label=factor_id)],
            )
            store.upsert(config)
            values = pl.read_parquet(values_path).select(
                pl.col("symbol").cast(pl.String),
                pl.col("date").cast(pl.Date),
                pl.col("value").cast(pl.Float64),
            )
            for day in sorted(values["date"].unique().to_list()):
                partition = values.filter(pl.col("date") == day).select("symbol", "value")
                write_ext_parquet(partition, config, data_dir, snapshot_date=date(day.year, day.month, day.day))
            landed.append(config_id)
        except Exception:  # noqa: BLE001 - 落地失败不中断主流程
            continue
    return landed
