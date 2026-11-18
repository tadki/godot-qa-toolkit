"""BTIDY.1 特征化测试：`risk.shadow._report_document` JSON 契约钉死。

钉住键序、unknown 统计、ranking 排序/切片、routing 三计数、confidence、
negatives/files/records 透传。拆分后断言不变。
"""

from __future__ import annotations

from godot_qa_toolkit.risk.shadow import _report_document

KEYS = [
    "report", "version", "evidence_level", "diagnostic_only",
    "dynamic_cache", "metric", "metric_version", "unknown", "ranking",
    "core_full", "noise", "acceptance_cost", "routing", "confidence",
    "negatives", "files", "records",
]


def test_report_document_contract():
    ranking = [
        {"function_id": "a", "score_status": "scored",
         "crap_line": {"score": 40.0}, "coverage": 0.9, "cc": 3},
        {"function_id": "b", "score_status": "scored",
         "crap_line": {"score": 31.0}, "coverage": 0.4, "cc": 5},
        {"function_id": "c", "score_status": "unknown",
         "crap_line": {"score": None}, "coverage": None, "cc": 2},
    ]
    doc = _report_document(
        ranking=ranking, unknown=[{"file": "x", "reason": "stale"}],
        core_full=[{"file": "a"}], noise=[{"file": "b"}],
        negatives=[{"file": "neg"}], per_file=[{"file": "a"}],
        sample_files=2, negative_files=1)

    # key order 是 JSON 契约
    assert list(doc.keys()) == KEYS
    assert doc["report"] == "shadow-report"
    assert doc["version"] == "shadow/1"
    assert doc["metric"] == "crap_line" and doc["metric_version"] == "crap-line/1"
    # unknown 统计：count=非scored, total=全量, share=count/total
    assert doc["unknown"]["count"] == 1
    assert doc["unknown"]["total"] == 3
    assert doc["unknown"]["share"] == 1 / 3
    assert doc["unknown"]["reasons"] == [{"file": "x", "reason": "stale"}]
    # ranking 排序（score 降序）+ 仅 scored
    assert [r["function_id"] for r in doc["ranking"]["top"]] == ["a", "b"]
    assert doc["ranking"]["manual_named"] == []
    # routing 三计数
    assert doc["routing"] == {"tidy": 1, "harti_safety_net": 1,
                              "tool_chain_gap": 1 + 1}
    # confidence
    assert doc["confidence"] == {
        "sample_files": 2,
        "caveat": "2-3 files cannot be extrapolated to the whole repository",
        "negative_files": 1,
    }
    # 透传
    assert doc["negatives"] == [{"file": "neg"}]
    assert doc["files"] == [{"file": "a"}]
    assert doc["records"] is ranking
    assert doc["acceptance_cost"]["samples"] == []


def test_report_document_empty_ranking():
    doc = _report_document(ranking=[], unknown=[], core_full=[], noise=[],
                           negatives=[], per_file=[], sample_files=0,
                           negative_files=0)
    assert doc["unknown"]["share"] == 0.0
    assert doc["ranking"]["top"] == []
    assert doc["routing"] == {"tidy": 0, "harti_safety_net": 0,
                              "tool_chain_gap": 0}
    assert list(doc.keys()) == KEYS
