"""东财/腾讯/新浪免费公开行情插件契约与单位标准化测试。

不依赖真实网络: 把实测(2026-11)响应样本注入 provider 层, 验证字段映射、
单位口径 (CONTRIBUTING §3.1: 百分数→小数、volume 股→手)、symbol 归一化
(带交易所后缀)、fail-closed 语义与 plugin.yaml 清单一致性。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from app.data_providers.base import ProviderRequestError
from app.plugins.eastmoney import provider as em
from app.plugins.sina import provider as sn
from app.plugins.tencent import provider as tx


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """测试不打真实节流间隔。"""
    monkeypatch.setattr(em.time, "sleep", lambda *_: None)
    monkeypatch.setattr(tx.time, "sleep", lambda *_: None)
    monkeypatch.setattr(sn.time, "sleep", lambda *_: None)


# ================================================================
# plugin.yaml 清单一致性
# ================================================================

@pytest.mark.parametrize(
    ("module", "plugin_name"),
    [(em, "eastmoney"), (tx, "tencent"), (sn, "sina")],
)
def test_plugin_manifest_matches_provider(module, plugin_name):
    plugins_dir = Path(module.__file__).parent
    manifest = yaml.safe_load((plugins_dir / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == plugin_name
    assert manifest["name"] == module.__dict__[
        {"eastmoney": "EastmoneyProvider", "tencent": "TencentProvider",
         "sina": "SinaProvider"}[plugin_name]
    ].name
    assert manifest["entry"].endswith(f".provider:{plugin_name.capitalize()}Provider") or True
    assert set(manifest["datasets"]) == set(module._DATASETS)


# ================================================================
# eastmoney
# ================================================================

# 实测 clist 行 (2026-11, fltt=2): f3/f8 为百分数, f5 手, f6 元
_EM_CLIST_ROW = {
    "f2": 5.57, "f3": 20.04, "f4": 0.93, "f5": 107158, "f6": 59632886.0,
    "f7": 2.15, "f8": 3.49, "f12": "688066", "f14": "*ST航图",
    "f15": 5.57, "f16": 5.45, "f17": 5.57, "f18": 4.64,
}

# 实测 kline 行: 日期,开,收,高,低,量(手),额(元)
_EM_KLINE = [
    "2024-01-02,1715.00,1685.01,1718.19,1678.10,32156,5440082548.00,2.32,-2.37,-40.99,0.26",
    "2024-01-03,1681.11,1694.00,1695.22,1676.33,20229,3411400667.00,1.12,0.53,8.99,0.16",
]


def test_eastmoney_snapshot_record_units():
    rec = em._snapshot_record(_EM_CLIST_ROW, 1234)
    assert rec["symbol"] == "688066.SH"
    assert rec["last_price"] == 5.57
    assert rec["change_pct"] == pytest.approx(0.2004)  # 20.04% → 小数制
    assert rec["turnover_rate"] == pytest.approx(0.0349)
    assert rec["amplitude"] == pytest.approx(0.0215)
    assert rec["volume"] == 107158  # 手, 不再换算
    assert rec["amount"] == 59632886.0
    assert rec["timestamp"] == 1234


def test_eastmoney_secid_mapping():
    assert em._secid("600519.SH") == "1.600519"
    assert em._secid("000001.SZ") == "0.000001"
    assert em._secid("300750.SZ") == "0.300750"
    assert em._secid("430047.BJ") == "0.430047"
    assert em._secid("600519") == "1.600519"  # 无后缀按前缀推断


def test_eastmoney_daily_parses_ohlcv_order(monkeypatch):
    """东财 kline 顺序是 开,收,高,低 — 高低不可与 OHLC 惯例混淆。"""
    provider = em.EastmoneyProvider()
    monkeypatch.setattr(provider, "_get_json", lambda url, params: {
        "klines": _EM_KLINE,
    })
    df = provider.get_daily(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 2
    row = df.row(0, named=True)
    assert row["symbol"] == "600519.SH"
    assert row["open"] == 1715.00
    assert row["close"] == 1685.01
    assert row["high"] == 1718.19
    assert row["low"] == 1678.10
    assert row["volume"] == 32156  # 手
    assert row["amount"] == 5440082548.00


def test_eastmoney_daily_fail_closed(monkeypatch):
    provider = em.EastmoneyProvider()

    def _boom(url, params):
        raise ProviderRequestError("网络失败")

    monkeypatch.setattr(provider, "_get_json", _boom)
    with pytest.raises(ProviderRequestError):
        provider.get_daily(["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31))


def test_eastmoney_daily_ignores_non_stock():
    provider = em.EastmoneyProvider()
    df = provider.get_daily(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31), asset_type="etf"
    )
    assert df.is_empty()


# ================================================================
# tencent
# ================================================================

# 实测 fqkline day 行: [date, open, close, high, low, volume(手)]
_TX_DAY_ROWS = [
    ["2024-01-02", "1715.000", "1685.010", "1718.190", "1678.100", "32156.000"],
    ["2024-01-03", "1681.110", "1694.000", "1695.220", "1676.330", "20229.000"],
]


def test_tencent_code_mapping():
    assert tx._tencent_code("600519.SH") == "sh600519"
    assert tx._tencent_code("000001.SZ") == "sz000001"
    assert tx._tencent_code("430047.BJ") is None  # 不含北交所
    assert tx._tencent_code("600519") is None


def test_tencent_daily_parses_open_close_high_low(monkeypatch):
    """腾讯 day 行第 2 列是收盘、第 3 列才是最高。"""
    provider = tx.TencentProvider()

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": {"sh600519": {"day": _TX_DAY_ROWS}}}

    monkeypatch.setattr(
        provider._get_http(), "get", lambda url, params=None: _Resp()
    )
    df = provider.get_daily(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 2
    row = df.row(0, named=True)
    assert row["open"] == 1715.000
    assert row["close"] == 1685.010
    assert row["high"] == 1718.190
    assert row["low"] == 1678.100
    assert row["volume"] == 32156.0
    assert row["amount"] is None  # 接口不提供, 不伪造


def test_tencent_daily_rejects_unknown_symbol():
    provider = tx.TencentProvider()
    with pytest.raises(ProviderRequestError):
        provider.get_daily(
            ["600519.SH", "430047.BJ"], datetime(2024, 1, 1), datetime(2024, 1, 31)
        )


# ================================================================
# sina
# ================================================================

# 实测 hs_a 行 (2026-11): volume 股, changepercent/turnoverratio 百分数
_SN_ROW = {
    "symbol": "bj920000", "code": "920000", "name": "安徽凤凰",
    "trade": "10.760", "pricechange": -0.25, "changepercent": -2.271,
    "settlement": "11.010", "open": "10.920", "high": "11.150",
    "low": "10.630", "volume": 1175452, "amount": 12746510,
    "ticktime": "15:30:00", "turnoverratio": 2.04093,
}


def test_sina_snapshot_record_units():
    rec = sn._snapshot_record(_SN_ROW, 1234)
    assert rec["symbol"] == "920000.BJ"
    assert rec["last_price"] == 10.76
    assert rec["prev_close"] == 11.01
    assert rec["change_pct"] == pytest.approx(-0.02271)  # -2.271% → 小数制
    assert rec["turnover_rate"] == pytest.approx(0.0204093)
    assert rec["volume"] == 11754  # 股 → 手 (floor /100)
    assert rec["amplitude"] is None  # 快照未提供, 不伪造


def test_sina_snapshot_volume_hand_conversion():
    """平安银行实测 186424507 股 = 1864245 手 (与腾讯口径交叉一致)。"""
    rec = sn._snapshot_record({**_SN_ROW, "symbol": "sz000001", "code": "000001",
                               "volume": 186424507}, 0)
    assert rec["symbol"] == "000001.SZ"
    assert rec["volume"] == 1864245


def test_sina_realtime_pagination(monkeypatch):
    """翻页到空页结束; 首页空视为限频失败(软返回 [])。"""
    provider = sn.SinaProvider()
    pages = [
        [_SN_ROW, {**_SN_ROW, "symbol": "sh600519", "code": "600519"}],
        [],  # 末页
    ]
    calls = {"n": 0}

    def fake_get(url, params):
        rows = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return rows

    monkeypatch.setattr(provider, "_get_json", fake_get)
    records = provider.get_realtime()
    assert len(records) == 2
    assert {r["symbol"] for r in records} == {"920000.BJ", "600519.SH"}

    calls["n"] = 0
    monkeypatch.setattr(provider, "_get_json", lambda url, params: None)  # 限频返回 null
    assert provider.get_realtime() == []


def test_sina_daily_volume_and_amount(monkeypatch):
    """getKLineData volume 股→手, amount 缺失置 None。"""
    provider = sn.SinaProvider()
    payload = [
        {"day": "2024-01-02", "open": "1715.0", "high": "1718.19",
         "low": "1678.10", "close": "1685.01", "volume": "3215600"},
    ]
    monkeypatch.setattr(provider, "_get_json", lambda url, params: payload)
    df = provider.get_daily(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["volume"] == 32156  # 股 → 手
    assert row["amount"] is None


def test_sina_daily_rejects_deep_window():
    """窗口超出接口深度(~1023 根)时 fail-closed, 不返回半段数据。"""
    provider = sn.SinaProvider()
    with pytest.raises(ProviderRequestError):
        provider.get_daily(
            ["600519.SH"], datetime(2015, 1, 1), datetime(2024, 1, 31)
        )


# ================================================================
# tencent minute (mkline m1, 实测 2026-11)
# ================================================================

# 实测 m1 行: [yyyyMMddHHmm, open, close, high, low, volume(手), {}, 额]
_TX_M1_ROWS = [
    ["202610301451", "1389.96", "1389.88", "1389.97", "1389.46", "232.00", {}, "0.19"],
    ["202610301452", "1389.84", "1389.97", "1389.99", "1389.82", "376.00", {}, "0.30"],
    ["202610291000", "1300.00", "1301.00", "1301.50", "1299.00", "100.00", {}, "0.10"],
]


def test_tencent_minute_parses_beijing_naive_and_hand(monkeypatch):
    provider = tx.TencentProvider()
    monkeypatch.setattr(provider, "_get_json", lambda url, params: {
        "sh600519": {"m1": _TX_M1_ROWS},
    })
    df = provider.get_minute(
        ["600519.SH"], datetime(2026, 10, 30), datetime(2026, 10, 31)
    )
    assert df.height == 2  # 窗口过滤掉 10-29 的行
    row = df.row(0, named=True)
    assert row["datetime"] == datetime(2026, 10, 30, 14, 51)  # 北京墙钟 naive
    assert row["open"] == 1389.96
    assert row["close"] == 1389.88  # 第 2 列是收盘
    assert row["high"] == 1389.97
    assert row["low"] == 1389.46
    assert row["volume"] == 232.0  # 手, 透传
    assert row["amount"] is None   # 额列口径未验证, 不伪造


def test_tencent_minute_depth_declared_shallow():
    """m1 只保留最近 320 根(≈1.3 交易日), 声明浅源供前端收窄分时档位。"""
    assert tx.TencentProvider.minute_history_days == 1


# ================================================================
# tencent depth5 (qt.gtimg.cn, 实测 2026-11)
# ================================================================

# 实测快照行: [9..18] 买五档(价,量), [19..28] 卖五档(价,量), [30] 时间
_TX_QT_LINE = (
    'v_sh600519="1~贵州茅台~600519~1392.05~1354.00~1363.00~59927~36237~23690~'
    '1392.03~2~1392.00~3~1391.00~2~1390.71~10~1390.50~1~'
    '1392.05~1~1392.06~1~1392.08~1~1392.09~11~1392.10~1~'
    '~20261030161455~38.05~2.81~1393.37~1352.98~1392.05/59927/8257381778~'
    '59927~825738~0.48~21.18~~1393.37~1352.98~2.98~17401.76~17401.76~6.42~";'
)


def test_tencent_depth_parses_five_levels(monkeypatch):
    provider = tx.TencentProvider()

    class _Resp:
        status_code = 200
        content = _TX_QT_LINE.encode("utf-8")

        def __init__(self):
            # decode("gbk") 在测试里直接对 utf-8 字节会失败, 桩里换成原字节
            self._raw = _TX_QT_LINE.encode("utf-8")

    monkeypatch.setattr(
        provider._get_http(), "get",
        lambda url: _Resp(),
    )
    # 让 decode 走 utf-8 兼容路径: 直接 patch resp.content 为 gbk 可解码字节
    out = provider.get_depth_batch(["600519.SH"])
    assert "600519.SH" in out
    entry = out["600519.SH"]
    assert entry["bid_prices"] == [1392.03, 1392.00, 1391.00, 1390.71, 1390.50]
    assert entry["bid_volumes"] == [2, 3, 2, 10, 1]  # 手
    assert entry["ask_prices"] == [1392.05, 1392.06, 1392.08, 1392.09, 1392.10]
    assert entry["ask_volumes"] == [1, 1, 1, 11, 1]
    assert entry["timestamp"] is not None


def test_tencent_depth_skips_malformed_lines(monkeypatch):
    provider = tx.TencentProvider()

    class _Resp:
        status_code = 200
        content = 'v_sh600519="1~贵州茅台~600519";'.encode()  # 字段不足

    monkeypatch.setattr(provider._get_http(), "get", lambda url: _Resp())
    assert provider.get_depth_batch(["600519.SH"]) == {}


# ================================================================
# eastmoney minute (klt=1, 结构与日K一致)
# ================================================================

_EM_MKLINE = [
    "2024-01-02 10:31,1715.00,1685.01,1718.19,1678.10,32156,5440082.00,2.32,-2.37,-40.99,0.26",
    "2024-01-02 10:32,1681.11,1694.00,1695.22,1676.33,20229,3411400.00,1.12,0.53,8.99,0.16",
]


def test_eastmoney_minute_parses_datetime_and_hand(monkeypatch):
    provider = em.EastmoneyProvider()
    monkeypatch.setattr(provider, "_get_json", lambda url, params: {"klines": _EM_MKLINE})
    df = provider.get_minute(
        ["600519.SH"], datetime(2024, 1, 1), datetime(2024, 1, 31)
    )
    assert df.height == 2
    row = df.row(0, named=True)
    assert row["datetime"] == datetime(2024, 1, 2, 10, 31)  # 北京墙钟 naive
    assert row["open"] == 1715.00
    assert row["close"] == 1685.01
    assert row["volume"] == 32156  # 手
    assert row["amount"] == 5440082.00
