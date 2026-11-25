"""SEE-1606 §SPEC-P2-R1：基线红测试物理跳过（test 粒度，collector 移除）。

判据：跳过 marker（`GQT_SKIP_RED marked=k requested=n names=…`）为「红测
试未被调用」的机器证据；跳过生效时空差集 = 真实存活（不落 suspect）；冷
通道无法物理跳过的面 + 基线脏 → 具名拒绝（不退回「跑了但不算」）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import json

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import directdrive, killbail
from godot_qa_toolkit.mutation import merging
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation import worker_channel as wc
from godot_qa_toolkit.mutation import worker_session
from godot_qa_toolkit.mutation import runhelp

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)
DIRTY_TAIL = "* test_red\n[Failed]\n"


def _write_project(tmp_path, with_gut=False):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    if with_gut:
        (proj / "addons" / "gut" / "gut.gd").write_text("extends Node\n")
    target = proj / "calc.gd"
    target.write_text(TARGET_SRC)
    return proj, target


def _materialize_selection(proj: Path, names: list[str]) -> str:
    sel_dir = proj / ".gqt-attr-select" / "testsel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(f'extends "res://tests/{n}"\n',
                                 encoding="utf-8")
    return "res://.gqt-attr-select/testsel/"


class TestMarker:
    def test_parse(self):
        info = killbail.parse_skip_red_marker(
            "noise\nGQT_SKIP_RED marked=2 requested=3 names=test_a,test_b\n")
        assert info == {"marked": 2, "requested": 3,
                        "names": ["test_a", "test_b"]}

    def test_absent_is_none(self):
        assert killbail.parse_skip_red_marker("nothing here") is None

    def test_zero_marked_no_names(self):
        info = killbail.parse_skip_red_marker(
            "GQT_SKIP_RED marked=0 requested=1 names=\n")
        assert info == {"marked": 0, "requested": 1, "names": []}


class TestClassifyCleanBatch:
    """跳过生效 → 空差集 = 真实存活（suspect 档抑制）。"""

    def _classify(self, tail, baseline):
        m = SimpleNamespace(file="calc.gd", line=2, column=1, kind="AOR",
                            operator="a+b→a-b", original="a + b",
                            mutated="a - b", stable_id="calc.gd#f:add#1")
        return runhelp._sm_classify(m, 1, tail, set(baseline), True,
                                    "src", None, "/p")

    def test_marker_suppresses_suspect(self):
        tail = ("* test_red\n[Risky]\n"
                "GQT_SKIP_RED marked=1 requested=1 names=test_red\n")
        rec = self._classify(tail, ["test_red"])
        assert rec["verdict"] == "survived"
        assert rec["skip_red"] == {"marked": 1, "requested": 1,
                                   "names": ["test_red"]}
        assert "physically skipped" in rec["reason"]

    def test_new_failure_after_skip_still_kills(self):
        tail = ("* test_new\n[Failed]\n"
                "GQT_SKIP_RED marked=1 requested=1 names=test_red\n")
        rec = self._classify(tail, ["test_red"])
        assert rec["verdict"] == "killed"
        assert rec["skip_red"]["marked"] == 1

    def test_no_marker_preexisting_red_survived(self):
        # SEE-1631 §SPEC-027/D7（QA 裁决）：无 skip marker 但失败 ⊆ 基线红
        # → survived（pre_existing_only）；skip_red 披露键仍缺席。
        rec = self._classify("* test_red\n[Failed]\n", ["test_red"])
        assert rec["verdict"] == "survived"
        assert rec.get("pre_existing_only") is True
        assert "skip_red" not in rec


class TestColdAbortOnUnsupportedFace:
    def _pin_cold(self, monkeypatch):
        # 两个解析点都钉冷：wc（_loop_slot/abort 判定）与 runner（_core_setup
        # route_scan 的 by-value import 名单）。
        from godot_qa_toolkit.mutation.identity import COLD_SCHEME
        monkeypatch.setattr(wc, "resolve_exec_scheme",
                            lambda *a, **k: (COLD_SCHEME, None))
        monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                            lambda *a, **k: (COLD_SCHEME, None))

    def test_dirty_unsupported_face_aborts_named(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, with_gut=False)
        self._pin_cold(monkeypatch)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, DIRTY_TAIL, 1.0))
        called = {"gut": 0}
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None: (
                called.__setitem__("gut", called["gut"] + 1), (0, ""))[1])
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")
        assert r["ok"] is False
        s = r["summary"]
        assert s.get("run_error") is True
        assert s.get("baseline_skip_unsupported") is True
        assert s.get("baseline_failures") == 1
        assert "cannot physically skip" in s.get("error", "")
        assert any("cannot physically skip" in f["reason"]
                   for f in r["failures"])
        assert called["gut"] == 0, "abort must run zero mutant legs"

    def test_clean_unsupported_face_runs_legacy(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, with_gut=False)
        self._pin_cold(monkeypatch)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, "all passed", 1.0))
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None:
            (0, "* test_x\n[passed]\n"))
        r = mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")
        # 干净基线走 legacy 形态：mutant 幸存 = ok 门禁失败（既有语义），
        # 关键断言是「未触发 P2-R1 拒绝、正常跑完 legs」。
        s = r["summary"]
        assert s.get("run_errors") == 0
        assert s.get("baseline_dirty") is False
        assert "baseline_skip_unsupported" not in s

    def test_dirty_supported_face_uses_host_without_bail(
            self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, with_gut=True)
        self._pin_cold(monkeypatch)
        glob = _materialize_selection(proj, ["test_a.gd"])
        captured = {"cfg": None}

        real_write = directdrive.write_direct_cfg

        def spy_write(*a, **k):
            path = real_write(*a, **k)
            # cfg 为临时文件（leg finally 即删）——写入即快照内容。
            captured["cfg"] = json.loads(Path(path).read_text())
            return path

        monkeypatch.setattr(directdrive, "write_direct_cfg", spy_write)
        # 基线尾需带脚本标题（D2：红身份=脚本+名）。
        baseline_tail = ("res://.gqt-attr-select/sel/test_a.gd\n"
                         "* test_red\n[Failed]\n")
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, baseline_tail, 1.0))
        tail = ("* test_green_new\n[Failed]\n"
                "GQT_SKIP_RED marked=1 requested=1 names=test_red\n")
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None: (1, tail))
        r = mrunner.run_mutation(str(target), str(proj), budget=1,
                                 tests_glob=glob)
        assert r["summary"].get("killed", 0) >= 1
        s = r["summary"]
        assert s.get("baseline_skip") == {"applied": True,
                                          "face": "selection_dir",
                                          "requested": 1}
        cfg = captured["cfg"]
        assert cfg["skip_red_tests"] == [["res://.gqt-attr-select/sel/test_a.gd", "test_red"]]
        assert cfg["bail_enabled"] is True


class TestWorkerSkip:
    def test_mutant_legs_carry_skip_names(self, tmp_path, monkeypatch):
        captured: dict = {}

        class _Sess:
            dead = False
            dead_reason = ""

            def start(self):
                pass

            def close(self):
                pass

            def note_canary(self):
                pass

            def run_leg(self, leg, target_res, sha, closure, tests_glob,
                        tests_scripts=None, baseline_failures=None,
                        skip_red_tests=None, allow_tests=None):
                captured.setdefault("legs", []).append(
                    {"leg": leg, "skip": skip_red_tests})
                return wc.WorkerLegResult(leg=leg, hash_ok=True, gut_ran=True,
                                          fail_count=0, failing_tests=set(),
                                          ok=True)

        mutants = [type("M", (), {"stable_id": f"x#f#{i}"})() for i in (1, 2)]
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: _Sess())
        wc.run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x",
                                       "project_root": "/tmp/x"})(),
            "calc.gd", "res://x.gd", (), "linux", "res://tests/",
            inject_fn=lambda m: ("sha", None),
            cold_runner=lambda m, seq: {"verdict": "survived"},
            record_fn=lambda m, v, e=0.0: None,
            classify_fn=lambda m, leg: {"verdict": "survived"},
            leg_timeout_s=10, baseline_failures={"test_red"},
            kill_bail=False, tests_scripts=None,
            red_pairs=[("res://.gqt-attr-select/sel/test_a.gd", "test_red")])
        # mutant 腿全带红名单（物理跳过，test 粒度）。
        assert captured["legs"], "no legs captured"
        assert all(entry["skip"] == [("res://.gqt-attr-select/sel/test_a.gd",
                                      "test_red")]
                   for entry in captured["legs"])


class TestMergeAndTemplate:
    def test_baseline_skip_passthrough(self):
        chunk = {"file": "/p/a.gd", "total_sites": 1}
        chunk_result = {
            "tool": "mutation", "ok": True,
            "summary": {"file": "/p/a.gd", "mutants": 1, "killed": 0,
                        "survived": 1, "suspect": 0, "timeout": 0,
                        "run_errors": 0, "invalid_mutants": 0,
                        "kill_rate": 0.0, "budget": 1,
                        "baseline_skip": {"applied": True,
                                          "face": "selection_dir",
                                          "requested": 2}},
            "failures": [],
        }
        merged = merging._merge_results([chunk], [chunk_result], 1)
        assert merged["results"][0]["summary"].get("baseline_skip") == {
            "applied": True, "face": "selection_dir", "requested": 2}

    def test_hosts_contain_skip_red_contract(self):
        assert killbail.SKIP_RED_MARKER in directdrive.COLD_DIRECT_GD_TEMPLATE
        assert killbail.SKIP_RED_MARKER in worker_session.WORKER_HOST_GD
        assert "skip_red_tests" in directdrive.COLD_DIRECT_GD_TEMPLATE
        assert "skip_red_tests" in worker_session.WORKER_HOST_GD

    def test_direct_cfg_skip_and_bail_fields(self, tmp_path):
        path = directdrive.write_direct_cfg(
            str(tmp_path), "res://calc.gd", "sha", "Calc",
            ["res://s/a.gd"], ["test_red"], bail_enabled=False,
            skip_red_tests=[("res://.gqt-attr-select/sel/test_a.gd", "test_red")])
        cfg = json.loads(Path(path).read_text())
        assert cfg["skip_red_tests"] == [["res://.gqt-attr-select/sel/test_a.gd", "test_red"]]
        assert cfg["bail_enabled"] is False
        assert cfg["bail_baseline_failures"] == ["test_red"]
