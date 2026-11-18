"""外部因子值评估 CLI 薄壳（核心在 app/backtest/factor_panel.py）。

供外部子进程（如 RD-Agent 桥接 Runner）以稳定契约调用:
- 入参: ``--factor-values <parquet> --start --end [--asset-type stock] [--n-groups 5]
  [--rebalance daily] [--qlib-instruments] [--data-dir <path>]``
- stdout 末行: JSON（NaN/Inf 已消毒为 null, 日期为 ISO 字符串）
- stderr: 进度/诊断行；exit 0 成功，exit 1 失败（JSON 内 status=error）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.factor_panel import (
    EvalError,
    evaluate_factor_values,
    make_price_loader,
    sanitize_json,
)


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
            price_loader=make_price_loader(data_dir, args.asset_type),
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
