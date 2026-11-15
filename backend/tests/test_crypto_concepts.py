"""crypto 概念板块 (DeFi/MEME/L1 等) 测试。

覆盖: 概念映射配置 (默认/清洗/损坏回退)、concept_rank 聚合 (排序/leader/
多概念成员口径)、trading_rules 门禁。
"""

from __future__ import annotations

import pytest

from app.services import crypto_concepts
from app.services.crypto_overview import _concept_rank

# ================================================================
# 概念映射配置
# ================================================================

def test_concepts_default_generated(tmp_path):
    concepts = crypto_concepts.load_concepts(tmp_path)
    assert "DeFi" in concepts and "MEME" in concepts and "L1" in concepts
    assert "BTCUSDT" in concepts["L1"]
    assert crypto_concepts.concepts_path(tmp_path).exists()


def test_concepts_roundtrip_cleaned(tmp_path):
    saved = crypto_concepts.save_concepts(tmp_path, {
        "DeFi": ["ethusdt", "ETHUSDT", "LINKUSDT"],
        "": ["BTCUSDT"],          # 空概念名剔除
        "MEME": ["DOGEUSDT", ""],  # 空成员剔除
    })
    assert saved == {"DeFi": ["ETHUSDT", "LINKUSDT"], "MEME": ["DOGEUSDT"]}


def test_concepts_corrupt_file_falls_back(tmp_path):
    path = crypto_concepts.concepts_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json{", encoding="utf-8")
    assert crypto_concepts.load_concepts(tmp_path) == crypto_concepts.default_concepts()


def test_concepts_empty_after_clean_falls_back(tmp_path):
    crypto_concepts.save_concepts(tmp_path, {"DeFi": []})
    assert crypto_concepts.load_concepts(tmp_path) == crypto_concepts.default_concepts()


def test_concept_of_symbol_reverse_mapping():
    rev = crypto_concepts.concept_of_symbol({"L1": ["BTCUSDT", "ETHUSDT"], "DeFi": ["ETHUSDT"]})
    assert rev == {"BTCUSDT": ["L1"], "ETHUSDT": ["L1", "DeFi"]}


# ================================================================
# concept_rank 聚合
# ================================================================

def _rows():
    return [
        {"symbol": "BTCUSDT", "name": "BTC", "change_pct": 0.01, "amount": 100.0},
        {"symbol": "ETHUSDT", "name": "ETH", "change_pct": 0.05, "amount": 200.0},
        {"symbol": "DOGEUSDT", "name": "DOGE", "change_pct": -0.10, "amount": 50.0},
    ]


def test_concept_rank_aggregation(tmp_path, monkeypatch):
    crypto_concepts.save_concepts(tmp_path, {
        "L1": ["BTCUSDT", "ETHUSDT"],
        "MEME": ["DOGEUSDT", "SOLUSDT"],  # SOLUSDT 不在 rows → 自然落空
    })
    monkeypatch.setattr("app.config.settings", type("S", (), {"data_dir": tmp_path})())

    rank = _concept_rank(_rows(), tmp_path)
    names = [i["name"] for i in rank["leading"]]
    assert names == ["L1", "MEME"]  # L1 均值 +3% > MEME 均值 -10%
    l1 = rank["leading"][0]
    assert l1["count"] == 2
    assert l1["avg_pct"] == pytest.approx(0.03)
    assert l1["up_count"] == 2 and l1["down_count"] == 0
    assert l1["leader"]["symbol"] == "ETHUSDT"
    assert l1["source_field"] is None  # crypto 无 ext 源, 静态行


def test_concept_rank_empty_rows_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings", type("S", (), {"data_dir": tmp_path})())
    assert _concept_rank([], tmp_path) == {"leading": [], "lagging": []}


def test_concept_rank_lagging_takes_bottom(tmp_path, monkeypatch):
    crypto_concepts.save_concepts(tmp_path, {
        "A": ["BTCUSDT"], "B": ["ETHUSDT"], "C": ["DOGEUSDT"],
        "D": ["DOGEUSDT"], "E": ["DOGEUSDT"], "F": ["DOGEUSDT"], "G": ["DOGEUSDT"],
    })
    monkeypatch.setattr("app.config.settings", type("S", (), {"data_dir": tmp_path})())
    rank = _concept_rank(_rows(), tmp_path)
    # leading 按 avg_pct 降序前 5, lagging 为其余按升序回排
    assert [i["name"] for i in rank["leading"]] == ["B", "A", "C", "D", "E"]  # B(+5%) > A(+1%)
    assert [i["name"] for i in rank["lagging"]] == ["C", "D", "E", "F", "G"]  # 升序前 5 (与 A 股同口径, 平局按插入序)


# ================================================================
# 多概念成员重复计数口径 (与 A 股多概念归属同口径)
# ================================================================

def test_multi_concept_member_counted_per_concept(tmp_path, monkeypatch):
    crypto_concepts.save_concepts(tmp_path, {"L1": ["ETHUSDT"], "DeFi": ["ETHUSDT"]})
    monkeypatch.setattr("app.config.settings", type("S", (), {"data_dir": tmp_path})())
    rank = _concept_rank([_rows()[1]], tmp_path)
    assert rank["leading"][0]["count"] == 1
    assert rank["leading"][1]["count"] == 1


# ================================================================
# trading_rules 门禁
# ================================================================

def test_crypto_sector_heat_enabled():
    from app.services.trading_rules import is_feature_enabled
    assert is_feature_enabled("crypto", "sector_heat") is True
