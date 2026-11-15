"""市场同步 (crypto/us) 任务库集成测试。

覆盖设计文档 Task 3 契约:
  - 手动同步经 job_store 跟踪 (label/trigger 标注), 忙时返回 None (API → 409);
  - run_*_sync 的 ok=False 翻译为 failed 记录 (不再伪装成功);
  - ok=True 但 errors 非空的部分失败 → succeeded 且错误摘要可见;
  - 定时路径 (trigger=daily) 同样走失败映射。
不依赖真实网络 (provider 层注入桩)。
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.services import us_universe
from app.services.pipeline_jobs import JobStore
from app.tickflow.repository import DataStore, KlineRepository

# ── 桩与工具 ─────────────────────────────────────────────────────────────

def _us_daily_df(symbol: str, days: list[date]) -> pl.DataFrame:
    n = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": days,
        "open": [100.0] * n,
        "high": [110.0] * n,
        "low": [95.0] * n,
        "close": [105.0] * n,
        "volume": [1_000_000.0] * n,
        "amount": [105_000_000.0] * n,
    })


class _FakeUSProvider:
    """最小美股 provider 桩 (与 test_us_market 同构); fail_symbols 支持部分失败。"""

    name = "fakeus"

    def __init__(self, fail_symbols: set[str] | None = None):
        self.fail_symbols = fail_symbols or set()

    def fetch_instruments_page(self, page: int) -> tuple[list[dict], int]:
        rows = pl.DataFrame({
            "symbol": ["AAPL", "MSFT"],
            "name": ["苹果", "微软"],
            "code": ["AAPL", "MSFT"],
            "exchange": ["NASDAQ"] * 2,
            "asset_type": ["us"] * 2,
            "source": ["fakeus"] * 2,
        }).to_dicts()
        return (rows, 2) if page == 1 else ([], 2)

    def get_instruments(self, asset_type="us"):
        return pl.DataFrame(self.fetch_instruments_page(1)[0])

    def get_daily(self, symbols, start_time, end_time, asset_type="us", on_chunk_done=None):
        end = date.today() - timedelta(days=1)
        days = [end - timedelta(days=69 - i) for i in range(70)]
        if start_time is not None:
            days = [d for d in days if d >= start_time.date()]
        frames = []
        for s in symbols:
            if s in self.fail_symbols:
                raise ProviderRequestError(f"{s} 网络失败")
            frames.append(_us_daily_df(s, days))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_minute(self, symbols, start_time, end_time, asset_type="us", freq="1m",
                   on_chunk_done=None):
        return pl.DataFrame()

    def close(self):
        pass


def _await_terminal(store: JobStore, jid: str, timeout_s: float = 10.0) -> dict:
    """后台线程轮询至终态 (手动同步是 daemon 线程, 测试里等待收尾)。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        j = store.get(jid)
        if j is not None and j["status"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {jid} 未在 {timeout_s}s 内到达终态: {store.get(jid)}")


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """隔离的 job_store: monkeypatch 模块单例, daily_pipeline 惰性导入取到的是它。"""
    s = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr("app.services.pipeline_jobs.job_store", s)
    return s


# ── 手动同步入任务库 ─────────────────────────────────────────────────────

def test_start_market_sync_creates_tracked_job(tmp_path, store, monkeypatch):
    """手动同步 → job 记录带 label=us_sync/trigger=manual, 成功含分步 result。"""
    from app.jobs.daily_pipeline import start_market_sync
    from app.services import us_sync

    us_universe.save_universe(tmp_path, ["AAPL"])
    repo = KlineRepository(DataStore(tmp_path))
    provider = _FakeUSProvider()
    monkeypatch.setattr(us_sync, "_us_provider", lambda: provider)

    jid = start_market_sync(repo, "us", with_minute=False)
    assert jid is not None
    j = _await_terminal(store, jid)
    assert j["status"] == "succeeded"
    assert j["label"] == "us_sync"
    assert j["trigger"] == "manual"
    assert "instruments" in (j["result"] or {})
    assert (j["result"] or {}).get("daily", {}).get("symbols") == 1


def test_start_market_sync_busy_returns_none(tmp_path, store):
    """任务库单飞被占 (任意市场/A股) → 返回 None, API 层映射 409。"""
    from app.jobs.daily_pipeline import start_market_sync

    store.create(timeout_s=600, label="crypto_sync", trigger="manual")  # 占住单飞
    repo = KlineRepository(DataStore(tmp_path))
    assert start_market_sync(repo, "us", with_minute=False) is None


def test_ok_false_maps_to_failed(tmp_path, store, monkeypatch):
    """run_*_sync 返回 ok=False → job 记 failed 且错误可见 (不再伪装成功)。"""
    from app.jobs.daily_pipeline import start_market_sync
    from app.services import us_sync

    monkeypatch.setattr(us_sync, "run_us_sync",
                        lambda *a, **k: {"ok": False, "error": "boom"})
    repo = KlineRepository(DataStore(tmp_path))
    jid = start_market_sync(repo, "us")
    j = _await_terminal(store, jid)
    assert j["status"] == "failed"
    assert "boom" in (j["error"] or "")


def test_partial_errors_succeeded_with_summary(tmp_path, store, monkeypatch):
    """ok=True 但 daily.errors 非空 → succeeded, 错误摘要留在 result 可展示。"""
    from app.jobs.daily_pipeline import start_market_sync
    from app.services import us_sync

    us_universe.save_universe(tmp_path, ["AAPL", "MSFT"])
    repo = KlineRepository(DataStore(tmp_path))
    provider = _FakeUSProvider(fail_symbols={"MSFT"})
    monkeypatch.setattr(us_sync, "_us_provider", lambda: provider)

    jid = start_market_sync(repo, "us")
    j = _await_terminal(store, jid)
    assert j["status"] == "succeeded"
    daily = (j["result"] or {}).get("daily") or {}
    assert daily.get("errors"), "部分失败摘要必须可见"
    assert any("MSFT" in e for e in daily["errors"])


def test_scheduled_daily_task_failure_mapping(tmp_path, store, monkeypatch):
    """定时路径 (trigger=daily): ok=False 同样映射为 failed 记录。"""
    from app.jobs import daily_pipeline
    from app.services import crypto_sync

    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(crypto_sync, "run_crypto_sync",
                        lambda *a, **k: {"ok": False, "error": "src down"})

    ok = daily_pipeline._run_tracked(
        lambda on_progress=None: daily_pipeline._run_market_sync(
            repo, "crypto", with_minute=False),
        "crypto_sync", trigger="daily", label="crypto_sync",
    )
    assert ok is False
    recent = store.list_recent(limit=5)
    assert recent[0]["status"] == "failed"
    assert recent[0]["trigger"] == "daily"
    assert "src down" in (recent[0]["error"] or "")


# ── API 契约 ─────────────────────────────────────────────────────────────

def test_us_sync_api_returns_409_when_busy(monkeypatch, tmp_path):
    """start_market_sync 忙 → POST /api/us/sync 409, 文案指明全局单飞语义。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import us as us_api

    monkeypatch.setattr(us_api, "start_market_sync", lambda *a, **k: None)

    app = FastAPI()
    app.state.repo = object()  # post_sync 仅透传给 start_market_sync (已打桩)
    app.include_router(us_api.router)
    client = TestClient(app)

    resp = client.post("/api/us/sync", json={"with_minute": False})
    assert resp.status_code == 409
    assert "已有数据任务" in resp.json()["detail"]


# ── 自动同步偏好 / 注册 / 门控 (Task 4) ──────────────────────────────────

class _StubScheduler:
    """APScheduler 桩: 记录 add_job/remove_job, 不真跑。"""

    def __init__(self):
        self.added: list[dict] = []
        self.removed: list[str] = []

    def add_job(self, fn, trigger=None, id=None, misfire_grace_time=None,
                replace_existing=None, **kw):
        self.added.append({"fn": fn, "trigger": trigger, "id": id,
                           "misfire_grace_time": misfire_grace_time,
                           "replace_existing": replace_existing})

    def remove_job(self, jid):
        self.removed.append(jid)


def _patch_prefs_storage(monkeypatch) -> dict:
    """隔离 preferences: load/save 都指向测试内 dict, 不触真实 preferences.json。"""
    from app.services import preferences

    storage: dict = {}

    def _fake_save(updates: dict) -> dict:
        storage.update(updates)
        return dict(storage)

    monkeypatch.setattr(preferences, "load", lambda: dict(storage))
    monkeypatch.setattr(preferences, "save", _fake_save)
    return storage


def test_market_auto_sync_prefs_roundtrip_and_validation(monkeypatch):
    from app.services import preferences

    _patch_prefs_storage(monkeypatch)
    preferences.set_market_auto_sync("crypto", True, 5)
    assert preferences.get_market_auto_sync("crypto") == {"enabled": True, "interval_minutes": 5}
    with pytest.raises(ValueError):
        preferences.set_market_auto_sync("crypto", True, 7)  # 非法档位
    with pytest.raises(ValueError):
        preferences.set_market_auto_sync("stock", True, 5)   # 市场自动同步仅 crypto/us
    assert preferences.get_market_auto_sync("us") == {"enabled": False, "interval_minutes": 15}


def test_market_auto_sync_corrupt_persisted_value_falls_back(monkeypatch):
    """存量非法 interval 回退 15, enabled 强转 bool (fail-closed)。"""
    from app.services import preferences

    monkeypatch.setattr(preferences, "load",
                        lambda: {"us_auto_sync": {"enabled": True, "interval_minutes": 999}})
    assert preferences.get_market_auto_sync("us") == {"enabled": True, "interval_minutes": 15}


def test_data_page_market_prefs(monkeypatch):
    from app.services import preferences

    _patch_prefs_storage(monkeypatch)
    assert preferences.get_data_page_market() == "stock"
    preferences.set_data_page_market("crypto")
    assert preferences.get_data_page_market() == "crypto"
    with pytest.raises(ValueError):
        preferences.set_data_page_market("etf")
    monkeypatch.setattr(preferences, "load", lambda: {"data_page_market": "hedge"})
    assert preferences.get_data_page_market() == "stock"  # 存量非法回退


def test_us_auto_sync_gate(monkeypatch):
    """us 自动同步 = XNYS 交易日 ∧ 美东常规时段; 两者缺一不可。"""
    from app import market_time
    from app.jobs import daily_pipeline
    from app.services import us_trading_day

    monkeypatch.setattr(us_trading_day, "is_us_trading_day", lambda d: True)
    monkeypatch.setattr(market_time, "us_in_session", lambda: False)
    assert daily_pipeline._us_auto_sync_allowed() is False
    monkeypatch.setattr(market_time, "us_in_session", lambda: True)
    assert daily_pipeline._us_auto_sync_allowed() is True
    monkeypatch.setattr(us_trading_day, "is_us_trading_day", lambda d: False)
    assert daily_pipeline._us_auto_sync_allowed() is False


def test_register_auto_jobs_respects_prefs(monkeypatch):
    """enabled → add_job(IntervalTrigger 档位, id 正确); disabled → remove_job。"""
    from app.jobs import daily_pipeline
    from app.services import preferences

    monkeypatch.setattr(preferences, "load", lambda: {
        "crypto_auto_sync": {"enabled": True, "interval_minutes": 15},
        "us_auto_sync": {"enabled": False, "interval_minutes": 15},
    })

    sched = _StubScheduler()
    daily_pipeline._register_crypto_auto_sync_job(sched, repo=None)
    assert len(sched.added) == 1
    assert sched.added[0]["id"] == "crypto_auto_sync"
    assert sched.added[0]["trigger"].interval == timedelta(minutes=15)
    assert sched.added[0]["replace_existing"] is True

    daily_pipeline._register_us_auto_sync_job(sched, repo=None)
    assert sched.removed == ["us_auto_sync"]
    assert len(sched.added) == 1  # disabled 不注册


def test_auto_wrapped_silent_skip_creates_no_record(tmp_path, store, monkeypatch):
    """忙时/us 非盘中 → 静默跳过零 job 记录; 空闲+盘中 → 恰 1 条 trigger=auto。"""
    from app.jobs import daily_pipeline
    from app.services import preferences, us_sync

    _patch_prefs_storage(monkeypatch)
    monkeypatch.setattr(preferences, "load",
                        lambda: {"us_auto_sync": {"enabled": True, "interval_minutes": 15}})
    us_universe.save_universe(tmp_path, ["AAPL"])
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(us_sync, "_us_provider", lambda: _FakeUSProvider())

    sched = _StubScheduler()
    daily_pipeline._register_us_auto_sync_job(sched, repo)
    wrapped = sched.added[0]["fn"]

    def _record_count() -> int:
        return len(list((tmp_path / "jobs").glob("*.json")))

    # 非盘中: 零记录
    monkeypatch.setattr(daily_pipeline, "_us_auto_sync_allowed", lambda: False)
    wrapped()
    assert _record_count() == 0

    # 忙时: 零记录
    monkeypatch.setattr(daily_pipeline, "_us_auto_sync_allowed", lambda: True)
    busy_marker = store.create(timeout_s=600, label="crypto_sync", trigger="manual")
    assert store.get(busy_marker[0]) is not None
    wrapped()
    assert _record_count() == 1  # 只有占位的 busy_marker 一条

    # 空闲 + 盘中: 恰好 1 条 trigger=auto 且成功 (任务毫秒级完成, 轮询终态记录)
    store.clear()
    wrapped()
    deadline = time.monotonic() + 10
    rec = None
    while time.monotonic() < deadline:
        recs = [r for r in store.list_recent(limit=5)
                if r.get("trigger") == "auto" and r.get("label") == "us_sync"]
        if recs and recs[0]["status"] in ("succeeded", "failed"):
            rec = recs[0]
            break
        time.sleep(0.05)
    assert rec is not None, "auto 记录未产生"
    assert rec["status"] == "succeeded"
