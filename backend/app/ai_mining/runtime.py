"""ai_mining 的 worker 运行体: 配置解析 → 面板供给 → 同步循环。"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.ai_mining.loop import LoopConfig, run_loop_sync
from app.backtest.factor_panel import make_price_loader, sanitize_json

_PANEL_WARMUP_DAYS = 180  # 因子自算窗口的预热余量（自然日，宁多勿少）


def run_ai_mining_runtime(
    config: dict[str, Any],
    *,
    data_dir: Path,
    engine,
    progress_cb: Callable[[dict], None] | None = None,
    cancel_check: Any = None,
    rss_sampler: Any = None,
) -> dict[str, Any]:
    """worker 子进程入口（_worker_entry 分发）。engine 为 BacktestEngine（评估与面板供给共用）。"""
    _ = rss_sampler  # 采样由 worker 外层统一处理; 循环自身无阶段级采样需求
    run_dir = Path(config["run_dir"])
    eval_start = date.fromisoformat(str(config["eval_start"]))
    eval_end = date.fromisoformat(str(config["eval_end"]))
    loop_config = LoopConfig(
        run_dir=run_dir,
        eval_start=eval_start,
        eval_end=eval_end,
        rounds=int(config.get("rounds", 3)),
        factors_per_round=int(config.get("factors_per_round", 2)),
        max_repairs=int(config.get("max_repairs", 3)),
        exec_timeout=float(config.get("exec_timeout", 180.0)),
        max_llm_calls=int(config.get("max_llm_calls", 120)),
        eval_days_hint=int(config.get("eval_days_hint", 90)),
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
            asset_type=str(config.get("asset_type", "stock")),
        )
        panel.write_parquet(panel_path)

    price_loader = make_price_loader(data_dir, str(config.get("asset_type", "stock")))
    summary = run_loop_sync(
        loop_config,
        price_loader=price_loader,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
    )
    return sanitize_json(summary)
