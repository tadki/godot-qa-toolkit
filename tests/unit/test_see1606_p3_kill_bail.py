"""SEE-1606 §SPEC-P3：脚本粒度 kill 即退（路线 A worker 早停 / 路线 B cold
一次性直驱宿主）。

判据面（Owner 2026-11-19 裁决 A+B 均实施）：
- 开关：默认开启（对齐 Stryker disableBail=false）；`--no-kill-bail` /
  env `GQT_MUTATION_KILL_BAIL=0` 关断并具名归因。
- 选面：仅 gqt 自物化选择目录 / 单脚本 glob 允许顺序执行（脚本清单与
  gut_cmdln 收集逐字节一致）；显式目录/全脸具名禁用，绝不静默改道。
- 直驱宿主：进程数与现状一致（不拆进程），beacon L1-L3 契约逐字节保留，
  marker `GQT_KILL_BAIL scripts=k/n new_fail=<test>` 供机器判定。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import directdrive, killbail
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation import worker_channel as wc
from godot_qa_toolkit.mutation import worker_session

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


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
    return f"res://.gqt-attr-select/testsel/"


class TestSwitchResolution:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv(killbail.ENV_KILL_BAIL, raising=False)
        assert killbail.resolve_kill_bail(None) is True
        assert killbail.disabled_by(None) is None

    def test_flag_wins(self):
        assert killbail.resolve_kill_bail(False) is False
        assert killbail.disabled_by(False) == "flag"

    def test_env_off(self, monkeypatch):
        monkeypatch.setenv(killbail.ENV_KILL_BAIL, "0")
        assert killbail.resolve_kill_bail(None) is False
        assert killbail.disabled_by(None) == "env"

    def test_env_invalid_falls_back_on(self, monkeypatch):
        monkeypatch.setenv(killbail.ENV_KILL_BAIL, "yes-please")
        assert killbail.resolve_kill_bail(None) is True

    def test_cli_flag(self):
        args = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", "/p"])
        assert args.no_kill_bail is False
        args = build_parser().parse_args(
            ["mutation", "x.gd", "--project-root", "/p", "--no-kill-bail"])
        assert args.no_kill_bail is True


class TestFaceResolution:
    def test_selection_dir_enumerated_sorted(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        glob = _materialize_selection(proj, ["test_b.gd", "test_a.gd"])
        scripts, kind = killbail.face_scripts(str(proj), glob)
        assert kind == killbail.FACE_SELECTION_DIR
        assert scripts == ["res://.gqt-attr-select/testsel/test_a.gd",
                           "res://.gqt-attr-select/testsel/test_b.gd"]

    def test_single_script_face(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        scripts, kind = killbail.face_scripts(
            str(proj), "res://tests/test_a.gd")
        assert kind == killbail.FACE_SINGLE_SCRIPT
        assert scripts == ["res://tests/test_a.gd"]

    def test_explicit_dir_unsupported(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        scripts, kind = killbail.face_scripts(str(proj), "res://tests/")
        assert scripts is None
        assert kind == killbail.FACE_UNSUPPORTED

    def test_none_glob_unsupported(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        scripts, kind = killbail.face_scripts(str(proj), None)
        assert scripts is None
        assert kind == killbail.FACE_UNSUPPORTED

    def test_selection_outside_project_root_unsupported(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        scripts, kind = killbail.face_scripts(str(proj),
                                              "res://../elsewhere/sel/")
        assert scripts is None


class TestMarker:
    def test_parse(self):
        info = killbail.parse_bail_marker(
            "noise\nGQT_KILL_BAIL scripts=3/12 new_fail=test_x\nmore")
        assert info == {"scripts_run": 3, "scripts_total": 12,
                        "new_failure": "test_x"}

    def test_absent_is_none(self):
        assert killbail.parse_bail_marker("no marker here") is None


class TestColdDirectDecision:
    def test_enabled_selection_dir_with_gut(self, tmp_path):
        proj, _ = _write_project(tmp_path, with_gut=True)
        glob = _materialize_selection(proj, ["test_a.gd"])
        scripts, kind, gut_ok = mrunner._cold_direct_decision(
            True, str(proj), glob, dirty=False)
        assert scripts is not None and kind == "selection_dir"
        assert gut_ok is True

    def test_disabled_by_flag(self, tmp_path):
        proj, _ = _write_project(tmp_path, with_gut=True)
        glob = _materialize_selection(proj, ["test_a.gd"])
        # bail off + 干净基线 → 不启用直驱宿主（legacy 形态）
        scripts, kind, gut_ok = mrunner._cold_direct_decision(
            False, str(proj), glob, dirty=False)
        assert scripts is None and kind == "selection_dir"
        # bail off + 基线脏 → 仍启用（P2-R1 物理跳过必须在可控宿主）
        scripts, kind, gut_ok = mrunner._cold_direct_decision(
            False, str(proj), glob, dirty=True)
        assert scripts is not None and gut_ok is True

    def test_gut_addon_missing_named(self, tmp_path):
        proj, _ = _write_project(tmp_path, with_gut=False)
        glob = _materialize_selection(proj, ["test_a.gd"])
        scripts, kind, gut_ok = mrunner._cold_direct_decision(
            True, str(proj), glob, dirty=True)
        assert scripts is None and kind == "gut_addon_missing"
        assert gut_ok is False

    def test_explicit_dir_never_direct(self, tmp_path):
        proj, _ = _write_project(tmp_path, with_gut=True)
        scripts, kind, gut_ok = mrunner._cold_direct_decision(
            True, str(proj), "res://tests/", dirty=True)
        assert scripts is None
        assert kind == "unsupported_explicit_or_full"


class TestPlumbing:
    def test_api_default_is_none(self):
        for fn in (mrunner.run_mutation,):
            param = inspect.signature(fn).parameters["kill_bail"]
            assert param.default is None

    def test_summary_disclosure_selection_dir(self, tmp_path, monkeypatch):
        from godot_qa_toolkit.mutation.identity import COLD_SCHEME
        proj, target = _write_project(tmp_path, with_gut=True)
        glob = _materialize_selection(proj, ["test_a.gd"])
        # 钉冷通道：focus 是路线 B 的面判定/披露，不拉真 worker 会话。
        monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                            lambda *a, **k: (COLD_SCHEME, None))
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, "* test_a\n[passed]\n", 1.0))
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None: (0, "all passed"))
        r = mrunner.run_mutation(str(target), str(proj), budget=1,
                                 tests_glob=glob)
        s = r["summary"]
        assert s["kill_bail"] is True
        assert s["kill_bail_face"] == "selection_dir"
        assert s["kill_bail_scripts"] == 1
        assert s["kill_bail_disabled_by"] is None

    def test_summary_disclosure_env_off(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setenv(killbail.ENV_KILL_BAIL, "no")
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, "pass", 1.0))
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None: (0, "all passed"))
        r = mrunner.run_mutation(str(target), str(proj), budget=1,
                                 tests_glob="res://tests/")
        s = r["summary"]
        assert s["kill_bail"] is False
        assert s["kill_bail_disabled_by"] == "env"

    def test_bail_marker_attaches_to_verdict(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (0, "pass", 1.0))
        tail = ("* test_new\n[Failed]\n"
                "GQT_KILL_BAIL scripts=2/4 new_fail=test_new\n")
        monkeypatch.setattr(
            mrunner, "_run_gut_on_project",
            lambda root, timeout_s=600, tests_glob=None: (1, tail))
        r = mrunner.run_mutation(str(target), str(proj), budget=1,
                                 tests_glob="res://tests/")
        # killed 明细不进 failures[]（差分契约）——marker 披露经分类面直测。
        assert r["summary"]["killed"] >= 1
        from types import SimpleNamespace
        from godot_qa_toolkit.mutation import runhelp
        m = SimpleNamespace(file="calc.gd", line=2, column=1, kind="AOR",
                            operator="a+b→a-b", original="a + b",
                            mutated="a - b", stable_id="calc.gd#f:add#1")
        rec = runhelp._sm_classify(m, 1, tail, set(), False, "src",
                                   None, str(proj))
        assert rec["verdict"] == "killed"
        assert rec["kill_bail_info"] == {"scripts_run": 2,
                                         "scripts_total": 4,
                                         "new_failure": "test_new"}

    def test_worker_leg_req_carries_scripts(self, tmp_path, monkeypatch):
        """路线 A：kill_bail + 脚本清单在场 → run_leg 请求携带清单与基线名。"""
        captured: dict = {}

        class _Sess:
            dead = False
            dead_reason = ""
            canaries = 0

            def start(self):
                pass

            def close(self):
                pass

            def note_canary(self):
                self.canaries += 1

            def run_leg(self, leg, target_res, sha, closure, tests_glob,
                        tests_scripts=None, baseline_failures=None,
                        skip_red_tests=None, allow_tests=None):
                captured["scripts"] = tests_scripts
                captured["baseline"] = baseline_failures
                captured["skip_red"] = skip_red_tests
                return wc.WorkerLegResult(leg=leg, hash_ok=True, gut_ran=True,
                                          fail_count=0, failing_tests=set(),
                                          ok=True)

        mutants = [type("M", (), {"stable_id": "x#f#1"})()]
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: _Sess())
        pairs, fatals = wc.run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x", "project_root": "/tmp/x"})(),
            "calc.gd", "res://x.gd", (), "linux", "res://.gqt-attr-select/s/",
            inject_fn=lambda m: ("sha", None),
            cold_runner=lambda m, seq: {"verdict": "survived"},
            record_fn=lambda m, v, e=0.0: None,
            classify_fn=lambda m, leg: {"verdict": "survived"},
            leg_timeout_s=10, baseline_failures={"test_old"},
            kill_bail=True, tests_scripts=["res://.gqt-attr-select/s/a.gd"],
            red_pairs=[("res://.gqt-attr-select/s/a.gd", "test_old")])
        assert captured.get("scripts") == [
            "res://.gqt-attr-select/s/a.gd"], \
            f"captured={captured} pairs={pairs} fatals={fatals}"
        assert captured.get("baseline") == ["test_old"]
        assert captured.get("skip_red") == [
            ("res://.gqt-attr-select/s/a.gd", "test_old")]
        assert pairs

    def test_worker_leg_req_without_scripts(self, tmp_path, monkeypatch):
        """kill_bail 关断 → run_leg 请求不携带清单（既有形态逐字节）。"""
        captured: dict = {}

        class _Sess:
            dead = False
            dead_reason = ""
            canaries = 0

            def start(self):
                pass

            def close(self):
                pass

            def note_canary(self):
                self.canaries += 1

            def run_leg(self, leg, target_res, sha, closure, tests_glob,
                        tests_scripts=None, baseline_failures=None,
                        skip_red_tests=None, allow_tests=None):
                captured["scripts"] = tests_scripts
                captured["skip_red"] = skip_red_tests
                return wc.WorkerLegResult(leg=leg, hash_ok=True, gut_ran=True,
                                          fail_count=0, failing_tests=set(),
                                          ok=True)

        mutants = [type("M", (), {"stable_id": "x#f#1"})()]
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: _Sess())
        wc.run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x", "project_root": "/tmp/x"})(),
            "calc.gd", "res://x.gd", (), "linux", "res://tests/",
            inject_fn=lambda m: ("sha", None),
            cold_runner=lambda m, seq: {"verdict": "survived"},
            record_fn=lambda m, v, e=0.0: None,
            classify_fn=lambda m, leg: {"verdict": "survived"},
            leg_timeout_s=10, baseline_failures=set(),
            kill_bail=False, tests_scripts=["res://should-not-appear.gd"])
        assert captured.get("scripts") is None, \
            f"captured={captured}"
        assert captured.get("skip_red") == [], \
            f"skip_red={captured.get('skip_red')}"


class TestHostTemplates:
    """路线 A/B 宿主的静态契约（marker/beacon 契约逐字节钉住）。"""

    def test_shared_runner_is_identical_in_both_hosts(self):
        assert directdrive.SEQUENTIAL_RUNNER_GD in \
            worker_session.WORKER_HOST_GD
        assert directdrive.SEQUENTIAL_RUNNER_GD in \
            directdrive.COLD_DIRECT_GD_TEMPLATE

    def test_cold_host_keeps_beacon_contract(self):
        tpl = directdrive.COLD_DIRECT_GD_TEMPLATE
        assert "GQT_BEACON_FAIL" in tpl
        assert "hash mismatch" in tpl
        assert "can_instantiate" in tpl
        assert "not mutant-sourced" in tpl
        assert killbail.BAIL_MARKER in tpl
        assert killbail.DIRECT_SCRIPTS_MARKER in tpl
        assert "quit(1 if int(seq[\"fail\"]) > 0 else 0)" in tpl

    def test_worker_host_bails_on_new_failure_only(self):
        assert "baseline_failures" in worker_session.WORKER_HOST_GD
        assert "tests_scripts" in worker_session.WORKER_HOST_GD

    def test_direct_cfg_carries_scripts_and_baseline(self, tmp_path):
        path = directdrive.write_direct_cfg(
            str(tmp_path), "res://calc.gd", "sha", "Calc",
            ["res://.gqt-attr-select/s/a.gd"], {"test_b", "test_a"})
        import json
        cfg = json.loads(path.read_text())
        assert cfg["tests_scripts"] == ["res://.gqt-attr-select/s/a.gd"]
        assert cfg["baseline_failures"] == ["test_a", "test_b"]
        assert cfg["target_res"] == "res://calc.gd"
