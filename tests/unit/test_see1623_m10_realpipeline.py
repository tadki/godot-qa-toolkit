"""SEE-1623 M10 真实管线行为钉 —— 收集计数与零执行证据门。

真实键管线：`_arm_plans` 为 gut_full 分片注入 `collected`（与
`_shard_groups` 同源 rglob 口径的 test_*.gd 脚本数），`_scan_shard_row`
回传该计数 + `stream`（任一非超时 rep 输出含 GUT 测试流证据）；
run_arm 的零执行证据门据真实值判定：

- 收集 0 脚本（tests 目录存在但零测试，Revy F3）→ 具名 invalid
  `empty_test_face`（不再以 `-gdir` 目录 token 冒充非空键集）；
- 收集 >0 但零测试流（引擎 rc=0 空输出）→ 具名 invalid
  `no_execution_evidence`；
- 健康收集且流证据在场 → valid，且臂文档披露
  `collected_scripts` / `execution_stream` 机器可审计。
"""

from __future__ import annotations

import subprocess as sp

import pytest

import test_see1594_s63_lazy_baseline as s63
from test_see1594_s63_lazy_baseline import (  # noqa: F401 — pytest fixture
    project,
)
from godot_qa_toolkit.measurement.arms import _scan_shard_row
from godot_qa_toolkit.measurement.orchestrator import run_measurement

_GREEN_STREAM = ("Godot Engine v4.4.1\n"
                 "res://tests/unit/test_game.gd\n"
                 "  * test_ok\n"
                 "\t\ttest_ok [Passed]\n"
                 "= Run Summary\nTotals: 1 passed\n")


class _FakeGodot:
    """可配置的 GUT 响应（rc, 输出文本）；非 GUT 调用透传真实实现。"""

    def __init__(self, rc=0, out=""):
        self.rc, self.out = rc, out
        self.real_run = sp.run

    def __call__(self, *a, **k):
        argv = list(a[0]) if a else []
        if "gut_cmdln.gd" not in " ".join(str(x) for x in argv):
            return self.real_run(*a, **k)

        class R:
            returncode = self.rc
            stdout = self.out
            stderr = ""
        return R()


def _run(project_root, monkeypatch, *, rc=0, out=""):
    monkeypatch.setattr(sp, "run", _FakeGodot(rc, out))
    return run_measurement(project_root, s63._targets(project_root),
                           s63._recipe(project_root, project_root.parent))


class TestCollectedCountIsArmEvidence:
    def test_scanner_surfaces_collected_and_stream(self):
        row = {"plan": {"args": ["-gdir=res://tests/"], "keys": None,
                        "collected": 3},
               "reps": [{"rc": 0, "out": _GREEN_STREAM, "timed_out": False}]}
        shard = _scan_shard_row(row)
        assert shard["collected"] == 3
        assert shard["stream"] is True
        # keys 保持既有语义（目录 token 仅作超时排除粒度）
        assert shard["keys"] == ["-gdir=res://tests/"]

    def test_scanner_stream_false_without_test_stream(self):
        row = {"plan": {"args": [], "keys": None, "collected": 1},
               "reps": [{"rc": 0, "out": "Godot Engine v4.4.1\n",
                         "timed_out": False}]}
        shard = _scan_shard_row(row)
        assert shard["collected"] == 1
        assert shard["stream"] is False

    def test_scanner_stream_true_with_ansi_prefixed_stream(self):
        """GUT 带色输出：测试流行以 ANSI SGR 前缀开头（真机形态）。"""
        colored = ("\x1b[4m\nres://tests/unit/test_game.gd\n"
                   "\x1b[0m* test_ok\n\x1b[1m1/1 passed.\n\x1b[0m")
        row = {"plan": {"args": ["-gdir=res://tests/"], "keys": None,
                        "collected": 1},
               "reps": [{"rc": 0, "out": colored, "timed_out": False}]}
        shard = _scan_shard_row(row)
        assert shard["stream"] is True


class TestZeroEvidenceGateRealPipeline:
    def test_empty_test_face_invalid_named(self, project, monkeypatch):
        """tests 目录存在但 0 测试（真实管线）→ empty_test_face。"""
        import pathlib
        for f in (pathlib.Path(project) / "tests" / "unit").glob("*.gd"):
            f.unlink()
        report = _run(project, monkeypatch, rc=0, out="Godot Engine\n")
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["invalid_reason"] == "empty_test_face"
        assert gut["collected_scripts"] == 0
        assert gut["execution_stream"] is False

    def test_zero_stream_invalid_named(self, project, monkeypatch):
        """收集 >0 但引擎 rc=0 零测试流 → no_execution_evidence。"""
        report = _run(project, monkeypatch, rc=0,
                      out="Godot Engine v4.4.1\n")
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["invalid_reason"] == "no_execution_evidence"
        assert gut["collected_scripts"] >= 1
        assert gut["execution_stream"] is False

    def test_healthy_stream_arm_valid_with_evidence(self, project,
                                                   monkeypatch):
        """健康收集 + 流证据在场 → valid 且证据键披露。"""
        report = _run(project, monkeypatch, rc=0, out=_GREEN_STREAM)
        gut = report["arms"][0]
        assert gut["state"] == "valid", gut["invalid_reason"]
        assert gut["collected_scripts"] >= 1
        assert gut["execution_stream"] is True
