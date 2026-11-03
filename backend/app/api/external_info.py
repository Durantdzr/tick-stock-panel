"""外部资讯 API — 新闻快讯与外部市场情绪指标 (决策辅助, 只读)。

可用性由交易规则注册表声明 (news / market_sentiment feature),
未开放的市场返回 404 而非空数据, 与后端其他 fail-closed 限制同源。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services import external_info, trading_rules

router = APIRouter(prefix="/api/info", tags=["external-info"])


def _require_feature(asset_type: str, feature: str) -> None:
    if asset_type not in trading_rules.known_asset_types():
        raise HTTPException(status_code=422, detail=f"未知的资产类型: {asset_type!r}")
    if not trading_rules.is_feature_enabled(asset_type, feature):
        raise HTTPException(status_code=404, detail=trading_rules.deny_reason(asset_type, feature))


@router.get("/news")
def get_news(asset_type: str = "stock"):
    """某市场的财经新闻快讯 (多源聚合, 单源失败在 errors 中说明)。"""
    _require_feature(asset_type, "news")
    return external_info.fetch_news(asset_type)


@router.get("/sentiment")
def get_sentiment(asset_type: str = "crypto"):
    """外部市场情绪指标: crypto=恐惧贪婪指数 + 合约资金费率。"""
    _require_feature(asset_type, "market_sentiment")
    return external_info.fetch_sentiment(asset_type)
