"""美股 (us) API — 白名单配置、数据同步与状态。

美股是独立市场: 数据走美股专用数据源 (默认 eastmoney_us 插件),
本路由只做配置/触发/查询, 数据消费统一走现有 kline/screener API
(带 asset_type=us)。手动同步经任务库跟踪 (label=us_sync), 进度可查
GET /api/pipeline/jobs?label=us_sync。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.jobs.daily_pipeline import start_market_sync
from app.services import us_sync, us_universe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/us", tags=["us"])


class UniverseIn(BaseModel):
    us: list[str] = Field(default_factory=list, description="美股代码, 如 AAPL")


class SyncIn(BaseModel):
    with_minute: bool = Field(False, description="同时同步近 3 天 1 分钟K")


@router.get("/universe")
def get_universe(request: Request):
    """读取美股 K 线同步白名单 (缺省自动生成权重股默认清单)。"""
    return {"us": us_universe.load_universe(request.app.state.repo.store.data_dir)}


@router.put("/universe")
def put_universe(request: Request, body: UniverseIn):
    """更新白名单。立即生效, 下次同步按新清单拉取。"""
    if not body.us:
        raise HTTPException(status_code=422, detail="美股白名单不能为空")
    cleaned = us_universe.save_universe(request.app.state.repo.store.data_dir, body.us)
    return {"us": cleaned}


@router.post("/sync")
def post_sync(request: Request, body: SyncIn | None = None):
    """后台触发一次美股同步 (instruments 全市场 → 白名单日K → enriched → 可选分钟K)。

    立即返回 job_id; 进度经 GET /api/us/status 或
    GET /api/pipeline/jobs?label=us_sync 查询。任务库为全局单飞:
    已有任意数据任务在运行 (含其他市场/A股) 时返回 409。
    """
    with_minute = bool(body.with_minute) if body else False
    job_id = start_market_sync(request.app.state.repo, "us", with_minute=with_minute)
    if job_id is None:
        raise HTTPException(status_code=409,
                            detail="已有数据任务在运行（可能是其他市场同步或 A股 任务）")
    return {"started": True, "job_id": job_id, "with_minute": with_minute}


@router.get("/status")
def get_status():
    """最近一次同步的状态摘要 (从未同步过时为 null)。"""
    return us_sync.last_status()
