"""AI 策略模型 provenance — META 盖章与回测区间重叠警告。"""

from __future__ import annotations

import ast
import textwrap
from types import SimpleNamespace

from app.api.strategy import _stamp_meta_provenance
from app.strategy.ai_generator import AIStrategyGenerator, find_meta_assignment

_CODE = textwrap.dedent(
    '''
    import polars as pl

    def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
        return pl.col("close") > 0

    META = {
        "id": "custom_demo",
        "name": "demo",
        "description": "demo",
        "scoring": {"close": 1.0},
    }
    '''
)

_PROV_A = {
    "source": "ai", "provider": "deepseek", "model": "v4",
    "generated_at": "2026-01-01T08:00:00+00:00",
}
_PROV_B = {
    "source": "ai", "provider": "kimi", "model": "k2",
    "generated_at": "2026-02-01T08:00:00+00:00",
}


def _meta_of(code: str) -> dict:
    found = find_meta_assignment(code)
    assert found is not None
    return ast.literal_eval(found[1])


# ================================================================
# META 盖章
# ================================================================
def test_stamp_injects_provenance_into_meta():
    stamped = _stamp_meta_provenance(_CODE, _PROV_A)
    meta = _meta_of(stamped)
    assert meta["provenance"] == _PROV_A
    # 其余 META 键不变
    assert meta["id"] == "custom_demo"
    assert meta["scoring"] == {"close": 1.0}


def test_stamp_replaces_existing_provenance():
    stamped = _stamp_meta_provenance(_stamp_meta_provenance(_CODE, _PROV_A), _PROV_B)
    assert _meta_of(stamped)["provenance"] == _PROV_B


def test_stamped_code_still_passes_safety_and_meta_semantics():
    stamped = _stamp_meta_provenance(_CODE, _PROV_A)
    AIStrategyGenerator._validate_safety(stamped)
    AIStrategyGenerator._validate_meta_semantics(stamped, _meta_of(stamped))


# ================================================================
# evidence 留档: 回测区间越过生成时刻 → warning
# ================================================================
def _finish(tmp_path, meta: dict, end: str) -> dict:
    from app.backtest.evidence import EvidenceRecorder

    config = SimpleNamespace(
        start="2024-01-01", end=end, strategy_id="s1", asset_type="stock"
    )
    strategy = SimpleNamespace(
        composite=None, execution_backend="matrix_native", meta=meta, file_path=None
    )

    class _Engine:
        def get(self, strategy_id):
            raise ValueError

    return EvidenceRecorder(tmp_path, config, strategy, _Engine()).finish(
        SimpleNamespace(run_id="abc1234567", stats={}, trades=[]), {"end": end}
    )


def test_evidence_warns_when_backtest_exceeds_generated_at(tmp_path):
    manifest = _finish(tmp_path, {"provenance": _PROV_A}, end="2026-06-30")
    block = manifest["model_provenance"]
    assert block["status"] == "warning"
    assert block["provider"] == "deepseek"
    assert block["model"] == "v4"
    assert block["generated_at"] == "2026-01-01T08:00:00+00:00"
    assert block["backtest_end"] == "2026-06-30"


def test_evidence_ok_when_backtest_within_generated_window(tmp_path):
    manifest = _finish(tmp_path, {"provenance": _PROV_B}, end="2025-12-31")
    assert manifest["model_provenance"]["status"] == "ok"


def test_evidence_block_is_none_without_provenance(tmp_path):
    # 无 provenance 的历史策略: 键恒存在但为 None (与 validation 块取态一致), 消费方 .get() 等价
    manifest = _finish(tmp_path, {}, end="2026-06-30")
    assert manifest["model_provenance"] is None
