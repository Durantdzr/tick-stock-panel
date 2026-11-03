"""交易规则注册表 (trading_rules) 测试。

覆盖: 注册表不变量 (features 全键覆盖 / 已知市场可查)、fail-closed
(未知市场/功能一律 False)、deny_reason 可读性、payload 形状,
以及回测/监控入口守卫对 crypto 的 fail-closed 行为。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.trading_rules import (
    FEATURE_KEYS,
    deny_reason,
    get_trading_rules,
    is_feature_enabled,
    known_asset_types,
    market_rules_payload,
)

# ================================================================
# 注册表不变量
# ================================================================

def test_every_market_covers_all_feature_keys():
    """任何市场的 features 必须覆盖全部功能键 — 防止新增键后漏声明静默关闭。"""
    for asset_type in known_asset_types():
        rules = get_trading_rules(asset_type)
        missing = [k for k in FEATURE_KEYS if k not in rules["features"]]
        assert not missing, f"{asset_type} 缺少功能声明: {missing}"


def test_stock_rules_and_features():
    rules = get_trading_rules("stock")
    assert rules["t_plus_1"] is True
    assert rules["price_limit"] is True
    assert rules["lot_size"] == 100
    for feature in ("dashboard", "daily_screener", "realtime_monitor", "backtest"):
        assert is_feature_enabled("stock", feature) is True


def test_crypto_rules_and_feature_boundaries():
    """crypto: T+0 / 无涨跌停 / 无整手; 回测已参数化开放, 相位机/分钟/梯队/财务仍关闭。"""
    rules = get_trading_rules("crypto")
    assert rules["t_plus_1"] is False
    assert rules["price_limit"] is False
    assert rules["lot_size"] == 0
    assert rules["sessions"] == "24x7"
    assert rules["volume_unit"] == "base_coin"
    for feature in ("dashboard", "daily_screener", "backtest", "news", "market_sentiment"):
        assert is_feature_enabled("crypto", feature) is True
    for feature in ("realtime_monitor", "minute_screener",
                    "limit_ladder", "sector_heat", "financial"):
        assert is_feature_enabled("crypto", feature) is False


# ================================================================
# fail-closed
# ================================================================

def test_unknown_asset_type_fails_closed():
    assert is_feature_enabled("unknown", "backtest") is False
    assert is_feature_enabled("unknown", "dashboard") is False
    with pytest.raises(ValueError):
        get_trading_rules("unknown")


def test_unknown_feature_fails_closed():
    assert is_feature_enabled("stock", "nonexistent_feature") is False
    assert is_feature_enabled("crypto", "nonexistent_feature") is False


def test_deny_reason_human_readable():
    reason = deny_reason("crypto", "realtime_monitor")
    assert reason and "加密货币" in reason
    assert deny_reason("crypto", "backtest") is None  # 已开放 → 无拒绝原因
    assert deny_reason("crypto", "dashboard") is None
    assert deny_reason("unknown", "backtest")  # 未知市场也给可读原因


# ================================================================
# payload 形状 (前端门控唯一来源)
# ================================================================

def test_market_rules_payload_shape():
    payload = market_rules_payload()
    markets = payload["markets"]
    assert set(markets) == set(known_asset_types())
    for rules in markets.values():
        assert isinstance(rules["label"], str) and rules["label"]
        assert set(rules["features"]) == set(FEATURE_KEYS)
        assert isinstance(rules["t_plus_1"], bool)
        assert isinstance(rules["price_limit"], bool)


# ================================================================
# 后端入口守卫
# ================================================================

def test_backtest_guard_allows_opened_markets():
    from app.api.backtest import _guard_backtest_asset_type

    _guard_backtest_asset_type("stock")  # 不抛
    _guard_backtest_asset_type("etf")    # 不抛
    _guard_backtest_asset_type("crypto")  # 撮合参数化后已开放, 不抛

    from app.services.trading_rules import deny_reason
    assert deny_reason("crypto", "backtest") is None


# ================================================================
# API 契约 (GET /api/markets/rules)
# ================================================================

def test_markets_rules_endpoint():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.markets import router

    client = TestClient(FastAPI())
    client.app.include_router(router)
    resp = client.get("/api/markets/rules")
    assert resp.status_code == 200
    payload = resp.json()
    assert "crypto" in payload["markets"]
    features = payload["markets"]["crypto"]["features"]
    assert features["dashboard"] is True
    assert features["backtest"] is True
    assert features["realtime_monitor"] is False
