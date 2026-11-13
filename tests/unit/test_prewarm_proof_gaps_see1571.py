"""SEE-1571 A5 hardener：E1 prewarm 证明判定面覆盖闭口（A3 移交清单）。

A3 tidy 移交的行覆盖缺口（`prewarm_proof.py` 75% 判定面）逐条补钉：
- `_junit_report_paths` 全路径（配置缺失/不可解析/非 res:///逃逸/在根内）；
- `_load_proof` 各失配早退（无文件/不可解析/根非对象/字段缺失/schema/
  realpath/last_import_rc/身份不可解析）；
- `_invalidate_proof` 三态（absent / stale-renamed / unlink 兜底 / 双失败）；
- `_run_import` rc/ERROR 输出/超时/OSError 四门；
- `_PrewarmLock` open 与 flock 失败（fail-closed：放弃跨进程跳过）；
- `prewarm_with_proof` 非项目 / 逃生门失败倒 / 无锁 / 流程异常兜底；
- `_locked_flow` 签证失败路径（首导入失败 / 双导入不收敛 / 校验导入失败 /
  扫描歧义 / 身份校准失败 / 不自包含）；
- `reissue_after_drift` 与 `guard_baseline_root` 全路径（不激活/绑定/双失配/
  执行根歧义）；
- `_engine_identity` 启动器不可解析 / 版本门 rc/空输出/OSError / 二进制本体；
- 扫描防御面（非 res:// remap、逃逸 remap、非常规文件、scandir 失败）。
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import prewarm_proof as pp

STUB_IDENTITY = {
    "command": "/fake/godot", "kind": "linux",
    "launcher_path": "/fake/godot", "launcher_sha256": "f" * 64,
    "chain_sha256": [], "version": "4.6.2-stable-test",
}


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _make_project(tmp_path: Path, name: str = "proj") -> Path:
    proj = tmp_path / name
    proj.mkdir()
    _write(proj / "project.godot", "config_version=5\n")
    _write(proj / "src.gd", "func a():\n\treturn 1\n")
    return proj


def _seed_artifacts(root: Path) -> None:
    godot = root / ".godot"
    (godot / "imported").mkdir(parents=True, exist_ok=True)
    _write(godot / "uid_cache.bin", b"uid-cache-v1")
    _write(godot / "global_script_class_cache.cfg", "cache\n")


def _install_identity(monkeypatch) -> None:
    monkeypatch.setattr(pp, "_engine_identity",
                        lambda check_version: dict(STUB_IDENTITY))


def _install_imports(monkeypatch, outcomes) -> list:
    """按序返回 (ok, reason) 的假导入；每次成功导入都补必要产物。"""
    calls: list[str] = []
    queue = list(outcomes)

    def fake(project_root: str) -> tuple[bool, str]:
        calls.append(project_root)
        ok, reason = queue.pop(0) if queue else (True, "rc=0 clean")
        if ok:
            _seed_artifacts(Path(project_root))
        return ok, reason

    monkeypatch.setattr(pp, "_run_import", fake)
    return calls


class _OsProxy:
    """局部 os 代理：只覆盖被测分支，避免污染进程级 os 模块。"""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(os, name)


class TestJunitReportPaths:
    def _files(self, proj: Path) -> dict:
        return pp._walk_files(proj.resolve())

    def test_no_gut_config_excludes_nothing(self, tmp_path):
        proj = _make_project(tmp_path)
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    def test_unparseable_config_excludes_nothing(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json", "{not json")
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    def test_non_object_root_excludes_nothing(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json", "[1, 2, 3]")
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    @pytest.mark.parametrize("value", [None, 42, "", "   "])
    def test_non_res_string_or_empty_excludes_nothing(self, tmp_path, value):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json",
               json.dumps({"junit_xml_file": value}))
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    def test_res_path_inside_root_is_excluded(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json",
               json.dumps({"junit_xml_file": "res://qa/report.xml"}))
        _write(proj / "qa" / "report.xml", "<xml/>")
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == {
            "qa/report.xml"}

    def test_res_path_escaping_root_excludes_nothing(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json",
               json.dumps({"junit_xml_file": "res://../outside.xml"}))
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    def test_non_res_absolute_form_excludes_nothing(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(proj / ".gutconfig.json",
               json.dumps({"junit_xml_file": "/tmp/report.xml"}))
        assert pp._junit_report_paths(proj.resolve(), self._files(proj)) == set()

    def test_junit_report_content_not_in_input_digest(self, tmp_path):
        """排除是判据级事实：报告内容变化不得改变 input_digest。"""
        proj = _make_project(tmp_path)
        _seed_artifacts(proj)
        _write(proj / ".gutconfig.json",
               json.dumps({"junit_xml_file": "res://qa/report.xml"}))
        _write(proj / "qa" / "report.xml", "run-1")
        first = pp._scan_state(str(proj))
        _write(proj / "qa" / "report.xml", "run-2-completely-different")
        second = pp._scan_state(str(proj))
        assert first["input_digest"] == second["input_digest"]

    def test_unparseable_config_keeps_report_in_input_digest(self, tmp_path):
        """不确定则不排除（保守多纳入）：报告变化必须改变 input_digest。"""
        proj = _make_project(tmp_path)
        _seed_artifacts(proj)
        _write(proj / ".gutconfig.json", "{not json")
        _write(proj / "qa" / "report.xml", "run-1")
        first = pp._scan_state(str(proj))
        _write(proj / "qa" / "report.xml", "run-2-completely-different")
        second = pp._scan_state(str(proj))
        assert first["input_digest"] != second["input_digest"]


class TestLoadProofEarlyExits:
    def test_missing_file(self, tmp_path):
        proj = _make_project(tmp_path)
        assert pp._load_proof(str(proj)) == (None, "no proof file")

    def test_unreadable_json(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(pp._proof_paths(str(proj))[1], "{broken")
        cert, reason = pp._load_proof(str(proj))
        assert cert is None and reason.startswith("proof unreadable")

    def test_root_not_object(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(pp._proof_paths(str(proj))[1], "[1]")
        assert pp._load_proof(str(proj)) == (None, "proof root not an object")

    def test_missing_fields(self, tmp_path):
        proj = _make_project(tmp_path)
        _write(pp._proof_paths(str(proj))[1], json.dumps({"schema": pp.SCHEMA}))
        cert, reason = pp._load_proof(str(proj))
        assert cert is None and reason.startswith("proof fields missing")

    def test_schema_mismatch(self, tmp_path):
        proj = _make_project(tmp_path)
        payload = self._complete_payload(proj)
        payload["schema"] = "gqt-prewarm-proof/0"
        _write(pp._proof_paths(str(proj))[1], json.dumps(payload))
        assert pp._load_proof(str(proj)) == (
            None, "schema mismatch: gqt-prewarm-proof/0")

    def test_project_realpath_mismatch(self, tmp_path):
        proj = _make_project(tmp_path)
        payload = self._complete_payload(proj)
        payload["project_realpath"] = "/someone/else"
        _write(pp._proof_paths(str(proj))[1], json.dumps(payload))
        assert pp._load_proof(str(proj)) == (
            None, "project realpath mismatch")

    def test_last_import_rc_nonzero(self, tmp_path):
        proj = _make_project(tmp_path)
        payload = self._complete_payload(proj)
        payload["last_import_rc"] = 1
        _write(pp._proof_paths(str(proj))[1], json.dumps(payload))
        assert pp._load_proof(str(proj)) == (None, "last import rc != 0")

    def test_identity_unresolvable(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        payload = self._complete_payload(proj)
        _write(pp._proof_paths(str(proj))[1], json.dumps(payload))

        def _boom(check_version):
            raise pp.TreeAmbiguity("engine gone")

        monkeypatch.setattr(pp, "_engine_identity", _boom)
        cert, reason = pp._load_proof(str(proj))
        assert cert is None
        assert reason == "engine identity unresolvable: engine gone"

    @staticmethod
    def _complete_payload(proj: Path) -> dict:
        return {
            "schema": pp.SCHEMA,
            "project_realpath": str(proj.resolve()),
            "identity": dict(STUB_IDENTITY),
            "providers_version": pp.REGISTRY_VERSION,
            "input_digest": "i" * 64,
            "output_seal": "o" * 64,
            "proof_id": "p1",
            "last_import_rc": 0,
        }


class TestInvalidateProof:
    def test_absent(self, tmp_path):
        proj = _make_project(tmp_path)
        assert pp._invalidate_proof(str(proj)) == "absent"

    def test_rename_to_stale(self, tmp_path):
        proj = _make_project(tmp_path)
        path = _write(pp._proof_paths(str(proj))[1], "{}")
        assert pp._invalidate_proof(str(proj)) == "stale-renamed"
        assert not path.exists()
        assert path.with_suffix(".json.stale").is_file()

    def test_unlink_fallback_when_rename_fails(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        path = _write(pp._proof_paths(str(proj))[1], "{}")
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == path:
                raise OSError("rename denied")
            return real_replace(src, dst)

        monkeypatch.setattr(pp, "os", _OsProxy(replace=replace))
        assert pp._invalidate_proof(str(proj)) == "unlinked"
        assert not path.exists()

    def test_remove_failed_when_both_paths_fail(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        path = _write(pp._proof_paths(str(proj))[1], "{}")

        def replace(src, dst):
            if Path(src) == path:
                raise OSError("rename denied")
            return os.replace(src, dst)

        def unlink(self, *a, **k):
            raise OSError("unlink denied")

        monkeypatch.setattr(pp, "os", _OsProxy(replace=replace))
        monkeypatch.setattr(Path, "unlink", unlink)
        assert pp._invalidate_proof(str(proj)) == "remove-failed"


class TestRunImportGates:
    def _fake_proc(self, rc=0, stdout="", stderr=""):
        return types.SimpleNamespace(returncode=rc, stdout=stdout,
                                     stderr=stderr)

    def _patch_run(self, monkeypatch, fn):
        monkeypatch.setattr(pp, "subprocess", types.SimpleNamespace(
            run=fn, TimeoutExpired=__import__("subprocess").TimeoutExpired))

    def test_clean_import(self, tmp_path, monkeypatch):
        seen = {}

        def run(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return self._fake_proc()

        self._patch_run(monkeypatch, run)
        ok, reason = pp._run_import(str(tmp_path))
        assert (ok, reason) == (True, "rc=0 clean")
        assert seen["argv"][1:3] == ["--headless", "--import"]

    def test_nonzero_rc(self, tmp_path, monkeypatch):
        self._patch_run(monkeypatch, lambda *a, **k: self._fake_proc(rc=3))
        assert pp._run_import(str(tmp_path)) == (False, "import rc=3")

    def test_error_output(self, tmp_path, monkeypatch):
        self._patch_run(monkeypatch, lambda *a, **k: self._fake_proc(
            stderr="ok\nERROR: bucket_table_size == 0\n"))
        ok, reason = pp._run_import(str(tmp_path))
        assert ok is False
        assert reason.startswith("import error output: ERROR:")
        assert "bucket_table_size" in reason

    def test_timeout(self, tmp_path, monkeypatch):
        import subprocess as sp

        def run(*a, **k):
            raise sp.TimeoutExpired(cmd="godot", timeout=1)

        self._patch_run(monkeypatch, run)
        ok, reason = pp._run_import(str(tmp_path))
        assert ok is False and reason.startswith("import timed out after")

    def test_launch_oserror(self, tmp_path, monkeypatch):
        def run(*a, **k):
            raise OSError("no such binary")

        self._patch_run(monkeypatch, run)
        assert pp._run_import(str(tmp_path)) == (
            False, "import launch failed: no such binary")


class TestPrewarmLock:
    def test_acquire_release_roundtrip(self, tmp_path):
        proj = _make_project(tmp_path)
        lock = pp._PrewarmLock(str(proj))
        assert lock.acquire() is True
        lock.release()
        assert lock._fd is None
        # 释放后可再次获取（fd 未泄漏）
        assert lock.acquire() is True
        lock.release()

    def test_open_failure_disables_skip(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)

        def open_fail(*a, **k):
            raise OSError("open denied")

        monkeypatch.setattr(pp, "os", _OsProxy(open=open_fail))
        assert pp._PrewarmLock(str(proj)).acquire() is False

    def test_flock_failure_disables_skip(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        closed = []

        def flock_fail(fd, op):
            raise OSError("flock denied")

        monkeypatch.setattr(pp, "fcntl", types.SimpleNamespace(
            flock=flock_fail, LOCK_EX=1, LOCK_UN=2))
        monkeypatch.setattr(pp, "os", _OsProxy(
            close=lambda fd: closed.append(fd)))
        assert pp._PrewarmLock(str(proj)).acquire() is False
        assert len(closed) == 1, "flock 失败必须关闭 fd（不得泄漏）"


class TestPrewarmEntryFailClosed:
    def test_non_project_is_skipped(self, tmp_path):
        bare = tmp_path / "not_a_project"
        bare.mkdir()
        assert pp.prewarm_with_proof(str(bare)) == {
            "decision": "skipped_non_project", "import_calls": 0,
            "probe_s": 0.0}

    def test_escape_hatch_reports_import_failure(
            self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_imports(monkeypatch, [(False, "import rc=9")])
        monkeypatch.setenv(pp.ENV_TOGGLE, "no")
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "disabled"
        assert out["import_calls"] == 1
        assert out["reason"] == "import rc=9"
        assert "escape hatch" in capsys.readouterr().err

    def test_lock_unavailable_imports_uncertified(
            self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_imports(monkeypatch, [(True, "rc=0 clean")])
        monkeypatch.setattr(pp._PrewarmLock, "acquire", lambda self: False)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_no_lock"
        assert out["import_calls"] == 1
        assert out["reason"] == "flock unavailable"
        assert "no reliable lock" in capsys.readouterr().err

    def test_flow_exception_falls_back_to_import(
            self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        calls = _install_imports(monkeypatch, [(True, "rc=0 clean")])

        def boom(project_root, t0, out):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pp, "_locked_flow", boom)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["import_calls"] == 1
        assert "prewarm flow error: unexpected" in out["reason"]
        assert len(calls) == 1
        assert "flow error fallback" in capsys.readouterr().err


class TestLockedFlowCertificationFailures:
    def _run(self, monkeypatch, proj, outcomes, capsys):
        calls = _install_imports(monkeypatch, outcomes)
        out = pp.prewarm_with_proof(str(proj))
        return out, calls, capsys.readouterr().err

    def test_first_import_failed(self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        out, calls, err = self._run(
            monkeypatch, proj, [(False, "import rc=5")], capsys)
        assert out["decision"] == "imported_uncertified"
        assert out["reason"] == "first import failed: import rc=5"
        assert len(calls) == 1
        assert "no cert" in err

    def test_post_import_scan_ambiguous(self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)

        def barren_import(project_root):
            return True, "rc=0 clean"          # 不创建 .godot 必要产物

        monkeypatch.setattr(pp, "_run_import", barren_import)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["reason"].startswith("post-import scan ambiguous:")
        assert "required import artifact missing" in out["reason"]

    def test_verification_import_failed(self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        out, calls, _ = self._run(
            monkeypatch, proj, [(True, "rc=0 clean"), (False, "import rc=7")],
            capsys)
        assert out["decision"] == "imported_uncertified"
        assert out["reason"] == "verification import failed: import rc=7"
        assert out["import_calls"] == 2
        assert len(calls) == 2

    def test_double_import_non_convergence(
            self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        counter = {"n": 0}

        def drifting_import(project_root: str):
            counter["n"] += 1
            root = Path(project_root)
            _seed_artifacts(root)
            _write(root / "drift.txt", f"generation-{counter['n']}")
            return True, "rc=0 clean"

        monkeypatch.setattr(pp, "_run_import", drifting_import)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["reason"] == "double import did not converge"
        assert counter["n"] == 2

    def test_identity_calibration_failed(self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)

        def boom(check_version):
            raise pp.TreeAmbiguity("version gate broke")

        monkeypatch.setattr(pp, "_engine_identity", boom)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["reason"] == (
            "identity/provider calibration failed: version gate broke")

    def test_project_not_self_contained(self, tmp_path, monkeypatch, capsys):
        proj = _make_project(tmp_path)
        _write(proj / "z.png", b"\x89PNG\r\n\x1a\n")
        _write(proj / "z.png.import",
               '[remap]\n\nimporter="exotic"\npath="res://z.ctex"\n')
        _write(proj / "z.ctex", b"art")
        _install_identity(monkeypatch)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        out = pp.prewarm_with_proof(str(proj))
        assert out["decision"] == "imported_uncertified"
        assert out["reason"].startswith("project not self-contained:")
        assert "exotic" in out["reason"]


class TestReissueAfterDrift:
    def test_reissue_invalidates_then_resigns(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        first = pp.prewarm_with_proof(str(proj))
        assert first["decision"] == "imported_certified"
        out = pp.reissue_after_drift(str(proj))
        assert out["decision"] == "imported_certified"
        # reissue 先失效旧证（stale-renamed），流程内二次失效见到 absent。
        assert out["reason"] == "invalidate=absent"
        assert out["proof_id"] == first["proof_id"]      # 同输入同身份 → 同证
        stale = pp._proof_paths(str(proj))[1].with_suffix(".json.stale")
        assert stale.is_file()

    def test_reissue_without_prior_proof_imports(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        out = pp.reissue_after_drift(str(proj))
        assert out["decision"] == "imported_certified"
        assert out["reason"] == "invalidate=absent"


class TestGuardBaselineRoot:
    def _sealed_project(self, tmp_path, monkeypatch) -> Path:
        proj = _make_project(tmp_path)
        _install_identity(monkeypatch)
        _install_imports(monkeypatch, [(True, "rc=0 clean")] * 2)
        assert pp.prewarm_with_proof(str(proj))["decision"] == (
            "imported_certified")
        return proj

    def test_inactive_without_proof(self, tmp_path):
        proj = _make_project(tmp_path)
        ok, reason = pp.guard_baseline_root(str(proj), str(proj))
        assert ok is True
        assert reason == "guard inactive (no usable proof: no proof file)"

    def test_bound_when_execution_root_matches(self, tmp_path, monkeypatch):
        proj = self._sealed_project(tmp_path, monkeypatch)
        assert pp.guard_baseline_root(str(proj), str(proj)) == (True, "bound")

    def test_input_digest_mismatch(self, tmp_path, monkeypatch):
        proj = self._sealed_project(tmp_path, monkeypatch)
        copy = tmp_path / "exec_root"
        import shutil
        shutil.copytree(proj, copy)
        _write(copy / "src.gd", "func a():\n\treturn 999\n")
        ok, reason = pp.guard_baseline_root(str(copy), str(proj))
        assert ok is False
        assert reason == "input digest mismatch at execution root"

    def test_output_seal_mismatch(self, tmp_path, monkeypatch):
        proj = self._sealed_project(tmp_path, monkeypatch)
        import shutil
        copy = tmp_path / "exec_root"
        shutil.copytree(proj, copy)
        _write(copy / ".godot" / "uid_cache.bin", b"tampered-output")
        ok, reason = pp.guard_baseline_root(str(copy), str(proj))
        assert ok is False
        assert reason == "output seal mismatch at execution root"

    def test_execution_root_scan_ambiguous(self, tmp_path, monkeypatch):
        proj = self._sealed_project(tmp_path, monkeypatch)
        import shutil
        copy = tmp_path / "exec_root"
        shutil.copytree(proj, copy)
        os.symlink(copy / "src.gd", copy / "link.gd")
        ok, reason = pp.guard_baseline_root(str(copy), str(proj))
        assert ok is False
        assert reason.startswith("execution root scan ambiguous:")


