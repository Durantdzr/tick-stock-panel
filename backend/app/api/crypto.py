"""加密货币 (crypto) API — 白名单/概念配置、数据同步与状态。

crypto 是独立市场: 数据走 crypto 专用数据源 (默认 binance 插件),
本路由只做配置/触发/查询, 数据消费统一走现有 kline/screener API
(带 asset_type=crypto)。手动同步经任务库跟踪 (label=crypto_sync),
进度可查 GET /api/pipeline/jobs?label=crypto_sync。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.jobs.daily_pipeline import start_market_sync
from app.services import crypto_concepts, crypto_sync, crypto_universe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crypto", tags=["crypto"])


class UniverseIn(BaseModel):
    spot: list[str] = Field(default_factory=list, description="现货交易对, 如 BTCUSDT")
    futures: list[str] = Field(default_factory=list, description="合约交易对, 如 BTCUSDT (内部自动加 .P)")


class SyncIn(BaseModel):
    with_minute: bool = Field(False, description="同时同步近 3 天 1 分钟K")


@router.get("/universe")
def get_universe(request: Request):
    """读取 crypto 标的白名单 (缺省自动生成主流币默认清单)。"""
    return crypto_universe.load_universe(request.app.state.repo.store.data_dir)


@router.put("/universe")
def put_universe(request: Request, body: UniverseIn):
    """更新白名单。立即生效, 下次同步按新清单拉取。"""
    if not body.spot and not body.futures:
        raise HTTPException(status_code=422, detail="现货与合约白名单不能同时为空")
    cleaned = crypto_universe.save_universe(
        request.app.state.repo.store.data_dir,
        {"spot": body.spot, "futures": body.futures},
    )
    return cleaned


class ConceptsIn(BaseModel):
    concepts: dict[str, list[str]] = Field(default_factory=dict, description='概念 → 成员 (如 "DeFi": ["ETHUSDT"])')


@router.get("/concepts")
def get_concepts(request: Request):
    """读取 crypto 概念板块映射 (DeFi/MEME/L1 等, 缺省自动生成)。"""
    return crypto_concepts.load_concepts(request.app.state.repo.store.data_dir)


@router.put("/concepts")
def put_concepts(request: Request, body: ConceptsIn):
    """更新概念映射。立即生效, 看板概念排名按新映射聚合。"""
    if not body.concepts:
        raise HTTPException(status_code=422, detail="概念映射不能为空")
    cleaned = crypto_concepts.save_concepts(request.app.state.repo.store.data_dir, body.concepts)
    return cleaned


@router.post("/sync")
def post_sync(request: Request, body: SyncIn | None = None):
    """后台触发一次 crypto 全量同步 (instruments → 日K → enriched → 可选分钟K)。

    立即返回 job_id; 进度经 GET /api/crypto/status 或
    GET /api/pipeline/jobs?label=crypto_sync 查询。任务库为全局单飞:
    已有任意数据任务在运行 (含其他市场/A股) 时返回 409。
    """
    with_minute = bool(body.with_minute) if body else False
    job_id = start_market_sync(request.app.state.repo, "crypto", with_minute=with_minute)
    if job_id is None:
        raise HTTPException(status_code=409,
                            detail="已有数据任务在运行（可能是其他市场同步或 A股 任务）")
    return {"started": True, "job_id": job_id, "with_minute": with_minute}


@router.get("/status")
def get_status():
    """最近一次同步的状态摘要 (从未同步过时为 null)。"""
    return crypto_sync.last_status()
