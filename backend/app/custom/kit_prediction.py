"""KiT 预测 API — 后端扩展注册 (POST /api/kit/predict, NDJSON 流式)。

流前校验 fail-closed: symbol 空 400 / 资产类型不支持 400 / 环境缺失 503 / 忙 409;
推理期错误以 NDJSON error 事件回传 (见 app/services/kit_prediction.predict_stream)。
扩展加载器自动发现 app.custom 下模块, 无需注册他处。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services.kit_prediction import (
    KitPredictLock,
    check_kit_env,
    predict_stream,
    resolve_kit_market,
)
from app.services.ndjson_heartbeat import with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kit", tags=["kit-prediction"])

EXTENSION_ID = "kit.prediction"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


class KitPredictRequest(BaseModel):
    symbol: str


@router.post("/predict")
async def predict(request: Request, req: KitPredictRequest):
    """单票 KiT 扩散模型日线预测 — NDJSON 流式返回。"""
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    repo = request.app.state.repo
    data_dir = repo.store.data_dir

    asset_type = repo.resolve_asset_type(req.symbol)
    if resolve_kit_market(asset_type) is None:
        raise HTTPException(400, f"该资产类型({asset_type})不支持 KiT 预测 (仅支持 A股/美股/加密货币)")

    env = check_kit_env(data_dir)
    if not env.available:
        raise HTTPException(503, "; ".join(env.missing))

    # 单飞探测: 同时刻仅允许 1 个预测任务 (真正持锁在 predict_stream 内)
    probe = KitPredictLock(data_dir)
    try:
        probe.acquire()
    except Exception as e:
        logger.info("kit predict busy: %s", e)
        raise HTTPException(409, "另一个 KiT 预测任务正在运行, 请稍后再试") from e
    probe.release()

    async def stream_gen():
        async for chunk in with_heartbeat(predict_stream(repo, data_dir, req.symbol)):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
