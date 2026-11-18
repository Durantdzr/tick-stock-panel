"""AI 因子进化 runs API: 启动/列表/详情/事件流(SSE)/取消/结果。

编排复用 MiningJobManager(kind="llm_evo")，run 记录持久化于
``data/research/llm_evo/runs/{run_id}/`` (manifest/summary/events/memory/因子产物)。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from app.ai_mining.runtime import run_dir_for
from app.services.mining_jobs import (
    ACTIVE_RUN_STATUSES,
    SUCCESS_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    MiningRunStore,
    MiningRunStoreError,
    MiningRunValidationError,
)
from app.services.mining_manager import MiningJobManager

router = APIRouter(prefix="/api/ai-mining", tags=["ai-mining"])

_SSE_POLL_SECONDS = 0.5
_SSE_HEARTBEAT_SECONDS = 15.0


class AiMiningStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_start: date
    eval_end: date
    asset_type: Literal["stock"] = "stock"
    rounds: int = Field(3, ge=1, le=10)
    factors_per_round: int = Field(2, ge=1, le=5)
    max_repairs: int = Field(3, ge=0, le=5)
    max_llm_calls: int = Field(120, ge=5, le=400)
    eval_days_hint: int = Field(90, ge=20, le=500)


def _manager(request: Request) -> MiningJobManager:
    manager = getattr(request.app.state, "ai_mining_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="AI 因子进化未启用（服务尚未就绪）")
    return manager


def _required_manifest(store: MiningRunStore, run_id: str) -> dict[str, Any]:
    try:
        manifest = store.get(run_id)
    except MiningRunValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiningRunStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to read ai-mining run") from exc
    if manifest is None:
        raise HTTPException(status_code=404, detail="ai-mining run not found")
    return manifest


def _project_run(store: MiningRunStore, manifest: Mapping[str, Any]) -> dict[str, Any]:
    run_id = str(manifest["run_id"])
    summary = store.read_summary(run_id)
    return {
        "run_id": run_id,
        "status": manifest["status"],
        "request": manifest.get("request") or {},
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "started_at": manifest.get("started_at"),
        "finished_at": manifest.get("finished_at"),
        "progress": summary.get("progress") if isinstance(summary.get("progress"), Mapping) else None,
        "error": manifest.get("error"),
        "summary": summary.get("result"),
    }


@router.get("/runs")
def list_runs(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    status: Annotated[list[str] | None, Query()] = None,
) -> dict[str, Any]:
    manager = _manager(request)
    statuses = None
    if status:
        unknown = sorted(set(status) - (ACTIVE_RUN_STATUSES | SUCCESS_RUN_STATUSES | TERMINAL_RUN_STATUSES))
        if unknown:
            raise HTTPException(status_code=400, detail=f"unsupported statuses: {unknown}")
        statuses = status
    try:
        manifests = manager.store.list_runs(limit=limit, statuses=statuses)
        return {"items": [_project_run(manager.store, item) for item in manifests]}
    except MiningRunValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiningRunStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to read ai-mining runs") from exc


@router.post("/runs")
def start_run(payload: AiMiningStartRequest, request: Request) -> dict[str, Any]:
    if payload.eval_start >= payload.eval_end:
        raise HTTPException(status_code=400, detail="eval_start 必须早于 eval_end")
    if payload.eval_end > date.today():
        raise HTTPException(status_code=400, detail="eval_end 不能晚于今天")
    manager = _manager(request)

    # 防重入: 同时只允许一个进化 run (LLM 花真金白银, 不允许无意识并发)
    active = manager.store.list_runs(statuses=ACTIVE_RUN_STATUSES)
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"已有进行中的进化 run: {active[0]['run_id']}，请先取消或等待完成",
        )

    worker_request = payload.model_dump(mode="json")
    try:
        manifest = manager.start(
            worker_request,
            data_fingerprint={"asset_type": payload.asset_type, "eval_window": [payload.eval_start.isoformat(), payload.eval_end.isoformat()]},
            source="manual",
        )
    except MiningRunValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MiningRunStoreError as exc:
        raise HTTPException(status_code=500, detail="failed to persist ai-mining run") from exc
    return _project_run(manager.store, manifest)


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict[str, Any]:
    store = _manager(request).store
    return _project_run(store, _required_manifest(store, run_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    try:
        return _project_run(manager.store, manager.cancel(run_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ai-mining run not found") from exc
    except MiningRunValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs/{run_id}/result")
def get_result(run_id: str, request: Request) -> dict[str, Any]:
    manager = _manager(request)
    store = manager.store
    manifest = _required_manifest(store, run_id)
    status = str(manifest["status"])
    if status not in SUCCESS_RUN_STATUSES:
        status_code = 409 if status not in TERMINAL_RUN_STATUSES else 422
        raise HTTPException(
            status_code=status_code,
            detail=f"ai-mining result is unavailable for status {status}",
        )
    summary = store.read_summary(run_id)
    data_dir = request.app.state.repo.store.data_dir
    run_dir = run_dir_for(data_dir, run_id)
    return {
        "run_id": run_id,
        "status": status,
        "summary": summary.get("result"),
        "artifacts": {
            "run_dir": str(run_dir),
            "memory": "memory.json",
            "panel": "panel.parquet",
        },
    }


@router.get("/runs/{run_id}/events")
def stream_events(
    run_id: str,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    store = _manager(request).store
    _required_manifest(store, run_id)
    cursor = _event_cursor(last_event_id)

    async def generate() -> AsyncIterator[dict[str, str]]:
        nonlocal cursor
        last_emit = asyncio.get_running_loop().time()
        terminal_sent = False
        while not await request.is_disconnected():
            events = await asyncio.to_thread(store.read_events, run_id, after_id=cursor)
            for event in events:
                cursor = int(event["id"])
                event_type = "failed" if event.get("type") == "error" else str(event["type"])
                payload = dict(event.get("payload") or {})
                if event_type in TERMINAL_RUN_STATUSES:
                    payload.setdefault("status", event_type)
                    terminal_sent = True
                yield {
                    "id": str(cursor),
                    "event": event_type,
                    "data": json.dumps(payload, ensure_ascii=False, allow_nan=False),
                }
                last_emit = asyncio.get_running_loop().time()
            manifest = await asyncio.to_thread(store.get, run_id)
            if manifest is None:
                return
            status = str(manifest["status"])
            if status in TERMINAL_RUN_STATUSES:
                if not terminal_sent:
                    event_type = "failed" if status == "failed" else status
                    yield {
                        "id": str(cursor),
                        "event": event_type,
                        "data": json.dumps(
                            {"status": status, "message": manifest.get("error")},
                            ensure_ascii=False,
                        ),
                    }
                return
            now = asyncio.get_running_loop().time()
            if now - last_emit >= _SSE_HEARTBEAT_SECONDS:
                yield {"event": "heartbeat", "data": "{}"}
                last_emit = now
            await asyncio.sleep(_SSE_POLL_SECONDS)

    return EventSourceResponse(generate(), ping=_SSE_HEARTBEAT_SECONDS)


def _event_cursor(value: str | None) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
