"""SEE-1604 D3：baseline 超时中止的 suggested_action hints 必须活过分片合并。

缺陷（QA D3）：串行回退/信封合并路径丢弃 `suggested_action` 与
`suggested_action_kind`——run_error=true 但文档承诺的机器可执行补救键缺失。
判据：serial chunk 的 run_error 结果经 `_merge_results` 后两键仍在。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation.merging import _merge_results


def test_serial_fallback_timeout_hints_survive_merge():
    chunk = {"file": "/proj/a.gd", "total_sites": 3}
    chunk_result = {
        "tool": "mutation",
        "ok": False,
        "summary": {
            "file": "/proj/a.gd",
            "run_error": True,
            "error": "baseline GUT timed out after 60s",
            "suggested_action": "raise --timeout past 60s",
            "suggested_action_kind": "raise_timeout",
        },
        "failures": [{"reason": "baseline GUT timed out after 60s"}],
    }
    merged = _merge_results([chunk], [chunk_result], 1)
    summary = merged["results"][0]["summary"]
    assert summary.get("suggested_action_kind") == "raise_timeout"
    assert summary.get("suggested_action")
    assert summary.get("run_error") is True
