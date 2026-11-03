"""市场(资产类型)元数据 API — 交易规则注册表的只读出口。

前端市场切换/组件门控统一读本接口 (GET /api/markets/rules),
不在组件里硬编码某市场支持哪些功能; 后端各功能入口按同一注册表
fail-closed 强制限制, 两端同源。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.services import trading_rules

router = APIRouter(prefix="/api/markets", tags=["markets"])


@router.get("/rules")
def get_market_rules():
    """全部资产类型的交易规则声明与功能可用性 (与后端强制限制同源)。"""
    return trading_rules.market_rules_payload()
