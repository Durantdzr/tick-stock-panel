"""crypto (加密货币) 资产类型管道测试。

覆盖: 白名单配置、repository 路由 (目录/resolve/append/enriched)、
币安 provider 契约 (时区/后缀/单位/fail-closed)、crypto_sync 增量语义、
能力矩阵的市场专用源隔离。不依赖真实网络。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from app.data_providers.base import ProviderRequestError
from app.plugins.binance import provider as bp
from app.services import crypto_sync, crypto_universe
from app.tickflow.repository import DataStore, KlineRepository

CN_TZ = timezone(timedelta(hours=8))


# ================================================================
# 白名单配置
# ================================================================

def test_universe_default_generated(tmp_path):
    universe = crypto_universe.load_universe(tmp_path)
    assert universe["spot"] and universe["futures"]
    assert "BTCUSDT" in universe["spot"]
    # 首次读取落盘默认清单
    assert crypto_universe.universe_path(tmp_path).exists()


def test_universe_roundtrip_and_perp_suffix(tmp_path):
    saved = crypto_universe.save_universe(tmp_path, {
        "spot": ["btcusdt", "BTCUSDT", "ethusdt"],   # 去重 + 大写
        "futures": ["BTCUSDT.P", "ETHUSDT"],          # .P 后缀剥离
    })
    assert saved["spot"] == ["BTCUSDT", "ETHUSDT"]
    assert saved["futures"] == ["BTCUSDT", "ETHUSDT"]
    assert crypto_universe.internal_symbols(saved) == ["BTCUSDT.P", "ETHUSDT.P", "BTCUSDT", "ETHUSDT"]


def test_universe_corrupt_file_falls_back(tmp_path):
    path = crypto_universe.universe_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json{", encoding="utf-8")
    universe = crypto_universe.load_universe(tmp_path)
    assert universe == crypto_universe.default_universe()


def test_universe_explicit_empty_market_kept(tmp_path):
    """显式空清单表示清空该市场 (API 层仅禁止双空)。"""
    crypto_universe.save_universe(tmp_path, {"spot": ["BTCUSDT"], "futures": []})
    universe = crypto_universe.load_universe(tmp_path)
    assert universe == {"spot": ["BTCUSDT"], "futures": []}


# ================================================================
# repository 路由
# ================================================================

@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


_CRYPTO_DAILY_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]


def _daily_df(symbol: str, days: list[date]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol] * len(days),
        "date": days,
        "open": [100.0] * len(days),
        "high": [110.0] * len(days),
        "low": [95.0] * len(days),
        "close": [105.0] * len(days),
        "volume": [1.5] * len(days),
        "amount": [150.0] * len(days),
    })


def test_repo_dirname_routing():
    from app.tickflow.repository import daily_dirname, enriched_dirname
    assert enriched_dirname("crypto") == "kline_crypto_enriched"
    assert daily_dirname("crypto") == "kline_crypto_daily"


def test_repo_resolve_and_daily(tmp_path, repo):
    days = [date(2026, 10, 1), date(2026, 10, 2)]
    repo.save_crypto_instruments(pl.DataFrame({
        "symbol": ["BTCUSDT"], "name": ["BTC"], "code": ["BTCUSDT"],
        "exchange": ["BINANCE"], "asset_type": ["crypto"], "source": ["binance"],
        "market": ["spot"],
    }))
    repo.append_daily_asset("crypto", _daily_df("BTCUSDT", days))
    repo.invalidate_enriched_asset("crypto")

    assert repo.resolve_asset_type("BTCUSDT") == "crypto"
    assert repo.resolve_asset_type("600519.SH") == "stock"
    assert repo.resolve_asset_type("BTCUSDT.P") == "stock"  # 未同步的合约回落 stock

    df = repo.get_daily_asset("crypto", "BTCUSDT", date(2026, 10, 1), date(2026, 10, 2))
    assert df.height == 2
    assert df["close"].to_list() == [105.0, 105.0]
    # name map 合并 crypto
    assert repo.get_name_map(["BTCUSDT"])["BTCUSDT"] == "BTC"


def test_repo_enriched_crypto(tmp_path, repo):
    """crypto enriched: 空因子/股本路径, 指标可算, turnover_rate/涨跌停列缺失。"""
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(70)]
    repo.save_crypto_instruments(pl.DataFrame({
        "symbol": ["BTCUSDT"], "name": ["BTC"], "code": ["BTCUSDT"],
        "exchange": ["BINANCE"], "asset_type": ["crypto"], "source": ["binance"],
        "market": ["spot"],
    }))
    repo.append_daily_asset("crypto", _daily_df("BTCUSDT", days))
    crypto_sync._recompute_crypto_enriched(repo)
    repo.invalidate_enriched_asset("crypto")
    df, latest = repo.get_enriched_latest_asset("crypto")
    assert latest == days[-1]
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["symbol"] == "BTCUSDT"
    assert "ma5" in df.columns and "rsi_14" in df.columns
    assert "turnover_rate" not in df.columns
    assert "consecutive_limit_ups" not in df.columns


# ================================================================
# 币安 provider 契约
# ================================================================

def test_binance_symbol_namespace():
    assert bp.split_symbol("BTCUSDT") == ("spot", "BTCUSDT")
    assert bp.split_symbol("BTCUSDT.P") == ("futures", "BTCUSDT")
    assert bp.join_symbol("futures", "BTCUSDT") == "BTCUSDT.P"
    assert bp.join_symbol("spot", "BTCUSDT") == "BTCUSDT"


def test_binance_daily_row_beijing_date():
    """币安日线 UTC 00:00 开盘 (= 北京 08:00), date 与 UTC 日期一致。"""
    # 2024-01-02 00:00 UTC = 北京 2024-01-02 08:00
    utc_ms = 1704153600000
    row = bp.BinanceProvider._daily_row("BTCUSDT", [utc_ms, "42000", "43000", "41000", "42500",
                                                    "123.4", 0, "5200000"])
    assert row["date"] == date(2024, 1, 2)
    assert row["volume"] == 123.4   # 基础币数量, 不做手换算
    assert row["amount"] == 5200000.0  # USDT


def test_binance_minute_beijing_naive():
    from app.plugins.binance.provider import _beijing_naive_of_ms
    # 2024-01-02 00:30 UTC = 北京 08:30
    utc_ms = 1704155400000
    assert _beijing_naive_of_ms(utc_ms) == datetime(2024, 1, 2, 8, 30)


def test_binance_ticker_record_units():
    rec = bp.BinanceProvider._ticker_record("futures", "BTCUSDT", {
        "symbol": "BTCUSDT", "lastPrice": "78000", "priceChange": "400",
        "priceChangePercent": "0.52", "openPrice": "77600", "highPrice": "78500",
        "lowPrice": "77000", "volume": "48596.8", "quoteVolume": "3790000000",
    }, 1234)
    assert rec["symbol"] == "BTCUSDT.P"
    assert rec["change_pct"] == pytest.approx(0.0052)  # 百分数 → 小数制
    assert rec["prev_close"] == pytest.approx(77600.0)  # 无昨收列时由涨跌额推导
    assert rec["turnover_rate"] is None


def test_binance_get_daily_rejects_non_crypto():
    provider = bp.BinanceProvider()
    with pytest.raises(ProviderRequestError):
        provider.get_daily(["600519.SH"], None, None, asset_type="stock")


def test_binance_get_daily_unknown_symbol_fail_closed():
    provider = bp.BinanceProvider()
    with pytest.raises(ProviderRequestError):
        provider.get_daily(["不合法"], None, None, asset_type="crypto")


# ================================================================
# crypto_sync (fake provider)
# ================================================================

class _FakeCryptoProvider:
    """最小 crypto provider 桩: 返回固定 instruments/daily/minute 帧。"""

    name = "fakecrypto"

    def __init__(self, daily_dates: int = 70):
        self.daily_dates = daily_dates
        self.daily_calls: list[tuple] = []

    def get_instruments(self, asset_type="crypto"):
        return pl.DataFrame({
            "symbol": ["BTCUSDT", "BTCUSDT.P", "ETHUSDT", "ETHUSDT.P"],
            "name": ["BTC", "BTC", "ETH", "ETH"],
            "code": ["BTCUSDT"] * 4,
            "exchange": ["BINANCE"] * 4,
            "asset_type": ["crypto"] * 4,
            "source": ["fakecrypto"] * 4,
            "market": ["spot", "futures", "spot", "futures"],
        })

    def get_daily(self, symbols, start_time, end_time, asset_type="crypto", on_chunk_done=None):
        self.daily_calls.append((tuple(symbols), start_time))
        days = [date(2026, 9, 1) + timedelta(days=i) for i in range(self.daily_dates)]
        if start_time is not None:  # 增量语义: 只返回 start_time 之后的bar
            days = [d for d in days if d >= start_time.date()]
        frames = []
        for symbol in symbols:
            base = 100.0 if symbol.endswith("USDT") else 50.0
            df = _daily_df(symbol, days)
            frames.append(df.with_columns((pl.col("open") + base - 100).alias("open")))
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    def get_minute(self, symbols, start_time, end_time, asset_type="crypto", freq="1m",
                   on_chunk_done=None):
        return pl.DataFrame()

    def close(self):
        pass


@pytest.fixture()
def fake_provider(monkeypatch):
    provider = _FakeCryptoProvider()
    monkeypatch.setattr(crypto_sync, "_crypto_provider", lambda: provider)
    return provider


def test_crypto_sync_full_and_incremental(tmp_path, repo, fake_provider):
    crypto_universe.save_universe(tmp_path, {"spot": ["BTCUSDT"], "futures": ["BTCUSDT"]})

    first = crypto_sync.run_crypto_sync(repo)
    assert first["ok"] is True
    assert first["instruments"] == 2  # 白名单过滤后
    daily = first["daily"]
    assert daily["symbols"] == 2 and daily["rows"] > 0

    df, latest = repo.get_enriched_latest_asset("crypto")
    assert latest is not None and df.height == 2

    # 增量: 无新数据 → rows=0 且不再拉全量 (start_time 从最新日期 +1 天起)
    second = crypto_sync.run_crypto_sync(repo)
    assert second["ok"] is True and second["daily"]["rows"] == 0
    starts = [s for _, s in fake_provider.daily_calls[-2:]]
    assert all(s is not None and s.date() == date(2026, 11, 10) for s in starts)


def test_crypto_sync_whitelist_filters_instruments(tmp_path, repo, fake_provider):
    crypto_universe.save_universe(tmp_path, {"spot": ["ETHUSDT"], "futures": []})
    result = crypto_sync.run_crypto_sync(repo)
    assert result["instruments"] == 1
    assert repo.get_crypto_instruments()["symbol"].to_list() == ["ETHUSDT"]


# ================================================================
# 能力矩阵: 市场专用源隔离
# ================================================================

def test_capability_matrix_excludes_market_scoped_sources(monkeypatch):
    from app.data_providers import capabilities as caps

    def fake_list_plugins():
        return [
            {"name": "binance", "display_name": "币安", "datasets": ["daily", "realtime"],
             "asset_types": ["crypto"], "available": True, "status": "ok"},
            {"name": "eastmoney", "display_name": "东方财富", "datasets": ["daily"],
             "asset_types": [], "available": True, "status": "ok"},
        ]

    monkeypatch.setattr(caps.custom_sources, "list_plugins", fake_list_plugins)
    monkeypatch.setattr(caps.custom_sources, "list_sources", lambda: [])

    matrix = caps.build_capability_matrix({"daily_data_provider": "tickflow"})
    daily = next(c for c in matrix["capabilities"] if c["id"] == "daily")
    names = [c["name"] for c in daily["candidates"]]
    assert "eastmoney" in names
    assert "binance" not in names  # 市场专用源不进 A 股能力候选

    rows = caps.resolve_capability_sources("expert")
    daily_row = next(r for r in rows if r["id"] == "daily")
    assert daily_row["usable"] is True  # tickflow 在 expert 档可用
