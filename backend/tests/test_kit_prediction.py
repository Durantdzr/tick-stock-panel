"""KiT 预测服务层测试 — 市场映射 / partial bar 丢弃 / 环境检查 / payload 构建 / 编排流。"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest

import app.services.kit_prediction as kp
from app.services.kit_prediction import (
    KIT_MARKET_BY_ASSET,
    KitEnvError,
    build_runner_payload,
    check_kit_env,
    drop_partial_last_bar,
    resolve_kit_market,
)

US_TZ = ZoneInfo("America/New_York")


def make_daily(n: int = 320, last_date: date | None = None) -> pl.DataFrame:
    """n 根日线样本 (date/open/high/low/close/volume), 2025-01-01 起逐日。"""
    start = date(2025, 1, 1)
    dates = [date.fromordinal(start.toordinal() + i) for i in range(n)]
    if last_date is not None:
        dates[-1] = last_date
    return pl.DataFrame({
        "date": dates,
        "open": [10.0 + i * 0.01 for i in range(n)],
        "high": [10.5 + i * 0.01 for i in range(n)],
        "low": [9.5 + i * 0.01 for i in range(n)],
        "close": [10.2 + i * 0.01 for i in range(n)],
        "volume": [1000.0 + i for i in range(n)],
    })


def _today() -> date:
    from app.market_time import cn_today
    return cn_today()


# ---------------- resolve_kit_market ----------------

def test_resolve_kit_market_maps_and_rejects():
    assert KIT_MARKET_BY_ASSET == {"stock": "ashare", "us": "us", "crypto": "crypto"}
    assert resolve_kit_market("stock") == "ashare"
    assert resolve_kit_market("us") == "us"
    assert resolve_kit_market("crypto") == "crypto"
    assert resolve_kit_market("index") is None
    assert resolve_kit_market("etf") is None
    assert resolve_kit_market("unknown") is None


# ---------------- drop_partial_last_bar ----------------

def test_drop_partial_last_bar_ashare_intraday():
    df = make_daily(last_date=_today())
    morning = datetime.combine(_today(), time(10, 30))
    after_close = datetime.combine(_today(), time(16, 0))
    assert len(drop_partial_last_bar(df, "stock", now=morning)) == len(df) - 1
    assert len(drop_partial_last_bar(df, "stock", now=after_close)) == len(df)


def test_drop_partial_last_bar_ashare_completed_days_kept():
    """最后一天不是今天(已收盘的历史)不丢。"""
    df = make_daily()  # 全部 2025 年, 无今天
    morning = datetime.combine(_today(), time(10, 30))
    assert len(drop_partial_last_bar(df, "stock", now=morning)) == len(df)


def test_drop_partial_last_bar_crypto_always_drops_today():
    """crypto 24/7, 当日北京日历日收盘(午夜)前一律视为 partial。"""
    df = make_daily(last_date=_today())
    late = datetime.combine(_today(), time(23, 0))
    assert len(drop_partial_last_bar(df, "crypto", now=late)) == len(df) - 1
    after_midnight = datetime.combine(_today(), time(0, 30))
    assert len(drop_partial_last_bar(df, "crypto", now=after_midnight)) == len(df) - 1


def test_drop_partial_last_bar_us_before_close():
    from app.market_time import us_now
    us_today = us_now().date()
    df = make_daily(last_date=us_today)

    def et(h: int, m: int) -> datetime:
        return datetime.combine(us_today, time(h, m), tzinfo=US_TZ)

    assert len(drop_partial_last_bar(df, "us", now=et(15, 0))) == len(df) - 1
    assert len(drop_partial_last_bar(df, "us", now=et(16, 30))) == len(df)


# ---------------- check_kit_env ----------------

def test_check_kit_env_missing_reports_all(tmp_path: Path):
    env = check_kit_env(tmp_path)
    assert env.available is False
    assert env.missing  # 非空


def test_check_kit_env_ok(tmp_path: Path):
    kit = tmp_path / "models" / "kit"
    (kit / ".venv" / "bin").mkdir(parents=True)
    (kit / ".venv" / "bin" / "python").write_text("")
    bundle = kit / "ckpt" / "step_market_284M"
    bundle.mkdir(parents=True)
    (bundle / "step_market_284M.pt").write_text("")
    (bundle / "stats.json").write_text("{}")
    env = check_kit_env(tmp_path)
    assert env.available is True
    assert env.missing == []
    assert env.kit_root == kit
    assert env.ckpt == bundle / "step_market_284M.pt"


def test_check_kit_env_partial_missing(tmp_path: Path):
    kit = tmp_path / "models" / "kit"
    (kit / ".venv" / "bin").mkdir(parents=True)
    (kit / ".venv" / "bin" / "python").write_text("")
    env = check_kit_env(tmp_path)
    assert env.available is False
    assert any("ckpt" in m for m in env.missing)


# ---------------- build_runner_payload ----------------

def test_build_runner_payload_stamps_close_time():
    df = make_daily(320)
    kit_root = Path("/x/models/kit")
    ckpt = kit_root / "ckpt" / "step_market_284M" / "step_market_284M.pt"
    for market, suffix in (("ashare", " 15:00:00"), ("us", " 16:00:00"), ("crypto", " 23:59:00")):
        payload = build_runner_payload("SYM", market, df, kit_root=kit_root, ckpt=ckpt)
        assert payload["market"] == market
        assert payload["timescale"] == "1d"
        assert payload["symbol"] == "SYM"
        assert payload["pred_len"] == 20
        assert payload["paths"] == 8 and payload["steps"] == 16 and payload["seed"] == 0
        assert payload["kit_root"] == str(kit_root)
        assert payload["ckpt"] == str(ckpt)
        assert len(payload["bars"]) == 320
        assert payload["bars"][-1]["datetime"].endswith(suffix)
        assert set(payload["bars"][0]) == {"datetime", "open", "high", "low", "close", "volume"}


def test_build_runner_payload_row_limit():
    df = make_daily(400)
    payload = build_runner_payload("SYM", "ashare", df, kit_root=Path("/x"), ckpt=Path("/y"))
    assert len(payload["bars"]) == 320


def test_build_runner_payload_insufficient_raises():
    df = make_daily(200)
    with pytest.raises(ValueError, match="250"):
        build_runner_payload("SYM", "ashare", df, kit_root=Path("/x"), ckpt=Path("/y"))


def test_kit_env_error_is_exception():
    assert issubclass(KitEnvError, RuntimeError)


# ================================================================
# Task 3: 编排 — 汇总 / 子进程流 / 单飞锁
# ================================================================

FIXTURES = Path(__file__).parent / "fixtures"
STUB_RUNNER = FIXTURES / "kit_stub_runner.py"
STUB_FAIL = FIXTURES / "kit_stub_runner_fail.py"
STUB_SLOW = FIXTURES / "kit_stub_runner_slow.py"


def _make_result() -> dict:
    """4 路径 × 2 bar 的 ohlcv 结果, prev_close=100 (手算值见 test_summarize_prediction_math)。"""
    close = [[100.0, 110.0], [104.0, 100.0], [90.0, 105.0], [108.0, 120.0]]
    return {
        "ok": True, "market": "ashare", "timescale": "1d",
        "anchor_ts": "2026-11-17 15:00:00",
        "prev_close": 100.0,
        "future_timestamps": ["2026-11-18 15:00:00", "2026-11-19 15:00:00"],
        "ohlcv": {
            "close": close,
            "open": [[c - 1.0 for c in p] for p in close],
            "high": [[c + 1.0 for c in p] for p in close],
            "low": [[c - 2.0 for c in p] for p in close],
            "volume": [[1000.0] * 2 for _ in close],
        },
        "instrument": {"symbol": "SYM", "inst_id": 0, "sector_id": 0, "matched": False},
        "meta": {"model_step": 50000, "Lc": 250, "Lh": 2, "n_paths": 4, "steps": 32,
                 "seed": 0, "precision": "fp32", "device": "cpu", "elapsed_s": 9.9,
                 "calendar": "xshg", "smoke": False},
    }


def test_summarize_prediction_math():
    p = kp.summarize_prediction(_make_result(), "SYM", "stock", "ashare")
    s = p["summary"]
    # terminal rel = [0.10, 0.00, 0.05, 0.20]
    assert s["median_return"] == pytest.approx(0.075)
    assert s["p10_return"] == pytest.approx(0.015)
    assert s["p90_return"] == pytest.approx(0.17)
    # max gain per path = [0.10, 0.04, 0.05, 0.20]
    assert s["median_max_gain"] == pytest.approx(0.075)
    # min rel per path = [0.0, 0.0, -0.10, 0.08]
    assert s["median_max_drawdown"] == pytest.approx(0.0)
    # per-bar median close: t1 median(100,104,90,108)=102; t2 median(110,100,105,120)=107.5
    assert p["bars"][0]["close"] == pytest.approx(102.0)
    assert p["bars"][1]["close"] == pytest.approx(107.5)
    # close_p10 t1: sorted(90,100,104,108) pos0.3 → 93.0
    assert p["bars"][0]["close_p10"] == pytest.approx(93.0)


def test_summarize_rounding_and_dates():
    r = _make_result()
    r["ohlcv"]["close"][0][0] = 100.123456789  # >6 位小数
    p = kp.summarize_prediction(r, "SYM", "stock", "ashare")
    assert p["anchor_date"] == "2026-11-17"
    assert [b["date"] for b in p["bars"]] == ["2026-11-18", "2026-11-19"]
    assert p["prev_close"] == 100.0
    assert p["model"]["cutoff"] == kp.KIT_MODEL_CUTOFF
    assert p["summary"]["median_return"] == round(p["summary"]["median_return"], 4)
    # 价格 6 位舍入: median close t1 = median(100.123456789,104,90,108) = 102.0 → median(100.123457,104,90,108)
    med = float(np.median([100.123457, 104.0, 90.0, 108.0]))
    assert p["bars"][0]["close"] == pytest.approx(round(med, 6))


def _stub_env():
    return kp.KitEnv(kit_root=Path("/x"), python=Path(sys.executable),
                     ckpt=STUB_RUNNER, stats=STUB_RUNNER, missing=[])


def _stub_repo(n_bars: int = 320, asset_type: str = "stock"):
    return SimpleNamespace(
        resolve_asset_type=lambda s: asset_type,
        get_daily_asset=lambda at, s, start, end, columns=None: make_daily(n_bars),
        store=SimpleNamespace(data_dir=Path("/tmp")),
    )


def _patch_subprocess(monkeypatch, runner_path: Path):
    monkeypatch.setattr(kp, "check_kit_env", lambda d: _stub_env())
    monkeypatch.setattr(kp, "_runner_path", lambda: runner_path)


async def _collect(events_iter):
    return [json.loads(e) async for e in events_iter]


async def test_predict_stream_happy_path(monkeypatch):
    _patch_subprocess(monkeypatch, STUB_RUNNER)
    events = await _collect(kp.predict_stream(_stub_repo(), Path("/tmp"), "SYM", timeout_s=30))
    stages = [e["type"] for e in events]
    assert stages == ["stage", "stage", "stage", "stage", "done"]
    assert [e["stage"] for e in events[:4]] == ["env", "data", "model", "sampling"]
    pred = events[-1]["prediction"]
    assert pred["symbol"] == "SYM"
    assert pred["asset_type"] == "stock"
    assert pred["market"] == "ashare"
    assert pred["summary"]["median_return"] is not None
    assert len(pred["bars"]) == 2


async def test_predict_stream_env_missing(tmp_path):
    with pytest.raises(kp.KitEnvError):
        async for _ in kp.predict_stream(_stub_repo(), tmp_path, "SYM"):
            pass


async def test_predict_stream_insufficient_history(monkeypatch):
    _patch_subprocess(monkeypatch, STUB_RUNNER)
    events = await _collect(kp.predict_stream(_stub_repo(n_bars=50), Path("/tmp"), "SYM", timeout_s=30))
    assert len(events) == 2  # env stage + error
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "insufficient_history"


async def test_predict_stream_unsupported_market(monkeypatch):
    _patch_subprocess(monkeypatch, STUB_RUNNER)
    events = await _collect(kp.predict_stream(_stub_repo(asset_type="index"), Path("/tmp"), "000300.SH", timeout_s=30))
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "unsupported_market"


async def test_predict_stream_subprocess_crash(monkeypatch):
    _patch_subprocess(monkeypatch, STUB_FAIL)
    events = await _collect(kp.predict_stream(_stub_repo(), Path("/tmp"), "SYM", timeout_s=30))
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "inference_failed"
    assert "boom" in events[-1]["message"]  # stderr 摘要进 message


async def test_predict_stream_timeout(monkeypatch):
    _patch_subprocess(monkeypatch, STUB_SLOW)
    events = await _collect(kp.predict_stream(_stub_repo(), Path("/tmp"), "SYM", timeout_s=0.5))
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "timeout"


def test_single_flight_lock(tmp_path):
    lock = kp.KitPredictLock(tmp_path)
    lock.acquire()
    try:
        with pytest.raises(kp.KitBusyError):
            kp.KitPredictLock(tmp_path).acquire()
    finally:
        lock.release()
    # release 后可再取
    lock2 = kp.KitPredictLock(tmp_path)
    lock2.acquire()
    lock2.release()


def test_runner_path_exists():
    """_runner_path 必须解析到真实存在的 runner (monkeypatch 测试覆盖不到的集成点)。"""
    p = kp._runner_path()
    assert p.is_file(), f"runner 不存在: {p}"
    assert p.name == "kit_predict_runner.py"
