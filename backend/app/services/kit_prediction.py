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
    paths: int = 16,
    steps: int = 32,
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
