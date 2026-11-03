"""外部资讯服务 (external_info) 测试。

只测解析与聚合逻辑, 不打真实网络: _fetch_text 由 monkeypatch 注入 fixture。
覆盖: RSS/Atom、新浪 7×24、东财快讯、恐惧贪婪指数的解析 (含结构异常隔离)、
多源聚合的 fail-soft 语义、TTL 缓存。
"""

from __future__ import annotations

import json

import pytest

from app.services import external_info

# ================================================================
# RSS / Atom 解析
# ================================================================

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>BTC breaks &lt;above&gt; resistance</title>
    <link>https://example.com/a</link>
    <description><p>Bitcoin rallied</p> on volume</description>
    <pubDate>Tue, 01 Oct 2026 08:00:00 GMT</pubDate>
  </item>
  <item>
    <title>   </title>
    <link>https://example.com/b</link>
  </item>
</channel></rss>"""

_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>ETH staking inflows rise</title>
    <link href="https://example.com/c" />
    <summary>Staking inflows up 12%</summary>
    <updated>2026-10-01T09:30:00Z</updated>
  </entry>
</feed>"""


def test_parse_rss():
    items = external_info.parse_rss(_RSS)
    assert len(items) == 1  # 空标题条目剔除
    it = items[0]
    assert it["title"] == "BTC breaks resistance"  # 实体解码后的 <above> 按标签剥离
    assert it["url"] == "https://example.com/a"
    assert it["summary"] == "Bitcoin rallied on volume"  # 标签剥离 + 空白压缩
    assert it["published_at"] and it["published_at"].startswith("2026-10-01")


def test_parse_atom():
    items = external_info.parse_rss(_ATOM)
    assert len(items) == 1
    assert items[0]["title"] == "ETH staking inflows rise"
    assert items[0]["url"] == "https://example.com/c"
    assert items[0]["published_at"].startswith("2026-10-01")


def test_parse_rss_invalid_xml_raises():
    with pytest.raises(ValueError):
        external_info.parse_rss("not xml at all <<")


# ================================================================
# 快讯 JSON 解析
# ================================================================

def test_parse_sina_zhibo():
    payload = {
        "result": {"data": {"feed": {"list": [
            {"rich_text": "央行开展 1000 亿元逆回购", "create_time": "2026-10-01 09:35:00", "docurl": ""},
            {"text": "三大指数集体低走", "create_time": "2026-10-01 09:40:00"},
            {"rich_text": ""},
        ]}}},
    }
    items = external_info.parse_sina_zhibo(json.dumps(payload, ensure_ascii=False))
    assert len(items) == 2
    assert items[0]["summary"] == "央行开展 1000 亿元逆回购"
    assert items[0]["published_at"].startswith("2026-10-01")
    assert items[1]["url"] is None


def test_parse_eastmoney_fast():
    payload = {"data": {"fastNewsList": [
        {"title": "国常会定调", "summary": "部署一揽子增量政策", "showTime": "2026-10-01 20:15"},
    ]}}
    items = external_info.parse_eastmoney_fast(json.dumps(payload, ensure_ascii=False))
    assert len(items) == 1
    assert items[0]["title"] == "国常会定调"
    assert items[0]["published_at"].startswith("2026-10-01")


# ================================================================
# 恐惧贪婪指数
# ================================================================

def test_parse_fng():
    payload = {"data": [
        {"value": "71", "value_classification": "Greed", "timestamp": "1727700000"},
        {"value": "25", "value_classification": "Extreme Fear", "timestamp": "1725000000"},
        {"value": "bad", "value_classification": "Fear", "timestamp": "1724900000"},  # 坏行剔除
    ]}
    data = external_info.parse_fng(json.dumps(payload))
    assert data["current"]["value"] == 71
    assert data["current"]["label"] == "贪婪"
    assert len(data["history"]) == 2
    # 最新在前
    assert data["history"][0]["value"] == 71


# ================================================================
# 聚合 fail-soft 与缓存
# ================================================================

@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    monkeypatch.setattr(external_info, "_news_cache", {})
    monkeypatch.setattr(external_info, "_fng_cache", None)
    monkeypatch.setattr(external_info, "_funding_cache", None)


def test_fetch_news_fail_soft_per_source(monkeypatch):
    """单源失败不影响其他源; 全部失败返回空列表 + errors。"""
    def fake_fetch(url: str) -> str:
        if "sina" in url:
            return json.dumps({
                "result": {"data": {"feed": {"list": [{"rich_text": "快讯A", "create_time": "2026-10-01 10:00:00"}]}}},
            }, ensure_ascii=False)
        raise RuntimeError("network down")

    monkeypatch.setattr(external_info, "_fetch_text", fake_fetch)
    result = external_info.fetch_news("stock")
    assert len(result["items"]) == 1
    assert result["items"][0]["source"].startswith("新浪")
    assert len(result["errors"]) == 1
    assert "东方财富" in result["errors"][0]

    monkeypatch.setattr(
        external_info, "_fetch_text", lambda url: (_ for _ in ()).throw(RuntimeError("all down")))
    result = external_info.fetch_news("stock", force=True)
    assert result["items"] == []
    assert len(result["errors"]) == 2


def test_fetch_news_cache_hit(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(url: str) -> str:
        calls["n"] += 1
        return json.dumps({
            "result": {"data": {"feed": {"list": [{"rich_text": "快讯B", "create_time": "2026-10-01 10:00:00"}]}}},
        }, ensure_ascii=False)

    monkeypatch.setattr(external_info, "_fetch_text", fake_fetch)
    first = external_info.fetch_news("crypto")
    second = external_info.fetch_news("crypto")
    assert calls["n"] == 2  # 首次拉取: crypto 两个源各一次; TTL 内第二次命中缓存
    assert first == second
    external_info.fetch_news("crypto", force=True)
    assert calls["n"] == 4  # force: 两个源各再拉一次


def test_fetch_fear_greed_fail_soft(monkeypatch):
    monkeypatch.setattr(
        external_info, "_fetch_text", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    data = external_info.fetch_fear_greed()
    assert data["current"] is None
    assert data["history"] == []
    assert data["error"]


def test_fetch_funding_rates_filters_universe(monkeypatch, tmp_path):
    """资金费率只保留白名单合约标的, 按 |费率| 降序。"""
    from app.config import settings as app_settings
    from app.services import crypto_universe

    class FakeProvider:
        def get_funding_rates(self):
            return [
                {"symbol": "BTCUSDT.P", "name": "BTC", "mark_price": 60000.0,
                 "funding_rate": 0.0001, "next_funding_time": None, "timestamp": 0},
                {"symbol": "ETHUSDT.P", "name": "ETH", "mark_price": 3000.0,
                 "funding_rate": -0.0005, "next_funding_time": None, "timestamp": 0},
                {"symbol": "ALTUSDT.P", "name": "ALT", "mark_price": 1.0,
                 "funding_rate": 0.02, "next_funding_time": None, "timestamp": 0},
            ]

    crypto_universe.save_universe(tmp_path, {"spot": ["BTCUSDT"], "futures": ["BTCUSDT", "ETHUSDT"]})
    monkeypatch.setattr(app_settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: FakeProvider())

    records = external_info.fetch_funding_rates()
    assert [r["symbol"] for r in records] == ["ETHUSDT.P", "BTCUSDT.P"]  # |费率| 降序, ALT 被过滤

    # provider 缺资金费率能力 → 空列表 (fail-soft)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: object())
    assert external_info.fetch_funding_rates(force=True) == []


def test_fetch_sentiment_stock_is_empty_state():
    data = external_info.fetch_sentiment("stock")
    assert data["fng"] is None and data["funding"] == []
    assert "情绪雷达" in data["note"]
