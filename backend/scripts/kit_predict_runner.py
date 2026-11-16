#!/usr/bin/env python
"""KiT 预测 runner — 必须在 KiT venv 中运行 (依赖 kit_v1/torch, 不 import 本项目 app.*)。

由 backend/app/services/kit_prediction.py 通过子进程调用:
  stdin  : 一个 JSON payload (契约见 docs/superpowers/plans/2026-11-17-kit-prediction.md Task 1)
  stdout : 最后一行 JSON 结果; 失败时 {"ok": false, ...} 且 exit 1
  stderr : PROGRESS <token> 进度行 (loading_model / sampling / calendar_fallback_weekend_skip)

ckpt="none" 仅为冒烟模式: 随机 tiny 模型 + 全 1 归一化 scale, 不读 checkpoint 目录。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


def _progress(token: str) -> None:
    print(f"PROGRESS {token}", file=sys.stderr, flush=True)


def _build_trading_days(market: str, df: pd.DataFrame):
    """trading_days 表 (datetime64[D] 升序) + 日历标识; crypto 全年历日, 其余用 exchange_calendars。"""
    start = df["datetime"].min().normalize() - pd.Timedelta(days=30)
    end = df["datetime"].max().normalize() + pd.Timedelta(days=45)
    if market == "crypto":
        return pd.date_range(start, end, freq="D").values, "alldays"
    try:
        import exchange_calendars as xcals
    except ImportError:
        _progress("calendar_fallback_weekend_skip")
        return None, "weekend_skip"
    code = "XSHG" if market == "ashare" else "XNYS"
    try:
        cal = xcals.get_calendar(code, start=start.date(), end=end.date())
    except Exception:  # noqa: BLE001 — exchange_calendars 节假日表只发布到当年年底,
        # 越界(如跨年预测)实例化会抛错; 回退仅跳周末, 节假日日期可能不准。
        _progress("calendar_fallback_weekend_skip")
        return None, "weekend_skip"
    return cal.sessions.values, code.lower()


def _lookup_instrument(ckpt_dir: Path, symbol: str) -> tuple[int, int, bool]:
    """从 checkpoint 的 instruments.json 查 (vocab_index, sector_id, matched); 未命中回退 0。"""
    path = ckpt_dir / "instruments.json"
    if not path.is_file() or not symbol:
        return 0, 0, False
    try:
        table = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return 0, 0, False
    entry = table.get(symbol) if isinstance(table, dict) else None
    if not isinstance(entry, dict):
        return 0, 0, False
    inst_id = int(entry.get("vocab_index", 0) or 0)
    sector_id = int(entry.get("sector", 0) or 0)
    return inst_id, sector_id, inst_id > 0


def _fail(error_type: str, message: str) -> None:
    print(json.dumps({"ok": False, "error_type": error_type, "error": message}, ensure_ascii=False))
    sys.exit(1)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        _fail("bad_request", f"stdin JSON 解析失败: {e}")

    try:
        kit_root = payload["kit_root"]
        sys.path.insert(0, kit_root)
        import torch
        from kit_v1.infer.loader import load_model_and_config
        from kit_v1.infer.postprocess import denormalize_paths, paths_to_ohlcv
        from kit_v1.infer.sampler import flow_sample
        from kit_v1.infer.window import prepare_inference_window
    except Exception as e:  # noqa: BLE001 — 环境错误统一 fail-closed
        _fail("env_error", f"KiT 环境不可用 (kit_root={kit_root}): {e}")

    market = payload["market"]
    timescale = payload["timescale"]
    symbol = payload.get("symbol", "")
    seed = int(payload.get("seed", 0))
    n_paths = int(payload.get("paths", 16))
    steps = int(payload.get("steps", 32))
    t0 = time.monotonic()

    ckpt = payload["ckpt"]
    smoke = ckpt.lower() == "none"

    try:
        _progress("loading_model")
        if smoke:
            # 冒烟: 随机 tiny 模型, 不读 ckpt 目录, 归一化 scale 全 1
            model, cfg = load_model_and_config("none", preset="tiny", seed=seed)
            from kit_v1.data.features import FEATURE_NAMES
            stats = {f"{market}|{timescale}|{name}": 1.0 for name in FEATURE_NAMES}
            inst_id, sector_id, matched = 0, 0, False
        else:
            ckpt_dir = Path(ckpt).parent
            model, cfg = load_model_and_config(ckpt, preset="kit_l", seed=seed)
            stats = json.loads((ckpt_dir / "stats.json").read_text())
            inst_id, sector_id, matched = _lookup_instrument(ckpt_dir, symbol)
        model.eval()

        Lc, Lh = cfg.data.window_for(timescale)  # noqa: N806 — 与 KiT 上游命名一致
        half_life_days = float(cfg.data.vol_ema_half_life_days)

        df = pd.DataFrame(payload["bars"])
        df["datetime"] = pd.DatetimeIndex(df["datetime"])
        trading_days, calendar = _build_trading_days(market, df)

        window = prepare_inference_window(
            df, market, timescale, Lc=Lc, Lh=Lh, stats=stats,
            soft_clip_c=float(cfg.data.soft_clip_c),
            half_life_days=half_life_days,
            sector_id=sector_id, inst_id=inst_id, trading_days=trading_days,
        )

        _progress("sampling")
        gen = torch.Generator().manual_seed(seed)
        paths = flow_sample(
            model, window["x_ctx"], window["cond"], Lh=Lh,
            steps=steps, solver="euler", n_paths=n_paths,
            generator=gen, device=torch.device("cpu"),
            precision="fp32", chunk_size=64,
        )
        feats = denormalize_paths(paths[0], market, timescale, stats)  # [K,Lh,5]
        ohlcv = paths_to_ohlcv(
            feats, window["prev_close"], window["prev_ema"], window["half_life_bars"],
        )
    except Exception as e:  # noqa: BLE001 — 推理链路统一 fail-closed
        _fail("inference_failed", f"{type(e).__name__}: {e}")

    result = {
        "ok": True,
        "market": market,
        "timescale": timescale,
        "anchor_ts": str(window["ctx_timestamps"][-1]),
        "prev_close": float(window["prev_close"]),
        "future_timestamps": [str(ts) for ts in window["future_timestamps"]],
        "ohlcv": {name: np.asarray(arr).tolist() for name, arr in ohlcv.items()},
        "instrument": {"symbol": symbol, "inst_id": inst_id, "sector_id": sector_id, "matched": matched},
        "meta": {
            "model_step": 0 if smoke else None,
            "Lc": int(Lc), "Lh": int(Lh),
            "n_paths": n_paths, "steps": steps, "seed": seed,
            "precision": "fp32", "device": "cpu",
            "elapsed_s": round(time.monotonic() - t0, 1),
            "calendar": calendar,
            "smoke": smoke,
        },
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
