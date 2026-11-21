"""SEE-1606 §SPEC-H1 hardener — 判别力缺口补强（test-only，src 零改动）。

Round1 变异矩阵 28 存活点的补强判据，按缺口簇组织：
- wiring 透传（cli 派发/串行、executor 快路径/分片盖章、planning 收尾、
  merging chunk payload）——开关被静默丢弃必须变红；
- worker 宿主脏基线豁免（worker 通道不可具名拒绝）；
- D2 classify 脚本限定语义（wildcard 回声不豁免 / 裸名退化必须变红 /
  red map 精确脚本限定契约）；
- worker 日志脚本限定失败解析；
- face_scripts 排序确定性（glob 序不等于排序）与项目根边界（marker 在
  根外的选择目录仍须拒绝）；
- C=32 契约（Python 常量 ↔ GDScript 字面量逐位一致）；
- `_kill_bail_flag` 函数本体；
- `parse_failures_with_scripts`（Run Summary 通配归属、res:// 头须 .gd）；
- parity 腿与 worker 冷跑回退的脚本门。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli
from godot_qa_toolkit.mutation import directdrive, killbail
from godot_qa_toolkit.mutation import merging
from godot_qa_toolkit.mutation import executor as executor_mod
from godot_qa_toolkit.mutation import planning as planning_mod
from godot_qa_toolkit.mutation import reporting
from godot_qa_toolkit.mutation import gutrun
from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation import worker_channel as wc
from godot_qa_toolkit.mutation import worker_session
from godot_qa_toolkit.mutation.identity import COLD_SCHEME, WORKER_SCHEME

TARGET_SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)


def _mutation_args(extra: list[str]):
    from godot_qa_toolkit.cli import build_parser
    return build_parser().parse_args(
        ["mutation", "calc.gd", "--project-root", "/p", *extra])


def _mutant():
    return SimpleNamespace(file="calc.gd", line=2, column=1, kind="AOR",
                           operator="a+b→a-b", original="a + b",
                           mutated="a - b", stable_id="calc.gd#f:add#1",
                           start_pos=5, end_pos=10)


@pytest.fixture(autouse=True)
def _clear_baseline_red_map():
    """§SPEC-P2-R1-D3 同款清场：进程内 red map 不得跨测试泄漏。"""
    yield
    mrunner._BASELINE_RED_MAP = set()


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


def _pin_scheme(monkeypatch, scheme):
    monkeypatch.setattr(wc, "resolve_exec_scheme",
                        lambda *a, **k: (scheme, None))
    monkeypatch.setattr(mrunner, "resolve_exec_scheme",
                        lambda *a, **k: (scheme, None))


def _materialize_selection(proj: Path, names: list[str]) -> str:
    sel_dir = proj / ".gqt-attr-select" / "testsel"
    sel_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        (sel_dir / n).write_text(f'extends "res://tests/{n}"\n',
                                 encoding="utf-8")
    return "res://.gqt-attr-select/testsel/"


class TestWiringPassthrough:
    """开关从 CLI/编排层到 run_mutation 的透传——任何一环静默丢弃必须变红。"""

    def test_kill_bail_flag_fn(self):
        assert cli._kill_bail_flag(_mutation_args([])) is None
        assert cli._kill_bail_flag(
            _mutation_args(["--no-kill-bail"])) is False

    def test_cli_serial_passes_flags(self, monkeypatch):
        captured: dict = {}

        def spy(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(mrunner, "run_mutation", spy)
        args = _mutation_args(["--dry-run", "--break-on-baseline-failure",
                               "--no-kill-bail"])
        cli._run_mutation_serial(args, "linux")
        assert captured.get("break_on_baseline_failure") is True
        assert captured.get("kill_bail") is False

    def test_cli_serial_defaults(self, monkeypatch):
        captured: dict = {}

        def spy(**kwargs):
            captured.update(kwargs)
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(mrunner, "run_mutation", spy)
        cli._run_mutation_serial(_mutation_args(["--dry-run"]), "linux")
        assert captured.get("break_on_baseline_failure") is False
        assert captured.get("kill_bail") is None

    def test_cli_dispatch_passes_flags(self, monkeypatch):
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("spy-stop")

        monkeypatch.setattr(executor_mod, "run_mutation_files", spy)
        args = _mutation_args_files(["--break-on-baseline-failure"])
        with pytest.raises(RuntimeError, match="spy-stop"):
            cli._dispatch_mutation(args, "linux")
        assert captured.get("break_on_baseline_failure") is True
        assert captured.get("kill_bail") is None

    def test_cli_dispatch_passes_no_kill_bail(self, monkeypatch):
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("spy-stop")

        monkeypatch.setattr(executor_mod, "run_mutation_files", spy)
        with pytest.raises(RuntimeError, match="spy-stop"):
            cli._dispatch_mutation(
                _mutation_args_files(["--no-kill-bail"]), "linux")
        assert captured.get("kill_bail") is False

    def test_serial_fast_path_passes_flags(self, monkeypatch):
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(executor_mod, "run_mutation", spy)
        executor_mod._serial_fast_path(
            "/p/calc.gd", "/p", 1, 60, False, None, "linux", None, None,
            None, False, None, None, "full",
            break_on_baseline_failure=True, kill_bail=False)
        assert captured.get("break_on_baseline_failure") is True
        assert captured.get("kill_bail") is False

    def test_build_chunks_stamps_flags(self, monkeypatch):
        chunk = {"file": "/p/calc.gd", "total_sites": 1}

        def fake_plan(*a, **k):
            return [chunk], None, None

        monkeypatch.setattr(executor_mod, "_plan_file_chunks", fake_plan)
        chunks, _settled, _ctx = executor_mod._build_chunks(
            ["/p/calc.gd"], "/p", 1, 60, None, 1,
            break_on_baseline_failure=True, kill_bail=False)
        assert chunks
        assert chunks[0]["break_on_baseline_failure"] is True
        assert chunks[0]["kill_bail"] is False

    def test_plan_serial_finish_passes_flags(self, monkeypatch):
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(executor_mod, "run_mutation", spy)
        planning_mod._plan_serial_finish(
            "/p/calc.gd", "/p", 0, 60, None, "linux", None, False,
            {"rc": 0, "tail": "", "elapsed": 1.0}, "run-1", False, None,
            None, "full", None, True, False)
        assert captured.get("break_on_baseline_failure") is True
        assert captured.get("kill_bail") is False

    def test_chunk_worker_passes_flags(self, monkeypatch):
        captured: dict = {}

        def spy(*args, **kwargs):
            captured.update(kwargs)
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(executor_mod, "run_mutation", spy)
        payload = {"serial": True, "file": "/p/calc.gd",
                   "project_root": "/p", "budget": 0, "timeout_s": 60,
                   "tests_glob": None, "dry_run": False,
                   "binary_kind": "linux", "timing_store": None,
                   "mutation_warning": None, "timeout_factor": None,
                   "mutate_all": False, "reuse_coverage": False,
                   "identity": None, "worker_channel": None,
                   "selection_mode": "full", "selection_meta": None,
                   "break_on_baseline_failure": True, "kill_bail": False}
        merging._chunk_worker(payload)
        assert captured.get("break_on_baseline_failure") is True
        assert captured.get("kill_bail") is False


def _mutation_args_files(extra: list[str]):
    from godot_qa_toolkit.cli import build_parser
    return build_parser().parse_args(
        ["mutation", "a.gd", "b.gd", "--project-root", "/p", *extra])


class TestWorkerHostDirtyExemption:
    """worker 通道 = 可控宿主：脏基线 + 冷不可跳过面 → 不得具名拒绝
    （拒绝仅限冷通道）。用 _loop_slot 哨兵隔离判定点本身：哨兵被触达
    = 未发生 abort。"""

    def test_dirty_worker_unsupported_face_does_not_abort(
            self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, with_gut=True)
        _pin_scheme(monkeypatch, WORKER_SCHEME)
        monkeypatch.setattr(
            mrunner, "_run_baseline",
            lambda root, tests_glob, timeout_s:
            (1, "res://tests/x/test_red.gd\n* test_red\n[Failed]\n", 1.0))

        def slot_sentinel(*a, **k):
            raise RuntimeError("slot-sentinel-reached")

        monkeypatch.setattr(mrunner, "_loop_slot", slot_sentinel)
        with pytest.raises(RuntimeError, match="slot-sentinel-reached"):
            mrunner.run_mutation(str(target), str(proj), budget=2,
                                 tests_glob="res://tests/")


class TestClassifyScriptQualified:
    """D2 classify：wildcard 回声不豁免、同名绿脚本 killer 照杀、
    脚本限定 red map 精确豁免。"""

    def test_wildcard_summary_echo_not_new(self, monkeypatch):
        monkeypatch.setattr(mrunner, "_BASELINE_RED_MAP",
                            {("res://s/a.gd", "test_x")})
        tail = ("res://s/a.gd\n* test_x\n[Failed]\n"
                "---- Run Summary ----\n- test_x\n  [Failed]: echo\n")
        rec = reporting._classify_mutant_run(_mutant(), 1, tail,
                                             {"test_x"}, True)
        assert rec["verdict"] != "killed", rec

    def test_same_name_other_script_still_kills(self, monkeypatch):
        monkeypatch.setattr(mrunner, "_BASELINE_RED_MAP",
                            {("res://s/a.gd", "test_x")})
        tail = "res://s/b.gd\n* test_x\n[Failed]\n"
        rec = reporting._classify_mutant_run(_mutant(), 1, tail,
                                             {"test_x"}, True)
        assert rec["verdict"] == "killed", rec

    def test_exact_script_match_exempted(self, monkeypatch):
        monkeypatch.setattr(mrunner, "_BASELINE_RED_MAP",
                            {("res://s/a.gd", "test_x")})
        tail = "res://s/a.gd\n* test_x\n[Failed]\n"
        rec = reporting._classify_mutant_run(_mutant(), 1, tail,
                                             {"test_x"}, True)
        assert rec["verdict"] != "killed", rec

    def test_no_headers_falls_back_bare(self, monkeypatch):
        monkeypatch.setattr(mrunner, "_BASELINE_RED_MAP", set())
        rec = reporting._classify_mutant_run(_mutant(), 1,
                                             "* test_new\n[Failed]\n",
                                             set(), True)
        assert rec["verdict"] == "killed", rec


class TestBaselineRedMapContract:
    """red map 契约：仅脚本限定的红身份（脚本头在场、非通配）入集。"""

    def test_map_is_script_qualified_exact(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path, with_gut=True)
        _pin_scheme(monkeypatch, COLD_SCHEME)
        glob = _materialize_selection(proj, ["test_a.gd"])
        baseline_tail = ("res://.gqt-attr-select/sel/a.gd\n"
                         "* test_red\n[Failed]\n"
                         "---- Run Summary ----\n- test_red\n"
                         "  [Failed]: echo\n")
        monkeypatch.setattr(mrunner, "_run_baseline",
                            lambda root, tests_glob, timeout_s:
                            (1, baseline_tail, 1.0))
        monkeypatch.setattr(mrunner, "_run_gut_on_project",
                            lambda root, timeout_s=600, tests_glob=None:
                            (0, "all passed"))
        mrunner.run_mutation(str(target), str(proj), budget=1,
                             tests_glob=glob)
        assert mrunner._BASELINE_RED_MAP == {
            ("res://.gqt-attr-select/sel/a.gd", "test_red")}, \
            f"red map drift: {mrunner._BASELINE_RED_MAP}"


class TestLegFailingScripts:
    """worker 日志分段 → 脚本限定失败集（D2 worker 侧身份源）。"""

    def _session_with_log(self, tmp_path, body: str):
        log = tmp_path / "worker.log"
        log.write_text(body, encoding="utf-8")
        s = worker_session.WorkerSession.__new__(worker_session.WorkerSession)
        s._log_path = log
        return s

    def test_segment_script_qualified(self, tmp_path):
        body = ("noise\n"
                "GQT_WORKER_LEG_BEGIN 3\n"
                "res://sel/a.gd\n* test_x\n[Failed]: x\n"
                "GQT_WORKER_LEG_END 3 \n"
                "GQT_WORKER_LEG_BEGIN 4\n"
                "res://sel/b.gd\n* test_y\n[Failed]\n"
                "GQT_WORKER_LEG_END 4 \n")
        s = self._session_with_log(tmp_path, body)
        assert s._leg_failing_scripts(3) == {("res://sel/a.gd", "test_x")}
        assert s._leg_failing_scripts(4) == {("res://sel/b.gd", "test_y")}
        assert s._leg_failing_scripts(9) == set()

    def test_missing_log_is_empty(self, tmp_path):
        s = worker_session.WorkerSession.__new__(worker_session.WorkerSession)
        s._log_path = tmp_path / "absent.log"
        assert s._leg_failing_scripts(1) == set()


class TestFaceScriptsOrderAndBoundary:
    """选面清单确定性排序（glob 返回序 ≠ 排序）与项目根边界。"""

    def test_selection_dir_sorted_deterministic(self, tmp_path, monkeypatch):
        import pathlib
        proj, _target = _write_project(tmp_path)
        sel_dir = proj / ".gqt-attr-select" / "testsel"
        sel_dir.mkdir(parents=True)
        for n in ("test_b.gd", "test_a.gd"):
            (sel_dir / n).write_text(f'extends "res://tests/{n}"\n',
                                     encoding="utf-8")
        original_glob = pathlib.Path.glob

        def anti_sorted_glob(self, pattern):
            if str(self).endswith("testsel") and pattern == "*.gd":
                return [self / "test_b.gd", self / "test_a.gd"]
            return original_glob(self, pattern)

        monkeypatch.setattr(pathlib.Path, "glob", anti_sorted_glob)
        scripts, kind = killbail.face_scripts(
            str(proj), "res://.gqt-attr-select/testsel/")
        assert kind == killbail.FACE_SELECTION_DIR
        assert scripts == [
            "res://.gqt-attr-select/testsel/test_a.gd",
            "res://.gqt-attr-select/testsel/test_b.gd"], \
            f"selection scripts must be sorted: {scripts}"

    def test_marker_outside_project_root_rejected(self, tmp_path):
        proj, _target = _write_project(tmp_path)
        outside = tmp_path / "elsewhere" / ".gqt-attr-select" / "sel"
        outside.mkdir(parents=True)
        (outside / "test_a.gd").write_text('extends "res://tests/x.gd"\n',
                                           encoding="utf-8")
        scripts, kind = killbail.face_scripts(
            str(proj), "res://../elsewhere/.gqt-attr-select/sel/")
        assert scripts is None
        assert kind == killbail.FACE_UNSUPPORTED


class TestKillBailChunkContract:
    """C=32 口径逐位一致：Python 常量 ↔ 顺序执行宿主字面量（单一出处）。"""

    def test_chunk_constant_is_32(self):
        assert killbail.KILL_BAIL_CHUNK == 32

    def test_host_chunk_default_matches_python_constant(self):
        # SEE-1615 §SPEC-PERF1：chunk 由字面量改为调用方注入（cfg/req），
        # 契约变为「宿主缺省回退 == Python 默认常量」+ 原语接形参。
        assert killbail.DEFAULT_BAIL_CHUNK == killbail.KILL_BAIL_CHUNK
        assert f'int(cfg.get("bail_chunk", {killbail.DEFAULT_BAIL_CHUNK}))' \
            in directdrive.COLD_DIRECT_GD_TEMPLATE, \
            "GDScript chunk default diverged from DEFAULT_BAIL_CHUNK"
        assert "chunk: int) -> Dictionary:" in directdrive.SEQUENTIAL_RUNNER_GD


class TestParserScriptQualified:
    """parse_failures_with_scripts：Run Summary 通配归属、res:// 头须 .gd。"""

    def test_summary_echo_attributed_wildcard(self):
        out = ("res://sel/a.gd\n* test_t1\n[Failed]: x\n"
               "res://sel/b.gd\n"
               "---- Run Summary ----\n- test_t1\n  [Failed]: echo\n")
        got = gutrun.parse_failures_with_scripts(out)
        assert ("*", "test_t1") in got, got
        assert ("res://sel/a.gd", "test_t1") in got, got
        assert ("res://sel/b.gd", "test_t1") not in got, got

    def test_res_header_requires_gd(self):
        out = ("res://sel/a.gd\n* test_t1\n[Failed]\n"
               "res://icon.svg.import\n* test_t2\n[Failed]\n")
        got = gutrun.parse_failures_with_scripts(out)
        assert ("res://sel/a.gd", "test_t2") in got, got
        assert ("res://icon.svg.import", "test_t2") not in got, got

    def test_two_scripts_qualified(self):
        out = ("res://sel/a.gd\n* test_x\n[Failed]\n"
               "res://sel/b.gd\n* test_y\n[Failed]\n")
        got = gutrun.parse_failures_with_scripts(out)
        assert got == {("res://sel/a.gd", "test_x"),
                       ("res://sel/b.gd", "test_y")}, got


class TestWorkerParityAndColdGate:
    """worker 腿脚本门：parity 腿与冷跑回退仅在 kill_bail（或脏基线）时
    携带脚本清单。"""

    def _fake_session(self):
        captured: dict = {}

        class _Sess:
            dead = False
            dead_reason = ""
            canaries = 0
            boot_s = 0.1

            def start(self):
                pass

            def close(self):
                pass

            def note_canary(self):
                self.canaries += 1

            def run_leg(self, leg, target_res, sha, closure, tests_glob,
                        tests_scripts=None, baseline_failures=None,
                        skip_red_tests=None, allow_tests=None):
                captured.setdefault("legs", []).append(
                    {"leg": leg, "scripts": tests_scripts,
                     "baseline": baseline_failures, "skip": skip_red_tests})
                return wc.WorkerLegResult(
                    leg=leg, hash_ok=True, gut_ran=True, fail_count=0,
                    failing_tests=set(baseline_failures or ()), ok=True)

        return _Sess(), captured

    def test_parity_leg_scripts_gate(self, monkeypatch):
        sess, captured = self._fake_session()
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: sess)
        mutants = [_mutant()]
        wc.run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x",
                                       "project_root": "/tmp/x"})(),
            "calc.gd", "res://x.gd", (), "linux",
            "res://.gqt-attr-select/s/",
            inject_fn=lambda m: ("sha", None),
            cold_runner=lambda m, seq: {"verdict": "survived"},
            record_fn=lambda m, v, e=0.0: None,
            classify_fn=lambda m, leg: {"verdict": "survived"},
            leg_timeout_s=10, baseline_failures=set(),
            parity_inject_fn=lambda: ("parity-sha", None),
            kill_bail=False, tests_scripts=["res://s/a.gd"], red_pairs=[])
        parity = [e for e in captured["legs"] if e["leg"] == -1]
        assert parity and parity[0]["scripts"] is None, captured

    def test_parity_leg_scripts_when_bail_on(self, monkeypatch):
        sess, captured = self._fake_session()
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: sess)
        mutants = [_mutant()]
        wc.run_worker_legs(
            mutants, type("Slot", (), {"root": "/tmp/x",
                                       "project_root": "/tmp/x"})(),
            "calc.gd", "res://x.gd", (), "linux",
            "res://.gqt-attr-select/s/",
            inject_fn=lambda m: ("sha", None),
            cold_runner=lambda m, seq: {"verdict": "survived"},
            record_fn=lambda m, v, e=0.0: None,
            classify_fn=lambda m, leg: {"verdict": "survived"},
            leg_timeout_s=10, baseline_failures={"t_old"},
            parity_inject_fn=lambda: ("parity-sha", None),
            kill_bail=True, tests_scripts=["res://s/a.gd"],
            red_pairs=[("res://s/a.gd", "t_old")])
        parity = [e for e in captured["legs"] if e["leg"] == -1]
        assert parity and parity[0]["scripts"] == ["res://s/a.gd"], captured
        mutant_legs = [e for e in captured["legs"] if e["leg"] >= 0]
        assert mutant_legs and mutant_legs[0]["skip"] == [
            ("res://s/a.gd", "t_old")], captured

    def test_worker_cold_rerun_carries_scripts_when_dirty(
            self, tmp_path, monkeypatch):
        """D1-R1/D2：脏基线（bail 关）时 worker 分支的 cold 腿同样走直驱
        宿主——否则红测试物理跳过在 cold 腿失效。经 execute_worker_branch
        驱动（闭包即变异面）。"""
        sess, _captured = self._fake_session()
        monkeypatch.setattr(wc, "WorkerSession", lambda *a, **k: sess)
        captured: dict = {}

        def spy_wb_cold(m, seq, **kwargs):
            captured.update(kwargs)
            return {"verdict": "survived"}

        monkeypatch.setattr(wc, "_wb_cold", spy_wb_cold)
        mutants = [_mutant()]
        wc.execute_worker_branch(
            "calc.gd", str(tmp_path), "SRC", mutants,
            "res://.gqt-attr-select/s/", {"t_red"}, True,
            type("Slot", (), {"root": str(tmp_path),
                              "project_root": str(tmp_path)})(),
            "Calc", "calc.gd", "res://x.gd", "linux",
            None, None, [], [], lambda m: ("own", None),
            timings_out={}, kill_bail=False,
            tests_scripts=["res://s/a.gd"],
            red_pairs=[("res://s/a.gd", "t_red")],
            baseline_red_map={("res://s/a.gd", "t_red")})
        assert captured.get("direct_scripts") == ["res://s/a.gd"], captured
        assert captured.get("red_pairs") == [("res://s/a.gd", "t_red")], \
            captured


class TestWorkerVerdictBailAttach:
    """worker 腿 bail_info 必须随 verdict 披露（§SPEC-P3 机器证据面）。"""

    def test_bail_info_attaches_to_killed_verdict(self):
        leg = wc.WorkerLegResult(
            leg=0, hash_ok=True, gut_ran=True, fail_count=1,
            failing_tests={"test_new"},
            failing_scripts={("res://s/b.gd", "test_new")}, ok=True,
            bail_info={"scripts_run": 1, "scripts_total": 2, "bail": True,
                       "bail_at": 0, "new_failure": "test_new"})
        verdict = reporting.classify_worker_leg(
            _mutant(), leg, {"test_old"}, True, None, "/p", "SRC",
            baseline_red_map={("res://s/a.gd", "test_old")})
        assert verdict["verdict"] == "killed", verdict
        assert verdict["kill_bail_info"] == {
            "scripts_run": 1, "scripts_total": 2, "bail": True,
            "bail_at": 0, "new_failure": "test_new"}, verdict

    def test_no_bail_info_keeps_verdict_clean(self):
        leg = wc.WorkerLegResult(
            leg=0, hash_ok=True, gut_ran=True, fail_count=1,
            failing_tests={"test_new"},
            failing_scripts={("res://s/b.gd", "test_new")}, ok=True)
        verdict = reporting.classify_worker_leg(
            _mutant(), leg, set(), False, None, "/p", "SRC")
        assert verdict["verdict"] == "killed", verdict
        assert "kill_bail_info" not in verdict, verdict


class TestCertificatesCarryKillBail:
    """killed 证书注入 kill_bail 披露（在场时）。"""

    def test_cert_injects_kill_bail(self):
        record = {"id": "calc.gd#f#1", "verdict": "killed",
                  "killed_by": "tests/h1/test_1_killer.gd::test_h1_killer",
                  "kill_bail_info": {"scripts_run": 1, "scripts_total": 2,
                                     "new_failure": "test_h1_killer"}}
        summary: dict = {}
        reporting._emit_killed_certificates(summary, [record])
        certs = summary.get("killed_by_certificates") or []
        assert certs and certs[0]["id"] == "calc.gd#f#1"
        assert certs[0]["kill_bail"] == {"scripts_run": 1,
                                         "scripts_total": 2,
                                         "new_failure": "test_h1_killer"}

    def test_cert_without_bail_stays_flat(self):
        record = {"id": "x#f#1", "verdict": "killed", "killed_by": "t"}
        summary: dict = {}
        reporting._emit_killed_certificates(summary, [record])
        certs = summary.get("killed_by_certificates") or []
        assert certs and "kill_bail" not in certs[0]


class TestSequentialHostStaticPins:
    """嵌入宿主的顺序执行原语静态钉（宿主为字符串模板，行为面由真实
    GUT battery 与 parity 覆盖；此处钉原语在场，防静默移除）。"""

    def test_bail_early_stop_break_pinned(self):
        needle = ('new_fail=" + new_failure)\n\t\t\tbreak')
        assert needle in directdrive.SEQUENTIAL_RUNNER_GD, \
            "bail early-stop break removed from sequential runner"

    def test_worker_host_sequential_branch_pinned(self):
        assert "if scripts.size() > 0:" in worker_session.WORKER_HOST_GD, \
            "worker sequential branch gate removed"


class TestPlanChunksFlagChain:
    """_plan_file_chunks → _plan_serial_finish 旗标透传（零执行分支）。"""

    def test_plan_file_chunks_passes_flags_to_serial_finish(
            self, monkeypatch):
        import godot_qa_toolkit.mutation.planning as pl
        captured: dict = {}

        monkeypatch.setattr(pl, "_plan_collect", lambda f: ("SRC", [], 0, None))
        monkeypatch.setattr(pl, "_plan_resolve_gate",
                            lambda *a, **k: ("res://tests/", {}, [], None))
        monkeypatch.setattr(pl, "_plan_diff_phase",
                            lambda *a, **k: ("h", [], {}, None, None, None,
                                             None))
        monkeypatch.setattr(pl, "_plan_budget_phase",
                            lambda *a, **k: ([], [], [], {"elapsed": 1.0}, []))

        def fake_finish(*args, **kwargs):
            captured["positional"] = args
            return {"ok": True, "summary": {}}

        monkeypatch.setattr(pl, "_plan_serial_finish", fake_finish)
        pl._plan_file_chunks("/p/calc.gd", "/p", 0, 60, None, 1, "linux",
                             None, None, False, False, {},
                             select_mode="full",
                             break_on_baseline_failure=True, kill_bail=False)
        args = captured["positional"]
        assert args[15] is True, f"break flag lost in planning chain: {args}"
        assert args[16] is False, f"kill_bail lost in planning chain: {args}"


class TestScriptAwareNewDirect:
    """_script_aware_new 不得退化为裸名差集（同名绿脚本 killer 照杀）。"""

    def test_same_name_other_script_is_new_failure(self):
        got = reporting._script_aware_new(
            {"test_x"}, {("res://s/b.gd", "test_x")}, {"test_x"},
            {("res://s/a.gd", "test_x")})
        assert got == {"test_x"}, got

    def test_exact_script_match_exempt(self):
        got = reporting._script_aware_new(
            {"test_x"}, {("res://s/a.gd", "test_x")}, {"test_x"},
            {("res://s/a.gd", "test_x")})
        assert got == set(), got
