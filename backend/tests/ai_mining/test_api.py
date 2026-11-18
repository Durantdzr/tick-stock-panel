"""ai-mining API 契约测试: 启动/校验/防重入/取消/结果（manager 用真实 store 的替身）。"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.ai_mining import router
from app.services.mining_jobs import (
    TERMINAL_RUN_STATUSES,
    MiningRunStore,
)


class _Repo:
    def __init__(self, data_dir: Path) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)


class _Manager:
    """不 spawn 线程的编排替身: store 走真实实现, start/cancel 只记账。"""

    def __init__(self, data_dir: Path) -> None:
        self.store = MiningRunStore(data_dir, namespace="llm_evo")
        self.starts: list[dict] = []

    def start(self, request, data_fingerprint, force=False, source="manual", run_id=None):
        self.starts.append({"request": request, "data_fingerprint": data_fingerprint, "source": source})
        return self.store.create(request, data_fingerprint, run_id=run_id)

    def cancel(self, run_id: str):
        manifest = self.store.get(run_id)
        if manifest is None:
            raise KeyError(run_id)
        if manifest["status"] in TERMINAL_RUN_STATUSES:
            return manifest
        self.store.append_event(run_id, "cancelled", {"status": "cancelled"})
        self.store.transition_status(run_id, "cancelled")
        return self.store.get(run_id)


@pytest.fixture()
def client(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)
    app.state.ai_mining_manager = _Manager(tmp_path)
    return TestClient(app), tmp_path


def _payload() -> dict:
    end = date.today() - timedelta(days=5)
    start = end - timedelta(days=90)
    return {"eval_start": start.isoformat(), "eval_end": end.isoformat(), "rounds": 2, "max_llm_calls": 20}


def test_start_creates_run(client):
    http, _ = client
    body = http.post("/api/ai-mining/runs", json=_payload())
    assert body.status_code == 200
    data = body.json()
    assert data["status"] == "queued"
    assert data["request"]["rounds"] == 2


def test_start_validates_window(client):
    http, _ = client
    payload = _payload()
    payload["eval_start"], payload["eval_end"] = payload["eval_end"], payload["eval_start"]
    response = http.post("/api/ai-mining/runs", json=payload)
    assert response.status_code == 400
    assert "eval_start" in response.json()["detail"]

    payload = _payload()
    payload["eval_end"] = "2099-01-01"
    response = http.post("/api/ai-mining/runs", json=payload)
    assert response.status_code == 400
    assert "不能晚于今天" in response.json()["detail"]


def test_start_rejects_extra_fields(client):
    http, _ = client
    payload = {**_payload(), "weapon": "extra"}
    assert http.post("/api/ai-mining/runs", json=payload).status_code == 422


def test_start_conflicts_with_active_run(client):
    http, tmp_path = client
    first = http.post("/api/ai-mining/runs", json=_payload())
    assert first.status_code == 200
    run_id = first.json()["run_id"]
    store = MiningRunStore(tmp_path, namespace="llm_evo")
    store.transition_status(run_id, "running")
    second = http.post("/api/ai-mining/runs", json=_payload())
    assert second.status_code == 409
    assert "进行中" in second.json()["detail"]


def test_get_run_and_list(client):
    http, _ = client
    created = http.post("/api/ai-mining/runs", json=_payload()).json()
    got = http.get(f"/api/ai-mining/runs/{created['run_id']}")
    assert got.status_code == 200
    assert got.json()["run_id"] == created["run_id"]
    listed = http.get("/api/ai-mining/runs")
    assert listed.status_code == 200
    assert any(item["run_id"] == created["run_id"] for item in listed.json()["items"])


def test_get_unknown_run_404(client):
    http, _ = client
    assert http.get("/api/ai-mining/runs/ghost").status_code == 404
    assert http.get("/api/ai-mining/runs/ghost/events").status_code == 404


def test_cancel_transitions(client):
    http, tmp_path = client
    created = http.post("/api/ai-mining/runs", json=_payload()).json()
    run_id = created["run_id"]
    store = MiningRunStore(tmp_path, namespace="llm_evo")
    store.transition_status(run_id, "running")
    cancelled = http.post(f"/api/ai-mining/runs/{run_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert http.post("/api/ai-mining/runs/ghost/cancel").status_code == 404


def test_result_gating(client):
    http, tmp_path = client
    created = http.post("/api/ai-mining/runs", json=_payload()).json()
    run_id = created["run_id"]
    store = MiningRunStore(tmp_path, namespace="llm_evo")
    store.transition_status(run_id, "running")
    # 终态前的 running: 409
    assert http.get(f"/api/ai-mining/runs/{run_id}/result").status_code == 409
    store.write_summary(run_id, {"result": {"rounds": [{"round": 1, "decision": "accept"}]}})
    store.transition_status(run_id, "succeeded")
    body = http.get(f"/api/ai-mining/runs/{run_id}/result")
    assert body.status_code == 200
    assert body.json()["summary"]["rounds"][0]["decision"] == "accept"
    # 失败态: 422
    other = http.post("/api/ai-mining/runs", json=_payload()).json()
    other_id = other["run_id"]
    store.transition_status(other_id, "failed")
    assert http.get(f"/api/ai-mining/runs/{other_id}/result").status_code == 422


def test_manager_missing_503(tmp_path: Path):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = _Repo(tmp_path)  # 不挂 ai_mining_manager
    http = TestClient(app)
    assert http.get("/api/ai-mining/runs").status_code == 503
    assert http.post("/api/ai-mining/runs", json=_payload()).status_code == 503
