"""us_data_provider 偏好出入口 API 测试。

harness: 最小 FastAPI 挂 settings 真路由 (无网关中间件), preferences
load/save 打成内存仓, custom.names 打成白名单 — 生产环境由 loader 注册。
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    from app.api import settings as settings_api
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    store: dict = {}
    monkeypatch.setattr(preferences, "load", lambda: store)
    monkeypatch.setattr(preferences, "save", lambda updates: store.update(updates))
    monkeypatch.setattr(custom_sources, "names", lambda: {"yahoo_us", "eastmoney_us"})

    app = FastAPI()
    app.include_router(settings_api.router)
    return TestClient(app)


def test_put_and_get_us_data_provider(client: TestClient) -> None:
    resp = client.put("/api/settings/preferences/data-providers",
                      json={"us_data_provider": "yahoo_us"})
    assert resp.status_code == 200
    assert resp.json()["us_data_provider"] == "yahoo_us"
    assert client.get("/api/settings/preferences").json()["us_data_provider"] == "yahoo_us"


def test_put_invalid_us_provider_falls_back(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    resp = client.put("/api/settings/preferences/data-providers",
                      json={"us_data_provider": "not_a_source"})
    assert resp.status_code == 200
    assert resp.json()["us_data_provider"] == "eastmoney_us", "非法值经 getter 白名单回退"
    with caplog.at_level(logging.WARNING, logger="app.services.preferences"):
        client.put("/api/settings/preferences/data-providers",
                   json={"us_data_provider": "not_a_source"})
    assert any("us_data_provider" in r.message for r in caplog.records), "回退必须留痕"


def test_put_without_field_keeps_existing(client: TestClient) -> None:
    client.put("/api/settings/preferences/data-providers",
               json={"us_data_provider": "yahoo_us"})
    resp = client.put("/api/settings/preferences/data-providers",
                      json={"daily_data_provider": "tickflow"})
    assert resp.status_code == 200
    assert resp.json()["us_data_provider"] == "yahoo_us", "未携带字段不得覆盖存量偏好"
