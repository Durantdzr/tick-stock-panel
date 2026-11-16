"""KiT 预测服务层测试 — 市场映射 / partial bar 丢弃 / 环境检查 / payload 构建。"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest

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
        assert payload["paths"] == 16 and payload["steps"] == 32 and payload["seed"] == 0
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
