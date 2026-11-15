"""crypto 市场总览装配 (crypto_overview) 测试。

不依赖真实网络: 快照行通过 monkeypatch crypto_overview._snapshot_rows 注入,
本地 enriched 用与 test_crypto_pipeline 相同的 KlineRepository(tmp_path) 模式。
覆盖: 快照优先 / 快照失败降级本地 / 双无空态、榜单形状、口径声明字段、
合约排除、涨跌停相关恒零。
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.services import crypto_overview
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture()
def repo(tmp_path):
    return KlineRepository(DataStore(tmp_path))


def _snapshot_rows() -> tuple[list[dict], bool]:
    """合成 24hr 快照行 (已过生产 _records_to_rows 过滤: 不含合约)。"""
    return [
        {"symbol": "BTCUSDT", "name": "BTC", "close": 60000.0, "change_pct": 0.021,
         "amount": 1_000_000_000.0, "board": "现货"},
        {"symbol": "ETHUSDT", "name": "ETH", "close": 3000.0, "change_pct": -0.004,
         "amount": 500_000_000.0, "board": "现货"},
        {"symbol": "PEPEUSDT", "name": "PEPE", "close": 0.0001, "change_pct": 0.35,
         "amount": 50_000_000.0, "board": "现货"},
    ], True


def test_records_to_rows_excludes_perp():
    """合约 (.P) 不入总览榜单 — 与现货同名同波动, 混入会重复计数。"""
    rows = crypto_overview._records_to_rows([
        {"symbol": "BTCUSDT", "name": "BTC", "last_price": 60000.0,
         "change_pct": 0.021, "amount": 1.0},
        {"symbol": "BTCUSDT.P", "name": "BTC", "last_price": 60010.0,
         "change_pct": 0.021, "amount": 2.0},
        {"symbol": "", "name": "x", "last_price": 1.0, "change_pct": 0.0, "amount": 0.0},
    ])
    assert [r["symbol"] for r in rows] == ["BTCUSDT"]


def _local_daily_df(symbol: str, days: list[date]) -> pl.DataFrame:
    """递增收盘序列 (100+i), 保证 MA5 / 60 日新高列有值。"""
    n = len(days)
    return pl.DataFrame({
        "symbol": [symbol] * n,
        "date": days,
        "open": [100.0] * n,
        "high": [110.0 + i for i in range(n)],
        "low": [95.0] * n,
        "close": [105.0 + i for i in range(n)],
        "volume": [1.5] * n,
        "amount": [150.0] * n,
    })


def _seed_local(repo, days=None):
    from app.services import crypto_sync

    # 70 天窗口: MA5/60日高低需要足够历史, 2 天数据算不出趋势列
    days = days or [date(2026, 10, 2) - timedelta(days=69 - i) for i in range(70)]
    repo.save_crypto_instruments(pl.DataFrame({
        "symbol": ["BTCUSDT"], "name": ["BTC"], "code": ["BTCUSDT"],
        "exchange": ["BINANCE"], "asset_type": ["crypto"], "source": ["binance"],
        "market": ["spot"],
    }))
    repo.append_daily_asset("crypto", _local_daily_df("BTCUSDT", days))
    # enriched 分区由同步管道重算 (append 只写原始日K)
    crypto_sync._recompute_crypto_enriched(repo)
    repo.invalidate_enriched_asset("crypto")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认注入快照; 单测需要"快照失败"场景时另行覆盖。"""
    monkeypatch.setattr(crypto_overview, "_snapshot_rows", _snapshot_rows)


# ================================================================
# 快照路径 (主路径)
# ================================================================

def test_overview_from_snapshot(repo):
    data = crypto_overview.build_crypto_overview(repo)
    assert data["market"] == "crypto"
    assert data["data_source"] == "binance_snapshot"
    assert data["data_window"] == "24h_rolling"
    # 合约被排除: breadth 只数 3 个现货对
    assert data["breadth"]["total"] == 3
    assert data["breadth"]["up"] == 2 and data["breadth"]["down"] == 1
    # 榜单: 涨幅榜首是 PEPE, 无合约
    assert data["top_gainers"][0]["symbol"] == "PEPEUSDT"
    assert all(not r["symbol"].endswith(".P") for r in data["top_gainers"])
    assert all(r["turnover_rate"] is None for r in data["top_gainers"])
    # 成交额榜按 amount 降序
    assert data["turnover_leaders"][0]["symbol"] == "BTCUSDT"
    # 主流币 mini 卡: 快照里没有的 BNB/SOL/XRP 为 null 值占位, 不报错
    idx = {r["symbol"]: r for r in data["indices"]}
    assert idx["BTCUSDT"]["last_price"] == 60000.0
    assert idx["SOLUSDT"]["last_price"] is None


def test_overview_crypto_no_price_limit_semantics(repo):
    """涨跌停/换手率/板块口径对 crypto 恒为零或空 (交易规则边界的数据面呈现)。"""
    data = crypto_overview.build_crypto_overview(repo)
    assert data["limit"]["limit_up"] == 0 and data["limit"]["tiers"] == []
    assert data["activity"]["avg_turnover"] == 0
    assert data["boards"] == []
    # 概念维度 (DeFi/MEME/L1 本地映射聚合): 结构契约; 空数据时为空列表
    assert set(data["concept_rank"]) == {"leading", "lagging"}
    assert data["active_leaders"] == []
    assert data["radar"] is None and data["emotion"] is None


# ================================================================
# 降级路径
# ================================================================

def test_overview_falls_back_to_local_when_snapshot_unavailable(repo, monkeypatch):
    _seed_local(repo)
    monkeypatch.setattr(crypto_overview, "_snapshot_rows", lambda: ([], False))
    data = crypto_overview.build_crypto_overview(repo)
    assert data["data_source"] == "local_enriched"
    assert data["data_window"] == "trading_day"
    assert data["as_of"] == "2026-10-02"
    assert data["breadth"]["total"] == 1
    # 指数卡回退本地收盘价 (递增序列末日: 105+69)
    idx = {r["symbol"]: r for r in data["indices"]}
    assert idx["BTCUSDT"]["last_price"] == 174.0


def test_overview_empty_when_no_data_at_all(repo, monkeypatch):
    monkeypatch.setattr(crypto_overview, "_snapshot_rows", lambda: ([], False))
    data = crypto_overview.build_crypto_overview(repo)
    assert data["data_source"] == "none"
    assert data["breadth"]["total"] == 0
    assert data["top_gainers"] == []
    assert data["as_of"] is None


def test_trend_from_local_enriched_even_with_snapshot(repo):
    """趋势维度始终来自本地 enriched (快照无历史), 未同步时为零值。"""
    data = crypto_overview.build_crypto_overview(repo)  # 快照成功但本地无数据
    assert data["trend"]["above_ma5"] == 0
    assert data["trend"]["new_high"] == 0

    _seed_local(repo)
    data = crypto_overview.build_crypto_overview(repo)
    # 递增序列: 最新日应站上 MA5 且为 60 日新高
    assert data["trend"]["above_ma5"] >= 1
    assert data["trend"]["new_high"] >= 1
