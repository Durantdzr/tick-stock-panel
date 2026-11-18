"""ai_mining 的 worker 运行体: MiningJobManager payload → 面板供给 → 同步循环。

payload 形状 (与 mining_manager._run_job 对齐):
    {"run_id": str, "request": {rounds, factors_per_round, eval_start, eval_end,
                                max_repairs, exec_timeout, max_llm_calls,
                                eval_days_hint, asset_type}}
run 目录约定: ``{data_dir}/research/{RUNS_NAMESPACE}/runs/{run_id}/``。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.ai_mining.loop import LoopConfig, run_loop_sync
from app.backtest.factor_panel import make_price_loader, sanitize_json

RUNS_NAMESPACE = "llm_evo"

_PANEL_WARMUP_DAYS = 180  # 因子自算窗口的预热余量（自然日，宁多勿少）


def run_dir_for(data_dir: Path, run_id: str) -> Path:
    return Path(data_dir) / "research" / RUNS_NAMESPACE / "runs" / run_id


def run_ai_mining_runtime(
    config: dict[str, Any],
    *,
    data_dir: Path,
    engine,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_check: Any = None,
    rss_sampler: Any = None,
) -> dict[str, Any]:
    """worker 子进程入口（_worker_entry 分发）。engine 为 BacktestEngine（面板供给与评估共用）。"""
    _ = rss_sampler  # 采样由 worker 外层统一处理; 循环自身无阶段级采样需求
    run_id = str(config.get("run_id") or "")
    if not run_id:
        raise ValueError("llm_evo runtime requires run_id")
    request = dict(config.get("request") or {})
    run_dir = run_dir_for(data_dir, run_id)
    eval_start = date.fromisoformat(str(request["eval_start"]))
    eval_end = date.fromisoformat(str(request["eval_end"]))
    loop_config = LoopConfig(
        run_dir=run_dir,
        eval_start=eval_start,
        eval_end=eval_end,
        rounds=int(request.get("rounds", 3)),
        factors_per_round=int(request.get("factors_per_round", 2)),
        max_repairs=int(request.get("max_repairs", 3)),
        exec_timeout=float(request.get("exec_timeout", 180.0)),
        max_llm_calls=int(request.get("max_llm_calls", 120)),
        eval_days_hint=int(request.get("eval_days_hint", 90)),
    )

    # 面板供给: 评估窗口 + 预热段一次性写 run_dir/panel.parquet（因子工作目录符号链接共享）
    run_dir.mkdir(parents=True, exist_ok=True)
    panel_path = run_dir / "panel.parquet"
    if not panel_path.is_file():
        columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover_rate"]
        panel = engine.load_panel(
            None,
            eval_start - timedelta(days=_PANEL_WARMUP_DAYS),
            eval_end,
            columns=columns,
            asset_type=str(request.get("asset_type", "stock")),
        )
        panel.write_parquet(panel_path)

    price_loader = make_price_loader(data_dir, str(request.get("asset_type", "stock")))
    summary = run_loop_sync(
        loop_config,
        price_loader=price_loader,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )

    # SOTA 落地: 循环完成且未被取消时, 把优胜因子经 ext_data 注册进面板
    if summary.get("stopped_reason") == "completed":
        from app.ai_mining.ext_landing import land_sota_factors
        from app.ai_mining.memory import load_memory

        if cancel_check is None or not getattr(cancel_check, "is_set", lambda: bool(cancel_check))():
            memory = load_memory(run_dir)
            landed = land_sota_factors(run_dir, memory, data_dir)
            if landed:
                summary["landed_ext_factors"] = landed
    return sanitize_json(summary)
