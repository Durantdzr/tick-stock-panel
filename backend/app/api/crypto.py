"""加密货币 (crypto) API — 白名单配置、数据同步与状态。

crypto 是独立市场: 数据走 crypto 专用数据源 (默认 binance 插件),
本路由只做配置/触发/查询, 数据消费统一走现有 kline/screener API
(带 asset_type=crypto)。
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.services import crypto_sync, crypto_universe

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


@router.post("/sync")
def post_sync(request: Request, body: SyncIn | None = None):
    """后台触发一次 crypto 全量同步 (instruments → 日K → enriched → 可选分钟K)。

    立即返回; 进度经 GET /api/crypto/status 查询。重复触发被同步锁拒绝。
    """
    if crypto_sync.is_running():
        raise HTTPException(status_code=409, detail="crypto 同步已在进行中")
    repo = request.app.state.repo
    with_minute = bool(body.with_minute) if body else False

    def _run():
        try:
            result = crypto_sync.run_crypto_sync(repo, with_minute=with_minute)
            logger.info("crypto 同步完成: %s", result.get("ok"))
        except Exception:
            logger.exception("crypto 同步线程异常")

    threading.Thread(target=_run, name="crypto-sync", daemon=True).start()
    return {"started": True, "with_minute": with_minute}


@router.get("/status")
def get_status():
    """最近一次同步的状态摘要 (从未同步过时为 null)。"""
    return crypto_sync.last_status()
