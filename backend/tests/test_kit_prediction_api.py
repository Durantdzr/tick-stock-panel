"""KiT 预测 API 测试 — /api/kit/predict 的校验错误与流式成功路径。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.custom.kit_prediction as kit_api
from app.services.kit_prediction import KitPredictLock


def make_daily(n: int = 320) -> pl.DataFrame:
    start = date(2025, 1, 1)
    dates = [date.fromordinal(start.toordinal() + i) for i in range(n)]
    return pl.DataFrame({
        "date": dates,
        "open": [10.0 + i * 0.01 for i in range(n)],
        "high": [10.5 + i * 0.01 for i in range(n)],
        "low": [9.5 + i * 0.01 for i in range(n)],
        "close": [10.2 + i * 0.01 for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    })


@pytest.fixture()
def client(tmp_path: Path):
    app = FastAPI()
    app.include_router(kit_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "index" if s.startswith("000300") else "stock",
        get_daily_asset=lambda at, s, start, end, columns=None: make_daily(),
    )
    return TestClient(app), tmp_path


def test_predict_empty_symbol_400(client):
    c, _ = client
    r = c.post("/api/kit/predict", json={"symbol": ""})
    assert r.status_code == 400


def test_predict_unsupported_market_returns_400(client):
    c, _ = client
    r = c.post("/api/kit/predict", json={"symbol": "000300.SH"})
    assert r.status_code == 400
    assert "不支持" in r.json()["detail"]


def test_predict_env_missing_returns_503(client):
    c, _ = client  # data_dir 为空 tmp → 环境缺失
    r = c.post("/api/kit/predict", json={"symbol": "600519.SH"})
    assert r.status_code == 503
    assert "setup_kit" in r.json()["detail"]


def test_predict_busy_returns_409(client, monkeypatch):
    c, tmp = client
    monkeypatch.setattr(kit_api, "check_kit_env",
                        lambda d: SimpleNamespace(available=True, missing=[], kit_root=tmp, python=None, ckpt=None, stats=None))
    lock = KitPredictLock(tmp)
    lock.acquire()
    try:
        r = c.post("/api/kit/predict", json={"symbol": "600519.SH"})
        assert r.status_code == 409
    finally:
        lock.release()


def test_predict_happy_path_streams_done(client, monkeypatch, tmp_path):
    c, tmp = client
    monkeypatch.setattr(kit_api, "check_kit_env",
                        lambda d: SimpleNamespace(available=True, missing=[], kit_root=tmp, python=None, ckpt=None, stats=None))

    async def fake_stream(repo, data_dir, symbol, **kwargs):
        yield json.dumps({"type": "stage", "stage": "env"})
        yield json.dumps({"type": "done", "prediction": {"symbol": symbol, "summary": {}}})

    monkeypatch.setattr(kit_api, "predict_stream", fake_stream)
    r = c.post("/api/kit/predict", json={"symbol": "600519.SH"})
    assert r.status_code == 200
    assert "application/x-ndjson" in r.headers["content-type"]
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["stage", "done"]
    assert events[-1]["prediction"]["symbol"] == "600519.SH"
