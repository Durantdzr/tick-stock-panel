"""美股 (us) 资产类型测试。

覆盖: 交易日历 (XNYS 节假日)、白名单配置、repository 路由 (目录/resolve/
enriched)、trading_rules us 契约、因子开放集合、同步服务 (fake provider)、
整股取整。不依赖真实网络 (provider 层注入桩)。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.services import us_sync, us_trading_day, us_universe
from app.services.trading_rules import deny_reason, is_feature_enabled
from app.tickflow.repository import DataStore, KlineRepository, daily_dirname, enriched_dirname

# ================================================================
# 交易日历 (XNYS)
# ================================================================

def test_xnys_holiday_excluded():
    """2026-11-26 为感恩节 (周四), 非交易日; 11-27 半日市仍为交易日。"""
    assert us_trading_day.is_us_trading_day(date(2026, 11, 26)) is False
    assert us_trading_day.is_us_trading_day(date(2026, 11, 27)) is True


def test_xnys_weekend_excluded():
    assert us_trading_day.is_us_trading_day(date(2026, 11, 7)) is False   # 周六
    assert us_trading_day.is_us_trading_day(date(2026, 11, 6)) is True    # 周五


def test_xnys_consistency_known_dates():
    """圣诞/新年假期边界 (抽查 XNYS 2026 年末)。"""
    assert us_trading_day.is_us_trading_day(date(2026, 12, 25)) is False  # 圣诞节
    assert us_trading_day.is_us_trading_day(date(2026, 12, 24)) is True


# ================================================================
# 白名单配置
# ================================================================

def test_us_universe_default_generated(tmp_path):
    symbols = us_universe.load_universe(tmp_path)
    assert "AAPL" in symbols and "MSFT" in symbols
    assert us_universe.universe_path(tmp_path).exists()


def test_us_universe_roundtrip(tmp_path):
    saved = us_universe.save_universe(tmp_path, ["aapl", "AAPL", "msft"])
    assert saved == ["AAPL", "MSFT"]
    assert us_universe.load_universe(tmp_path) == ["AAPL", "MSFT"]


def test_us_universe_corrupt_file_falls_back(tmp_path):
    path = us_universe.universe_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json{", encoding="utf-8")
    assert us_universe.load_universe(tmp_path) == us_universe.DEFAULT_US


# ================================================================
# trading_rules us 契约
# ================================================================

def test_us_rules_and_features():
    assert is_feature_enabled("us", "backtest") is True
    assert is_feature_enabled("us", "daily_screener") is True
    assert is_feature_enabled("us", "dashboard") is True
    for feature in ("realtime_monitor", "minute_screener", "limit_ladder",
                    "sector_heat", "financial"):
        assert is_feature_enabled("us", feature) is False
    rules = us_trading_day and __import__("app.services.trading_rules", fromlist=["get_trading_rules"]).get_trading_rules("us")
    assert rules["t_plus_1"] is False
    assert rules["lot_size"] == 1
    assert rules["price_limit"] is False
    assert rules["volume_unit"] == "share"


def test_us_deny_reason_readables():
    assert "美股" in (deny_reason("us", "realtime_monitor") or "")
    assert deny_reason("us", "backtest") is None
    assert deny_reason("us", "dashboard") is None


# ================================================================
# repository 路由
# ================================================================

def test_repo_dirname_routing_us():
    assert enriched_dirname("us") == "kline_us_enriched"
    assert daily_dirname("us") == "kline_us_daily"


_US_DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]


def _daily_df(symbol: str, days: list[date]) -> pl.DataFrame:
    n = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": days,
        "open": [100.0] * n,
        "high": [110.0] * n,
        "low": [95.0] * n,
        "close": [105.0] * n,
        "volume": [1_000_000.0] * n,  # 股 (美股口径)
        "amount": [105_000_000.0] * n,
    })


def test_repo_resolve_and_daily_us(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    days = [date(2026, 10, 1), date(2026, 10, 2)]
    repo.save_us_instruments(pl.DataFrame({
        "symbol": ["AAPL"], "name": ["苹果"], "code": ["AAPL"],
        "exchange": ["NASDAQ"], "asset_type": ["us"], "source": ["eastmoney_us"],
    }))
    repo.append_daily_asset("us", _daily_df("AAPL", days))
    repo.invalidate_enriched_asset("us")

    assert repo.resolve_asset_type("AAPL") == "us"
    assert repo.resolve_asset_type("600519.SH") == "stock"
    assert repo.get_name_map(["AAPL"])["AAPL"] == "苹果"

    df = repo.get_daily_asset("us", "AAPL", date(2026, 10, 1), date(2026, 10, 2))
    assert df.height == 2
    assert df["volume"].to_list() == [1_000_000.0, 1_000_000.0]  # 股, 不换算


def test_repo_enriched_us(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(70)]
    repo.save_us_instruments(pl.DataFrame({
        "symbol": ["AAPL"], "name": ["苹果"], "code": ["AAPL"],
        "exchange": ["NASDAQ"], "asset_type": ["us"], "source": ["eastmoney_us"],
    }))
    repo.append_daily_asset("us", _daily_df("AAPL", days))
    us_sync._recompute_us_enriched(repo)
    repo.invalidate_enriched_asset("us")

    df, latest = repo.get_enriched_latest_asset("us")
    assert latest == days[-1]
    assert "ma5" in df.columns and "rsi_14" in df.columns
    assert "turnover_rate" not in df.columns       # 无股本口径
    assert "consecutive_limit_ups" not in df.columns  # 无涨跌停口径


# ================================================================
# 因子开放集合
# ================================================================

def test_us_factors_match_crypto_set():
    from app.factors.registry import all_factors
    us = {f.id for f in all_factors("us")}
    crypto = {f.id for f in all_factors("crypto")}
    assert len(us) == 68
    assert us == crypto  # 东财美股同样无股本/涨跌停, 排除集合一致


# ================================================================
# us_sync (fake provider)
# ================================================================

class _FakeUSProvider:
    """最小美股 provider 桩: 固定 instruments 全市场 + 白名单日K帧。"""

    name = "fakeus"

    def __init__(self, daily_dates: int = 70):
        self.daily_dates = daily_dates
        self.daily_calls: list[tuple] = []

    def fetch_instruments_page(self, page: int) -> tuple[list[dict], int]:
        """一页 200 只: 全市场 4 只 → 第 1 页即完成。"""
        rows = pl.DataFrame({
            "symbol": ["AAPL", "MSFT", "NVDA", "GOOGL"],
            "name": ["苹果", "微软", "英伟达", "谷歌"],
            "code": ["AAPL", "MSFT", "NVDA", "GOOGL"],
            "exchange": ["NASDAQ"] * 4,
            "asset_type": ["us"] * 4,
            "source": ["fakeus"] * 4,
        }).to_dicts()
        return (rows, 4) if page == 1 else ([], 4)

    def get_instruments(self, asset_type="us"):
        return pl.DataFrame(self.fetch_instruments_page(1)[0])

    def get_daily(self, symbols, start_time, end_time, asset_type="us", on_chunk_done=None):
        self.daily_calls.append((tuple(symbols), start_time))
        end = date.today() - timedelta(days=1)  # 最新 bar = 昨天
        days = [end - timedelta(days=self.daily_dates - 1 - i) for i in range(self.daily_dates)]
        if start_time is not None:  # 增量语义: 只返回 start_time 之后的 bar
            days = [d for d in days if d >= start_time.date()]
        frames = []
        for symbol in symbols:
            df = _daily_df(symbol, days)
            frames.append(df)
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_minute(self, symbols, start_time, end_time, asset_type="us", freq="1m",
                   on_chunk_done=None):
        return pl.DataFrame()

    def close(self):
        pass


@pytest.fixture()
def fake_us_provider(monkeypatch):
    provider = _FakeUSProvider()
    monkeypatch.setattr(us_sync, "_us_provider", lambda: provider)
    return provider


def test_us_sync_instruments_full_market(tmp_path, fake_us_provider):
    repo = KlineRepository(DataStore(tmp_path))
    count = us_sync.sync_us_instruments(repo)
    assert count == 4  # 全市场目录落维表 (与白名单无关)
    assert repo.resolve_asset_type("NVDA") == "us"


def test_us_sync_full_and_incremental(tmp_path, fake_us_provider):
    us_universe.save_universe(tmp_path, ["AAPL", "MSFT"])
    repo = KlineRepository(DataStore(tmp_path))

    first = us_sync.run_us_sync(repo)
    assert first["ok"] is True
    assert first["instruments"] == 4  # 维表全市场
    assert first["daily"]["symbols"] == 2  # K 线仅白名单
    df, latest = repo.get_enriched_latest_asset("us")
    assert latest is not None and df.height == 2

    # 增量: 无新数据 → rows=0, 起点从最新日期 +1 天
    second = us_sync.run_us_sync(repo)
    assert second["ok"] is True and second["daily"]["rows"] == 0
    starts = [s for _, s in fake_us_provider.daily_calls[-2:]]
    assert all(s is not None and s.date() == date.today() for s in starts)


def test_us_sync_holiday_skip(tmp_path, monkeypatch):
    """节假日门控: skip_non_trading_day=True 且美东非交易日 → 跳过且不调 provider。"""
    repo = KlineRepository(DataStore(tmp_path))
    called = {"n": 0}

    def _fake_instruments(asset_type="us"):
        called["n"] += 1
        return pl.DataFrame()

    monkeypatch.setattr(us_sync, "is_us_trading_session_day", lambda: False)
    monkeypatch.setattr(us_sync, "sync_us_instruments", _fake_instruments)
    result = us_sync.run_us_sync(repo, skip_non_trading_day=True)
    assert result.get("skipped") == "us_holiday"
    assert called["n"] == 0  # 节假日不触发任何拉取


def test_us_sync_whitelist_error_fail_closed(tmp_path, fake_us_provider):
    """全白名单标的拉取失败 → fail-closed (非增量首次场景)。"""
    us_universe.save_universe(tmp_path, ["AAPL"])
    repo = KlineRepository(DataStore(tmp_path))

    def _boom(symbols, start_time, end_time, asset_type="us", on_chunk_done=None):
        raise ProviderRequestError("网络失败")

    fake_us_provider.get_daily = _boom
    result = us_sync.run_us_sync(repo)
    assert result["ok"] is False
    assert "网络失败" in (result.get("error") or "")


def test_run_us_sync_reports_progress(tmp_path, fake_us_provider):
    """P1 回归: run_us_sync 接受 on_progress 并按阶段回调
    (_run_tracked 以 fn(on_progress=progress) 调用任务函数)。"""
    us_universe.save_universe(tmp_path, ["AAPL"])
    repo = KlineRepository(DataStore(tmp_path))
    events: list[str] = []
    result = us_sync.run_us_sync(
        repo, on_progress=lambda stage, pct, msg: events.append(stage),
    )
    assert result["ok"] is True
    assert events == ["instruments", "daily", "enriched"]


# ================================================================
# 整股取整 (lot_size=1)
# ================================================================

def test_lot_round_shares_lot_size_1_whole_shares():
    from app.backtest.engine import _lot_round_shares
    # 美股: 1 股整股 (10000/95 = 105.26 → 105 股)
    assert _lot_round_shares(10_000.0, 95.0, 0.0, 1.0) == pytest.approx(105.0)
    assert _lot_round_shares(9_499.0, 95.0, 0.0, 1.0) == pytest.approx(99.0)
