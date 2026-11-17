"""价格口径声明契约 — daily 数据集 price_basis 与双重复权防护。

本仓库复权模型: provider 交 raw 日线 + adj_factor 数据集 → enriched 本地乘法
前复权。若自定义源实际交付的是已复权价 (声明 price_basis=adjusted), 又同时
声明 adj_factor 数据集, enriched 会再乘一次因子 → 双重复权。本契约把口径
变成数据边界上的显式声明, 组合在配置层 fail-closed。
"""

from __future__ import annotations

import pytest

from app.data_providers.base import ProviderCapabilities


# ================================================================
# Provider 契约字段
# ================================================================
def test_provider_capabilities_default_basis_is_raw():
    caps = ProviderCapabilities()
    assert caps.daily_price_basis == "raw"
    assert caps.daily is False  # 既有布尔位不受影响


def test_tickflow_declares_raw_basis():
    from app.data_providers.tickflow_provider import TickFlowProvider

    assert TickFlowProvider.capabilities.daily_price_basis == "raw"


# ================================================================
# DatasetConfig 声明与解析 (镜像 pct_unit 模式)
# ================================================================
def test_dataset_config_parses_price_basis():
    from app.data_providers.custom.config import _dataset_from_dict

    cfg = _dataset_from_dict({"url": "http://x", "price_basis": "adjusted"})
    assert cfg.price_basis == "adjusted"
    default = _dataset_from_dict({"url": "http://x"})
    assert default.price_basis is None


def test_dataset_config_rejects_unknown_basis():
    from app.data_providers.custom.config import _dataset_from_dict

    with pytest.raises(ValueError, match="raw"):
        _dataset_from_dict({"url": "http://x", "price_basis": "qfq"})


def test_config_from_dict_rejects_adjusted_with_adj_factor_combo():
    from app.data_providers.custom.config import config_from_dict

    with pytest.raises(ValueError, match="双重复权"):
        config_from_dict({
            "name": "x",
            "datasets": {
                "daily": {"url": "http://x", "price_basis": "adjusted"},
                "adj_factor": {"url": "http://y"},
            },
        })


def test_config_from_dict_allows_adjusted_without_adj_factor():
    from app.data_providers.custom.config import config_from_dict

    config = config_from_dict({
        "name": "x",
        "datasets": {"daily": {"url": "http://x", "price_basis": "adjusted"}},
    })
    assert config.datasets["daily"].price_basis == "adjusted"


# ================================================================
# provider.validate() 二层拦截 (程序化构造绕过 config_from_dict 的路径)
# ================================================================
def test_provider_validate_rejects_double_adjustment():
    from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
    from app.data_providers.custom.provider import GenericHTTPProvider

    config = CustomSourceConfig(
        name="x",
        display_name="X",
        datasets={
            "daily": DatasetConfig(url="http://x", price_basis="adjusted"),
            "adj_factor": DatasetConfig(url="http://y"),
        },
    )
    errors = GenericHTTPProvider(config).validate()
    assert any("双重复权" in error for error in errors)


def test_provider_validate_accepts_raw_with_adj_factor():
    from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
    from app.data_providers.custom.provider import GenericHTTPProvider

    config = CustomSourceConfig(
        name="x",
        display_name="X",
        datasets={
            "daily": DatasetConfig(url="http://x"),
            "adj_factor": DatasetConfig(url="http://y"),
        },
    )
    errors = [e for e in GenericHTTPProvider(config).validate() if "双重复权" in e]
    assert not errors


# ================================================================
# YAML sanitize 层 (loader) 与能力矩阵透出
# ================================================================
def test_sanitize_dataset_passes_price_basis_on_daily():
    from app.data_providers.custom.loader import _sanitize_dataset

    out = _sanitize_dataset("daily", {"url": "http://x", "price_basis": "adjusted"})
    assert out["price_basis"] == "adjusted"
    with pytest.raises(ValueError, match="仅用于 daily"):
        _sanitize_dataset("realtime", {"url": "http://x", "price_basis": "raw"})


def test_matrix_daily_entry_exposes_price_basis(monkeypatch):
    from app.data_providers import capabilities as caps

    fake = [{
        "name": "fuyao", "display": "fuyao",
        "datasets": {"daily", "adj_factor"},
        "asset_types": set(),
        "available": True, "status": "ok", "kind": "custom",
        "price_basis": "adjusted",
    }]
    monkeypatch.setattr(caps, "_declared_sources", lambda: fake)
    matrix = caps.build_capability_matrix({"daily_data_provider": "fuyao"}, tickflow_tier="none")
    daily = next(c for c in matrix["capabilities"] if c["id"] == "daily")
    entry = next(c for c in daily["candidates"] if c["name"] == "fuyao")
    assert entry["price_basis"] == "adjusted"
    assert entry["note"]  # 已复权候选附双重复权警示
