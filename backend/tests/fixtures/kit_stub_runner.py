"""KiT runner 契约 stub — 读 stdin payload, 按 Task 1 stdout 契约输出确定性结果。

供 predict_stream 子进程编排测试使用 (真实 runner 依赖 torch, 在主 venv 不可用)。
"""
import json
import sys

payload = json.load(sys.stdin)
print("PROGRESS loading_model", file=sys.stderr, flush=True)
print("PROGRESS sampling", file=sys.stderr, flush=True)

K = int(payload.get("paths", 2))
LH = 2
prev = float(payload["bars"][-1]["close"])
ohlcv = {
    name: [[prev * (1 + 0.01 * (k + t)) for t in range(LH)] for k in range(K)]
    for name in ("open", "high", "low", "close", "volume")
}
result = {
    "ok": True,
    "market": payload["market"],
    "timescale": "1d",
    "anchor_ts": payload["bars"][-1]["datetime"],
    "prev_close": prev,
    "future_timestamps": [f"2026-11-{10 + t} 15:00:00" for t in range(LH)],
    "ohlcv": ohlcv,
    "instrument": {"symbol": payload.get("symbol", ""), "inst_id": 0, "sector_id": 0, "matched": False},
    "meta": {"model_step": 50000, "Lc": 250, "Lh": 20, "n_paths": K, "steps": 32,
             "seed": 0, "precision": "fp32", "device": "cpu", "elapsed_s": 1.0,
             "calendar": "xshg", "smoke": False},
}
print(json.dumps(result, ensure_ascii=False))
