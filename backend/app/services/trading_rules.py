"""交易规则注册表 — 各资产类型「交易模式」的单一权威声明。

设计取向: 数据层(行情同步/指标管道/日线策略)按 asset_type 复用同一管道;
绑定交易规则语义的功能(实时监控相位机 / 分钟策略 / T+1 撮合回测 / 涨跌停
梯队 / 财务)能否对某市场开放, 由本注册表的 rules + features 声明:

  - 后端 API 层据 features 做 fail-closed 强制限制 (回测/监控入口守卫);
  - 前端门控统一消费 GET /api/markets/rules, 不自行硬编码能力清单
    (与 CONTRIBUTING §4 能力路由矩阵的"前端不硬编码"同一条不变量)。

回测边界注记 (CONTRIBUTING §5.3): 撮合约束已按市场参数化 (MatcherConfig
t_plus_1 / lot_size / price_limit, 由本注册表派生)。日线口径下各市场的入场
撮合统一为 open_t+1 (信号收盘后才产生, 当日收盘撮合当日信号构成未来函数);
T+0 市场 (crypto) 的差异仅在于: 当日买入持仓当日可卖 (当日风险退出放行)、
卖出后当日可再买; 无涨跌停市场跳过封板拒单, lot_size=0 表示任意精度数量。
`matching=close_t` 属用户自选的激进口径, 不改变上述默认。
"""

from __future__ import annotations

from typing import Any

# 功能键全集: 任何 asset_type 的 features 必须覆盖全部键 (缺失按 fail-closed 处理)
FEATURE_KEYS: tuple[str, ...] = (
    "dashboard",          # 主界面看板(市场总览)
    "daily_screener",     # 日线策略筛选
    "minute_screener",    # 分钟策略筛选
    "realtime_monitor",   # 实时监控(相位机/通知)
    "backtest",           # 回测/优化
    "limit_ladder",       # 涨停梯队/封单
    "sector_heat",        # 概念/行业热度
    "financial",          # 财务数据
    "news",               # 外部资讯(新闻快讯)
    "market_sentiment",   # 外部市场情绪指标(恐惧贪婪等)
)

_ASSET_LABELS = {"stock": "A股", "etf": "ETF", "crypto": "加密货币", "us": "美股"}

_TRADING_RULES: dict[str, dict[str, Any]] = {
    "stock": {
        "label": "A股",
        "t_plus_1": True,
        "price_limit": True,            # 涨跌停约束存在 (enriched 判定价列 + 撮合拒单)
        "lot_size": 100,                # 整手(股)
        "stamp_tax": True,
        "sessions": "cn_a_share",       # 北京时间 09:30-11:30 / 13:00-15:00
        "day_boundary": "beijing_calendar_day",
        "volume_unit": "share",         # 手(前端×100)/股口径
        "change_pct_window": "trading_day",
        "features": {
            "dashboard": True,
            "daily_screener": True,
            "minute_screener": True,
            "realtime_monitor": True,
            "backtest": True,
            "limit_ladder": True,
            "sector_heat": True,
            "financial": True,
            "news": True,
            "market_sentiment": True,
        },
    },
    "etf": {
        "label": "ETF",
        "t_plus_1": True,
        "price_limit": True,
        "lot_size": 100,
        "stamp_tax": False,
        "sessions": "cn_a_share",
        "day_boundary": "beijing_calendar_day",
        "volume_unit": "share",
        "change_pct_window": "trading_day",
        "features": {
            "dashboard": False,         # 总览看板为 A 股全市场语义, ETF 无独立看板
            "daily_screener": True,
            "minute_screener": True,
            "realtime_monitor": True,
            "backtest": True,
            "limit_ladder": False,
            "sector_heat": False,
            "financial": False,
            "news": True,
            "market_sentiment": True,
        },
    },
    "crypto": {
        "label": "加密货币",
        "t_plus_1": False,              # 7×24 T+0
        "price_limit": False,           # 无涨跌停
        "lot_size": 0,                  # 0 = 任意精度(无整手约束)
        "stamp_tax": False,
        "sessions": "24x7",
        "day_boundary": "utc_day",      # 日线以 UTC 00:00 开盘(= 北京 08:00 同日)
        "volume_unit": "base_coin",     # volume=基础币数量, amount=USDT 计价
        "change_pct_window": "24h_rolling",  # 快照为 24 小时滚动窗口口径
        "features": {
            "dashboard": True,
            "daily_screener": True,
            "minute_screener": False,
            "realtime_monitor": False,  # 相位机/交易时段为 A 股语义, 7×24 市场无相位
            "backtest": True,           # 撮合已参数化: T+0 当日可卖/可再买, 无涨跌停拒单, 任意精度
            "limit_ladder": False,      # 无涨跌停, 梯队/封单无意义
            "sector_heat": True,        # 概念热度 = 本地概念映射聚合 (DeFi/MEME/L1 等, 白名单范围)
            "financial": False,         # 无财报
            "news": True,
            "market_sentiment": True,   # 恐惧贪婪指数/资金费率
        },
    },
    "us": {
        "label": "美股",
        "t_plus_1": False,              # 日内回转合法 (PDT 规则属券商层, 引擎不建模)
        "price_limit": False,           # 无个股涨跌停 (熔断为市场级, 一期不建模)
        "lot_size": 1,                  # 1 股整股 (碎股/零股属券商层)
        "stamp_tax": False,
        "sessions": "us_regular",       # 美东 09:30-16:00 (DST 由 XNYS 日历处理)
        "day_boundary": "us_eastern_calendar_day",  # 日线 date=美东交易日
        "volume_unit": "share",         # volume=股 (非 A 股"手"), amount=美元
        "change_pct_window": "trading_day",
        "features": {
            "dashboard": True,
            "daily_screener": True,
            "minute_screener": False,   # 分钟策略引擎为 A 股语义
            "realtime_monitor": False,  # 相位机 A 股专用; 美股相位一期不建模
            "backtest": True,           # 撮合参数化复用: T+0 回转/无涨跌停/整股
            "limit_ladder": False,      # 无涨跌停
            "sector_heat": False,       # 行业口径一期不建模
            "financial": False,         # 东财美股无财务表
            "news": True,
            "market_sentiment": True,
        },
    },
}


def known_asset_types() -> tuple[str, ...]:
    """注册表声明了的资产类型。"""
    return tuple(_TRADING_RULES)


def get_trading_rules(asset_type: str) -> dict[str, Any]:
    """按资产类型取交易规则声明。未知类型抛 ValueError (fail-closed)。"""
    rules = _TRADING_RULES.get(asset_type)
    if rules is None:
        raise ValueError(f"未注册的资产类型: {asset_type!r} (已知: {', '.join(_TRADING_RULES)})")
    return rules


def is_feature_enabled(asset_type: str, feature: str) -> bool:
    """某市场是否开放某功能。未知市场/未知功能/声明缺失一律 False (fail-closed)。"""
    rules = _TRADING_RULES.get(asset_type)
    if rules is None or feature not in FEATURE_KEYS:
        return False
    return bool(rules["features"].get(feature, False))


def deny_reason(asset_type: str, feature: str) -> str | None:
    """功能未开放时的用户可读原因; 已开放返回 None。"""
    if is_feature_enabled(asset_type, feature):
        return None
    label = _TRADING_RULES.get(asset_type, {}).get("label") or asset_type
    reasons = {
        ("crypto", "realtime_monitor"): "实时监控依赖 A 股交易时段相位机, 加密货币 7×24 无相位口径",
        ("crypto", "minute_screener"): "分钟策略当前仅支持 A 股",
        ("us", "realtime_monitor"): "实时监控依赖 A 股交易时段相位机, 美股相位一期不建模",
        ("us", "minute_screener"): "分钟策略当前仅支持 A 股",
        ("us", "limit_ladder"): "美股无个股涨跌停, 涨停梯队/封单不适用",
        ("us", "sector_heat"): "美股行业热度口径一期不建模",
        ("us", "financial"): "美股数据源暂无财务报表",
        ("crypto", "limit_ladder"): "加密货币无涨跌停, 涨停梯队/封单不适用",
        ("crypto", "financial"): "加密货币无财务数据",
    }
    return reasons.get((asset_type, feature), f"{feature} 功能未对 {label} 开放")


def market_rules_payload() -> dict[str, Any]:
    """/api/markets/rules 响应: 全部市场的规则 + 功能可用性 (前端门控唯一来源)。"""
    return {
        "markets": {
            asset_type: {
                "label": rules["label"],
                "t_plus_1": rules["t_plus_1"],
                "price_limit": rules["price_limit"],
                "lot_size": rules["lot_size"],
                "sessions": rules["sessions"],
                "day_boundary": rules["day_boundary"],
                "volume_unit": rules["volume_unit"],
                "change_pct_window": rules["change_pct_window"],
                "features": dict(rules["features"]),
            }
            for asset_type, rules in _TRADING_RULES.items()
        }
    }
