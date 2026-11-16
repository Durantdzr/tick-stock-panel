"""KiT 扩散模型单票日线预测 — 服务层。

取数(repo/enriched 前复权日线) → 市场映射 / partial bar 丢弃 / payload 构建 →
子进程调用 KiT venv 的 runner(backend/scripts/kit_predict_runner.py) →
路径汇总(median/p10/p90) → NDJSON 流式事件。

KiT 代码(AGPL-3.0)只存在于 <data_dir>/models/kit/, 不进本仓库; torch 等大依赖
只装在 KiT venv, 主 venv 保持轻量。环境未配置时 fail-closed(KitEnvError),
不影响个股分析其他功能。

数据契约见 docs/superpowers/plans/2026-11-17-kit-prediction.md Task 1/3;
设计见 docs/superpowers/specs/2026-11-17-kit-prediction-design.md。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.market_time import cn_now, cn_today, us_now

CN_TZ = "Asia/Shanghai"

# 注入 KiT 的日线根数: Lc=250 + volume EMA 预热余量 (KiT 上游 keep 公式 ≈ Lc + 12*half_life*bars_per_day + 8)
_KIT_DAILY_BARS = 320
# 可用完整日线低于此数 fail-closed (KiT 1d 训练窗口 Lc=250)
_KIT_MIN_BARS = 250
# 锚点 = 最近一根已收盘日线; 预测 20 根
_KIT_PRED_LEN = 20

# checkpoint 训练数据截止 (step_market_284M 三市场 bundle)
KIT_MODEL_CUTOFF = "2025-12-31"

# 本项目 asset_type → KiT market
KIT_MARKET_BY_ASSET = {"stock": "ashare", "us": "us", "crypto": "crypto"}


class KitEnvError(RuntimeError):
    """KiT 环境未配置/不可用 (fail-closed, 提示用户跑 setup_kit.sh)。"""


class KitBusyError(RuntimeError):
    """已有预测任务在跑 (单飞锁)。"""


def resolve_kit_market(asset_type: str) -> str | None:
    """asset_type → KiT market 名; index/etf 等不支持返回 None (fail-closed)。"""
    return KIT_MARKET_BY_ASSET.get(asset_type)


# ================================================================
# 环境检查
# ================================================================

@dataclass
class KitEnv:
    """KiT 运行环境探测结果; available=False 时 missing 列出缺失项。"""
    kit_root: Path | None = None
    python: Path | None = None
    ckpt: Path | None = None
    stats: Path | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return not self.missing


def _venv_python(kit_root: Path) -> Path | None:
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        p = kit_root / rel
        if p.exists():
            return p
    return None


def check_kit_env(data_dir: Path) -> KitEnv:
    """探测 <data_dir>/models/kit 下的 KiT 环境 (venv python + step_market_284M bundle)。"""
    kit_root = Path(data_dir) / "models" / "kit"
    env = KitEnv(kit_root=kit_root)
    if not kit_root.is_dir():
        env.missing.append(f"KiT 未安装: {kit_root} (运行 backend/scripts/setup_kit.sh)")
        return env
    env.python = _venv_python(kit_root)
    if env.python is None:
        env.missing.append(f"KiT venv 缺失: {kit_root}/.venv (重跑 backend/scripts/setup_kit.sh)")
    bundle = kit_root / "ckpt" / "step_market_284M"
    env.ckpt = bundle / "step_market_284M.pt"
    env.stats = bundle / "stats.json"
    if not env.ckpt.is_file():
        env.missing.append(f"checkpoint 缺失: {env.ckpt} (重跑 backend/scripts/setup_kit.sh)")
    if not env.stats.is_file():
        env.missing.append(f"stats.json 缺失: {env.stats} (重跑 backend/scripts/setup_kit.sh)")
    return env


# ================================================================
# 锚点与取数
# ================================================================

def drop_partial_last_bar(df: pl.DataFrame, asset_type: str, now: datetime | None = None) -> pl.DataFrame:
    """盘中预测试丢弃最后一根未收盘的当日 bar (KiT 日线语义为收盘 bar, 喂 partial 是分布偏移)。

    - stock(ashare): 当日北京时间 15:00 前视为 partial
    - crypto: 24/7, 当日北京日历日(午夜收盘)一律视为 partial
    - us: 当日美东 16:10 前视为 partial
    now 仅供测试注入: naive 视为北京时间; aware 按自身时区参与对应市场判定,
    另一市场口径取当前真实时间。最后一天不是对应市场的"今天"时一律不丢。
    """
    if df.is_empty():
        return df
    last_date = df["date"][-1]
    if now is None:
        cn_dt, us_dt = cn_now(), us_now()
    elif now.tzinfo is None:
        cn_dt, us_dt = now, us_now()
    else:
        cn_dt, us_dt = now.astimezone(ZoneInfo(CN_TZ)), now
    drop = False
    if asset_type == "crypto":
        drop = last_date == cn_dt.date()
    elif asset_type == "us":
        drop = last_date == us_dt.date() and us_dt.time() < time(16, 10)
    else:  # stock(ashare)
        drop = last_date == cn_dt.date() and cn_dt.time() < time(15, 0)
    return df[:-1] if drop else df


def load_kit_daily_frame(repo, symbol: str) -> tuple[pl.DataFrame, str]:
    """取该标的已收盘 enriched 日线 (复权 OHLCV) 并判定市场; 不足 250 根抛 ValueError。"""
    asset_type = repo.resolve_asset_type(symbol)
    market = resolve_kit_market(asset_type)
    if market is None:
        raise ValueError(f"该资产类型({asset_type})不支持 KiT 预测")
    df = drop_partial_last_bar(
        repo.get_daily_asset(asset_type, symbol, cn_today() - timedelta(days=_KIT_DAILY_BARS * 2), cn_today()),
        asset_type,
    )
    if len(df) < _KIT_MIN_BARS:
        raise ValueError(f"日线数据不足: 仅 {len(df)} 根, KiT 预测需要至少 {_KIT_MIN_BARS} 根已收盘日线")
    return df, asset_type


# ================================================================
# runner payload
# ================================================================

# KiT 日线收盘时刻 (runner 按市场日历特征解析本地墙钟)
_CLOSE_STAMP = {"ashare": " 15:00:00", "us": " 16:00:00", "crypto": " 23:59:00"}


def build_runner_payload(
    symbol: str,
    market: str,
    df: pl.DataFrame,
    *,
    kit_root: Path,
    ckpt: Path,
    paths: int = 8,
    steps: int = 16,
    seed: int = 0,
) -> dict:
    """enriched 日线 → runner stdin JSON (契约见计划 Task 1)。不足 250 根抛 ValueError。"""
    if len(df) < _KIT_MIN_BARS:
        raise ValueError(f"日线数据不足: 仅 {len(df)} 根, KiT 预测需要至少 {_KIT_MIN_BARS} 根已收盘日线")
    stamp = _CLOSE_STAMP[market]
    bars = [
        {
            "datetime": f"{row['date'].isoformat()}{stamp}",
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for row in df.tail(_KIT_DAILY_BARS).iter_rows(named=True)
    ]
    return {
        "kit_root": str(kit_root),
        "ckpt": str(ckpt),
        "symbol": symbol,
        "market": market,
        "timescale": "1d",
        "bars": bars,
        "pred_len": _KIT_PRED_LEN,
        "paths": paths,
        "steps": steps,
        "seed": seed,
    }


# ================================================================
# 单飞锁: 同一 data_dir 同时只允许 1 个预测任务 (CPU 推理约 4 分钟)
# ================================================================

class KitPredictLock:
    """flock 文件锁 (<data_dir>/.kit_predict.lock), 跨进程互斥; 精简自 mining_process_lock。"""

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / ".kit_predict.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        import os

        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT)
        try:
            self._flock(fd)
        except OSError as e:
            os.close(fd)
            raise KitBusyError("另一个 KiT 预测任务正在运行, 请稍后再试") from e
        self._fd = fd

    def release(self) -> None:
        import os

        if self._fd is None:
            return
        try:
            self._funlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    @staticmethod
    def _flock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _funlock(fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


# ================================================================
# 路径汇总 (median + p10/p90)
# ================================================================

def summarize_prediction(result: dict, symbol: str, asset_type: str, market: str) -> dict:
    """runner stdout → 前端 prediction 结构 (契约见计划 Task 3)。

    比例字段为小数制 (0.05 = 5%); 价格 round 6 位、比例 round 4 位。
    """
    import numpy as np

    ohlcv = {k: np.asarray(v, dtype=np.float64) for k, v in result["ohlcv"].items()}
    close = ohlcv["close"]  # [K, Lh]
    prev_close = float(result["prev_close"])
    rel = close / prev_close - 1.0

    summary = {
        "median_return": round(float(np.median(rel[:, -1])), 4),
        "p10_return": round(float(np.quantile(rel[:, -1], 0.10)), 4),
        "p90_return": round(float(np.quantile(rel[:, -1], 0.90)), 4),
        "median_max_gain": round(float(np.median(rel.max(axis=1))), 4),
        "median_max_drawdown": round(float(np.median(rel.min(axis=1))), 4),
    }
    meta = result.get("meta", {})
    bars = []
    for t, ts in enumerate(result["future_timestamps"]):
        bars.append({
            "date": str(ts)[:10],
            "open": round(float(np.median(ohlcv["open"][:, t])), 6),
            "high": round(float(np.median(ohlcv["high"][:, t])), 6),
            "low": round(float(np.median(ohlcv["low"][:, t])), 6),
            "close": round(float(np.median(close[:, t])), 6),
            "close_p10": round(float(np.quantile(close[:, t], 0.10)), 6),
            "close_p90": round(float(np.quantile(close[:, t], 0.90)), 6),
        })
    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "market": market,
        "anchor_date": str(result["anchor_ts"])[:10],
        "prev_close": round(prev_close, 6),
        "bars": bars,
        "summary": summary,
        "model": {
            "cutoff": KIT_MODEL_CUTOFF,
            "n_paths": meta.get("n_paths"),
            "steps": meta.get("steps"),
            "elapsed_s": meta.get("elapsed_s"),
            "calendar": meta.get("calendar"),
            "instrument_matched": bool(result.get("instrument", {}).get("matched")),
        },
    }


# ================================================================
# 子进程编排 + NDJSON 流
# ================================================================

def _runner_path() -> Path:
    # app/services/kit_prediction.py → parents[2] = backend/
    return Path(__file__).resolve().parents[2] / "scripts" / "kit_predict_runner.py"


# runner stderr PROGRESS token → 前端 stage
_STAGE_BY_PROGRESS = {"loading_model": "model", "sampling": "sampling"}


async def predict_stream(
    repo,
    data_dir: Path,
    symbol: str,
    *,
    paths: int = 8,
    steps: int = 16,
    seed: int = 0,
    timeout_s: float = 900.0,
) -> AsyncIterator[str]:
    """单票 KiT 预测, 逐行 yield NDJSON 事件 (不含 \\n)。

    事件: {"type":"stage","stage":...} → {"type":"done","prediction":{...}}
          或 {"type":"error","code":...,"message":...}
    校验失败: 环境缺失抛 KitEnvError (API 层转 503); 市场/历史不足以 error 事件回传。
    """
    env = check_kit_env(data_dir)
    if not env.available:
        raise KitEnvError("; ".join(env.missing))
    yield json.dumps({"type": "stage", "stage": "env"})

    try:
        df, asset_type = load_kit_daily_frame(repo, symbol)
    except ValueError as e:
        code = "unsupported_market" if "不支持" in str(e) else "insufficient_history"
        yield json.dumps({"type": "error", "code": code, "message": str(e)}, ensure_ascii=False)
        return
    market = KIT_MARKET_BY_ASSET[asset_type]
    anchor = df["date"][-1]
    yield json.dumps({
        "type": "stage", "stage": "data",
        "detail": f"{len(df)} bars · 锚点 {anchor}",
    }, ensure_ascii=False)

    payload = build_runner_payload(symbol, market, df, kit_root=env.kit_root, ckpt=env.ckpt,
                                   paths=paths, steps=steps, seed=seed)
    runner = _runner_path()
    if not runner.is_file():
        yield json.dumps({"type": "error", "code": "env_missing", "message": f"runner 缺失: {runner}"})
        return

    lock = KitPredictLock(data_dir)
    try:
        lock.acquire()
    except KitBusyError as e:
        yield json.dumps({"type": "error", "code": "busy", "message": str(e)}, ensure_ascii=False)
        return


    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            str(env.python), str(runner),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode())
        await proc.stdin.drain()
        proc.stdin.close()

        stdout_task = asyncio.create_task(proc.stdout.read())
        err_lines: list[str] = []
        forwarded = 0

        async def _pump_err() -> None:
            async for raw in proc.stderr:
                err_lines.append(raw.decode(errors="replace").strip())

        pump_task = asyncio.create_task(_pump_err())
        deadline = asyncio.get_running_loop().time() + timeout_s
        timed_out = False
        try:
            while not stdout_task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    timed_out = True
                    break
                await asyncio.sleep(0.2)
                for token in err_lines[forwarded:]:
                    forwarded += 1
                    if not token.startswith("PROGRESS"):
                        continue
                    stage = _STAGE_BY_PROGRESS.get(token[len("PROGRESS"):].strip())
                    if stage:
                        yield json.dumps({"type": "stage", "stage": stage})
            if timed_out:
                proc.kill()
                await proc.wait()
                yield json.dumps({
                    "type": "error", "code": "timeout",
                    "message": f"推理超时 (> {int(timeout_s)}s), 已终止",
                })
                return
            stdout = await stdout_task
            await proc.wait()
            # 排干剩余 stderr, 转发最后的 PROGRESS (sampling 结束等)
            for token in err_lines[forwarded:]:
                forwarded += 1
                if token.startswith("PROGRESS"):
                    stage = _STAGE_BY_PROGRESS.get(token[len("PROGRESS"):].strip())
                    if stage:
                        yield json.dumps({"type": "stage", "stage": stage})
        finally:
            pump_task.cancel()

        if proc.returncode != 0:
            tail = [x for x in err_lines if not x.startswith("PROGRESS")][-2:]
            detail = "; ".join(tail) or "无 stderr 输出"
            yield json.dumps({
                "type": "error", "code": "inference_failed",
                "message": f"KiT 推理失败 (exit {proc.returncode}): {detail}",
            }, ensure_ascii=False)
            return

        # stdout 最后一行是结果 JSON (前面可能有 load_model_and_config 的 [info]/[warn] 行)
        result = None
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and "ok" in candidate:
                    result = candidate
        if result is None or not result.get("ok"):
            msg = (result or {}).get("error", "runner 无结果输出")
            yield json.dumps({"type": "error", "code": "inference_failed", "message": msg},
                             ensure_ascii=False)
            return

        prediction = summarize_prediction(result, symbol, asset_type, market)
        yield json.dumps({"type": "done", "prediction": prediction}, ensure_ascii=False)
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
        lock.release()
