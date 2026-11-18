"""Baostock 插件契约测试。

不依赖真实网络: 用 2026-11 实测样本验证累积因子→单事件比值换算 (口径已在
端到端验证: 最后除权日后前复权偏差 0.0000%)、财务指标映射、股本表结构、
fail-closed 语义与 plugin.yaml 一致性。
"""

from __future__ import annotations

import sys
import types
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from app.data_providers.base import ProviderRequestError
from app.plugins.baostock import provider as bs
from app.plugins.baostock.provider import BaostockProvider

# ================================================================
# plugin.yaml 清单一致性
# ================================================================

def test_plugin_manifest_matches_provider():
    plugins_dir = Path(bs.__file__).parent
    manifest = yaml.safe_load((plugins_dir / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "baostock"
    assert BaostockProvider.name == "baostock"
    assert set(manifest["datasets"]) == {"adj_factor", "financial"}
    assert "api_key_env" not in manifest  # 匿名登录, 无 Key


# ================================================================
# 因子换算 (核心口径)
# ================================================================

def test_cumulative_to_event_factors_matches_calibration():
    """实测茅台累积链 → 单事件比值 (与 qfq/raw 逐日比完全吻合, 见模块 docstring)。

    期望值为实盘显示的 6 位舍入值, 容差放宽到 1e-4。
    """
    cums = [7.113689, 7.224927, 7.366525, 7.491968]
    events = bs.cumulative_to_event_factors(cums)
    assert events[0] == pytest.approx(7.113689)  # 首事件以 1.0 为基
    assert events[1] == pytest.approx(1.015637, abs=1e-4)
    assert events[2] == pytest.approx(1.019600, abs=1e-4)
    assert events[3] == pytest.approx(1.017028, abs=1e-4)


def test_cumulative_to_event_factors_rejects_nonpositive():
    with pytest.raises(ProviderRequestError):
        bs.cumulative_to_event_factors([1.0, 0.0])


def test_adj_factor_rejects_non_stock():
    p = BaostockProvider()
    df = p.get_adj_factors(["600519.SH"], None, None, "etf")
    assert df.is_empty()


def test_adj_factor_all_unsupported_fails_closed(monkeypatch):
    """北交所 baostock 不覆盖: 全部标的不可用时 fail-closed 抛错, 不冒充完整。"""
    p = BaostockProvider()
    with pytest.raises(ProviderRequestError):
        p.get_adj_factors(["430047.BJ"], None, None, "stock")


# ================================================================
# 财务映射 (实测 2026Q3 茅台样本)
# ================================================================

# 实测 query_profit_data 单行 (2026-11 拉取)
_PROFIT_ROW = {
    "code": "sh.600519", "pubDate": "2026-10-31", "statDate": "2026-09-30",
    "roeAvg": "0.24995", "npMargin": "0.509405", "gpMargin": "0.895811",
    "netProfit": "66717143218.04", "epsTTM": "65.724307",
    "MBRevenue": "89352478256.56", "totalShare": "1256197800.00",
    "liqaShare": "1256197800.00",
}


def test_metrics_field_map_covers_five_interfaces():
    """五类季度指标接口各至少映射一个 canonical 列。"""
    assert set(bs._METRICS_FIELD_MAP) == {"profit", "growth", "balance", "cash_flow", "operation"}
    assert bs._METRICS_FIELD_MAP["profit"]["roeAvg"] == "roe"
    assert bs._METRICS_FIELD_MAP["growth"]["YOYNI"] == "net_income_yoy"
    assert bs._METRICS_FIELD_MAP["balance"]["liabilityToAsset"] == "debt_to_asset_ratio"
    assert bs._METRICS_FIELD_MAP["cash_flow"]["CFOToOR"] == "operating_cash_to_revenue"
    assert bs._METRICS_FIELD_MAP["operation"]["INVTurnRatio"] == "inventory_turnover"


def test_profit_row_values_are_decimal_ratio():
    """小数制口径: 0.509405 = 50.94%, 原样透传不做百分比换算。"""
    assert bs._to_float(_PROFIT_ROW["npMargin"]) == pytest.approx(0.509405)
    assert bs._to_float(_PROFIT_ROW["netProfit"]) == pytest.approx(66717143218.04)


def test_financial_statements_return_empty_with_existing_data():
    """三大报表 baostock 无科目级数据 → 空表 (上游 merge 写保留存量)。"""
    p = BaostockProvider()
    for table in ("income", "balance_sheet", "cash_flow"):
        assert p.get_financials(table, ["600519.SH"]).is_empty()


def test_financial_unknown_table_returns_empty():
    p = BaostockProvider()
    assert p.get_financials("nonexistent", ["600519.SH"]).is_empty()


def test_recent_quarters_walks_back_across_year():
    p = BaostockProvider()
    quarters = p._recent_quarters(6)
    assert len(quarters) == 6
    # 严格倒序, 跨年连续无跳期
    for (y0, q0), (y1, q1) in pairwise(quarters):
        expected = (y0, q0 - 1) if q0 > 1 else (y0 - 1, 4)
        assert (y1, q1) == expected


# ================================================================
# symbol 映射
# ================================================================

def test_baostock_code_mapping():
    assert bs._baostock_code("600519.SH") == "sh.600519"
    assert bs._baostock_code("000001.SZ") == "sz.000001"
    assert bs._baostock_code("430047.BJ") is None
    assert bs._baostock_code("600519") is None


# ================================================================
# 试拉契约 (设置页 test_dataset)
# ================================================================

def test_test_dataset_rejects_unknown_dataset():
    out = BaostockProvider().test_dataset("minute", ["600519.SH"])
    assert out["rows"] == 0 and "error" in out
    assert "回退" in out["error"]


# ================================================================
# 会话失效自愈 (财务一直拉不到的根因: _logged_in 永久缓存, 服务端会话过期后全部查询静默失败)
# ================================================================

class _FakeRS:
    def __init__(self, error_code="0", error_msg="", fields=None, rows=None):
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = fields or []
        self._rows = rows or []
        self._i = 0

    def next(self):
        return self._i < len(self._rows)

    def get_row_data(self):
        row = self._rows[self._i]
        self._i += 1
        return row


def _install_fake_baostock(monkeypatch, login_calls, next_profit_rs):
    """替换 sys.modules['baostock'] 为受控桩; next_profit_rs(code,year,quarter) → RS。"""
    import sys
    import types

    fake = types.ModuleType("baostock")
    fake.login = lambda: (login_calls.append(1), _FakeRS(error_code="0"))[1]
    fake.logout = lambda: None
    for name in ("query_growth_data", "query_balance_data", "query_cash_flow_data", "query_operation_data"):
        setattr(fake, name, lambda code, year, quarter: _FakeRS(fields=[], rows=[]))
    fake.query_profit_data = next_profit_rs
    monkeypatch.setitem(sys.modules, "baostock", fake)
    return fake


def test_financial_metrics_relogin_on_session_expired(monkeypatch):
    """查询报「用户未登录」→ 重置登录标志 → 重登 → 同一查询重试一次。

    baostock 服务端会话有时效且单会话互踢; 常驻 worker 的 _logged_in 永久
    True 时, 过期后全部财务查询静默失败 (用户实测「财务一直获取不到」的根因)。
    """
    login_calls: list[int] = []
    profit_calls: list[tuple] = []
    profit_ok = _FakeRS(
        fields=["code", "pubDate", "statDate", "roeAvg"],
        rows=[["sh.600519", "2026-10-31", "2026-09-30", "0.24995"]],
    )

    def _next_profit_rs(code, year, quarter):
        profit_calls.append((code, year, quarter))
        return _FakeRS(error_code="10001", error_msg="用户未登录!") if len(profit_calls) == 1 else profit_ok

    fake = types.ModuleType("baostock")
    fake.login = lambda: (login_calls.append(1), _FakeRS(error_code="0"))[1]
    fake.logout = lambda: None
    for name in ("query_growth_data", "query_balance_data", "query_cash_flow_data", "query_operation_data"):
        setattr(fake, name, lambda code, year, quarter: _FakeRS(fields=[], rows=[]))
    fake.query_profit_data = _next_profit_rs
    monkeypatch.setitem(sys.modules, "baostock", fake)

    monkeypatch.setattr(bs, "_logged_in", True)  # 模拟常驻 worker 的过期登录态
    p = BaostockProvider()
    df = p._financial_metrics(["600519.SH"], latest_only=True)

    assert not df.is_empty(), "重试后应拿到数据"
    assert df.height == 1 and df["roe"][0] == pytest.approx(0.24995)
    assert len(login_calls) >= 1, "会话失效后必须重新登录"
    assert profit_calls.count(profit_calls[0]) == 2, "同一查询应恰好重试一次"


def test_financial_metrics_relogin_on_network_error(monkeypatch):
    """「网络接收错误」与用户未登录同源 (连接/会话失效), 同样触发重登重试。

    metrics 8 小时长跑后连接断开时, shares 同步曾整轮 0 行 (错误文案不同
    未被自愈匹配捕获)。
    """
    login_calls: list[int] = []
    profit_calls: list[tuple] = []
    profit_ok = _FakeRS(
        fields=["code", "pubDate", "statDate", "roeAvg", "totalShare"],
        rows=[["sh.600519", "2026-10-31", "2026-09-30", "0.24995", "1256197800.00"]],
    )

    def _next_profit_rs(code, year, quarter):
        profit_calls.append((code, year, quarter))
        return _FakeRS(error_code="10002", error_msg="网络接收错误。") if len(profit_calls) == 1 else profit_ok

    fake = types.ModuleType("baostock")
    fake.login = lambda: (login_calls.append(1), _FakeRS(error_code="0"))[1]
    fake.logout = lambda: None
    for name in ("query_growth_data", "query_balance_data", "query_cash_flow_data", "query_operation_data"):
        setattr(fake, name, lambda code, year, quarter: _FakeRS(fields=[], rows=[]))
    fake.query_profit_data = _next_profit_rs
    monkeypatch.setitem(sys.modules, "baostock", fake)

    monkeypatch.setattr(bs, "_logged_in", True)
    p = BaostockProvider()
    df = p._financial_metrics(["600519.SH"], latest_only=True)

    assert not df.is_empty(), "网络错误重试后应拿到数据"
    assert len(login_calls) >= 1, "网络接收错误必须重登"
    assert profit_calls.count(profit_calls[0]) == 2
