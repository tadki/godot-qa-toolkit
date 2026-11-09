"""§SPEC-007：缓存身份与 killed 证书单测。

- `OPS_VERSION` 单 bump：旧 resume/coverage store 不读不写不删并 stderr 披露；
- 身份包含 inject_scheme / 精确 Godot 版本串（按 path+mtime 缓存）/
  boot-wiring hash（规范化 project.godot + autoload 清单 + testscan .tscn
  清单）/ GUT addon 内容哈希；
- killed_by 完整失败集（撤除 [:3] 截断）+ killer 改/删/依赖变 → 仅该 entry
  重跑；新增测试不失效旧 killed；解析失败 → 保守重跑。
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import identity
from godot_qa_toolkit.mutation.diffengine import plan_diff
from godot_qa_toolkit.mutation.identity import (
    INJECT_SCHEME,
    TestOwnershipIndex,
    boot_wiring_hash,
    certificate_valid,
    deps_closure_sha1,
    disclose_superseded,
    godot_version_string,
    gut_addon_hash,
    identity_pack,
    identity_sha1,
    killer_certificate,
    normalize_project_godot,
)
from godot_qa_toolkit.mutation.ops import OPS_VERSION
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2


def _write_project(tmp_path, with_gut=True):
    proj = tmp_path / "proj"
    proj.mkdir()
    if with_gut:
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut stub")
    (proj / "project.godot").write_text(
        'config_version=5\n\n[application]\n'
        'run/main_scene="res://main.tscn"\n\n[autoload]\n'
        'Events="*res://autoload/events.gd"\n')
    (proj / "autoload").mkdir()
    (proj / "autoload" / "events.gd").write_text("extends Node\n")
    return proj


CERT = [{"test_name": "test_x", "script_path": "res://tests/test_x.gd",
         "script_sha1": "a", "deps_sha1": "b"}]


class TestIdentityPack:
    def test_pack_components(self, tmp_path):
        proj = _write_project(tmp_path)
        pack = identity_pack(str(proj), "linux", godot_bin="/nonexistent/g")
        assert pack["inject_scheme"] == INJECT_SCHEME == "disk-farm/1"
        assert pack["ops_version"] == OPS_VERSION
        assert pack["binary_kind"] == "linux"
        assert pack["godot_version"].startswith("unresolved:")
        assert pack["boot_wiring_hash"]
        assert pack["gut_addon_hash"] == hashlib.sha1(
            f"addons/gut/gut_cmdln.gd:{hashlib.sha1(b'# gut stub').hexdigest()}"
            .encode("utf-8")).hexdigest()

    def test_identity_sha1_stable_and_sensitive(self, tmp_path):
        proj = _write_project(tmp_path)
        p1 = identity_pack(str(proj), "linux", godot_bin="/nonexistent/g")
        p2 = identity_pack(str(proj), "linux", godot_bin="/nonexistent/g")
        assert identity_sha1(p1) == identity_sha1(p2)
        p3 = dict(p1, godot_version="4.6.2-other")
        assert identity_sha1(p3) != identity_sha1(p1)

    def test_version_cached_by_path_and_mtime(self, tmp_path, monkeypatch):
        binp = tmp_path / "godot"
        binp.write_text("#!/bin/sh\necho 4.6.2-fake\n")
        binp.chmod(0o755)
        calls = {"n": 0}
        import subprocess as sp
        real_run = sp.run

        def counting_run(*a, **k):
            calls["n"] += 1
            return real_run(*a, **k)

        monkeypatch.setattr(identity.subprocess, "run", counting_run)
        v1 = godot_version_string(str(binp))
        v2 = godot_version_string(str(binp))
        assert v1 == v2 == "4.6.2-fake"
        assert calls["n"] == 1, "同 path+mtime 缓存命中"
        binp.write_text("#!/bin/sh\necho 4.7.0-fake\n")  # mtime 变化
        v3 = godot_version_string(str(binp))
        assert v3 == "4.7.0-fake"
        assert calls["n"] == 2, "mtime 漂移必须重解析"

    def test_wiring_hash_sensitive_to_godot_edits(self, tmp_path):
        proj = _write_project(tmp_path)
        h1 = boot_wiring_hash(str(proj))
        # 排版/注释变化 → 不触发
        pg = proj / "project.godot"
        pg.write_text(pg.read_text(encoding="utf-8") + "\n; a comment\n")
        assert boot_wiring_hash(str(proj)) == h1
        # autoload 接线变化 → 触发
        pg.write_text('config_version=5\n[autoload]\n'
                      'Events="*res://autoload/events.gd"\n'
                      'More="*res://autoload/events.gd"\n')
        assert boot_wiring_hash(str(proj)) != h1

    def test_wiring_hash_sensitive_to_tscn_refs(self, tmp_path):
        proj = _write_project(tmp_path)
        h1 = boot_wiring_hash(str(proj))
        tests = proj / "tests"
        tests.mkdir()
        (tests / "test_scene.gd").write_text(
            'const S := preload("res://scenes/main.tscn")\n')
        assert boot_wiring_hash(str(proj)) != h1

    def test_gut_addon_hash_missing_and_content_sensitive(self, tmp_path):
        proj = _write_project(tmp_path, with_gut=False)
        assert gut_addon_hash(str(proj)) == "missing"
        gut = proj / "addons" / "gut"
        gut.mkdir(parents=True)
        (gut / "gut_cmdln.gd").write_text("# v1")
        h1 = gut_addon_hash(str(proj))
        (gut / "gut_cmdln.gd").write_text("# v2 upgraded")
        assert gut_addon_hash(str(proj)) != h1

    def test_normalize_strips_comments_and_blanks(self):
        lines = normalize_project_godot(
            "; comment\n\n[autoload]\n  Key = \"value\"  \n# hash comment\n")
        assert lines == ["[autoload]", 'Key="value"']


class TestKillCertificate:
    def _fixture(self, tmp_path):
        proj = _write_project(tmp_path, with_gut=False)
        tests = proj / "tests"
        tests.mkdir()
        (tests / "test_x.gd").write_text(
            'extends GutTest\n\nconst H := preload("res://helpers/h.gd")\n\n'
            "func test_x():\n\tpass\n")
        helpers = proj / "helpers"
        helpers.mkdir()
        (helpers / "h.gd").write_text("extends RefCounted\n")
        return proj

    def test_full_failure_set_no_truncation(self, tmp_path):
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        failures = {f"test_k{i}" for i in range(30)}
        (tests := proj / "tests").joinpath("test_k.gd").write_text(
            "".join(f"func test_k{i}():\n\tpass\n\n" for i in range(30)))
        ownership2 = TestOwnershipIndex(str(proj))
        cert = killer_certificate(failures, ownership2, str(proj))
        assert cert is not None
        assert len(cert) == 30, "完整失败集——撤除 [:3] 截断"
        assert {c["test_name"] for c in cert} == failures

    def test_cert_fields_shape(self, tmp_path):
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        cert = killer_certificate({"test_x"}, ownership, str(proj))
        assert cert is not None and len(cert) == 1
        c = cert[0]
        assert c["test_name"] == "test_x"
        assert c["script_path"] == "res://tests/test_x.gd"
        assert c["script_sha1"] == hashlib.sha1(
            (proj / "tests" / "test_x.gd").read_bytes()).hexdigest()
        assert c["deps_sha1"] == deps_closure_sha1(
            str(proj), "res://tests/test_x.gd")

    def test_ambiguous_owner_returns_none(self, tmp_path):
        proj = self._fixture(tmp_path)
        (proj / "tests" / "test_x2.gd").write_text(
            "func test_x():\n\tpass\n")
        ownership = TestOwnershipIndex(str(proj))
        assert killer_certificate({"test_x"}, ownership, str(proj)) is None

    def test_missing_owner_returns_none(self, tmp_path):
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        assert killer_certificate({"test_absent"}, ownership,
                                  str(proj)) is None

    def test_deps_closure_literal_only(self, tmp_path):
        """deps = 字面量 preload/load 闭包——动态构造路径 → None（保守）。"""
        proj = self._fixture(tmp_path)
        sha = deps_closure_sha1(str(proj), "res://tests/test_x.gd")
        assert sha is not None
        # 依赖文件内容变化 → 闭包哈希变化（helper 修改触发重跑的依据）
        (proj / "helpers" / "h.gd").write_text("extends RefCounted\n# chg\n")
        assert deps_closure_sha1(str(proj), "res://tests/test_x.gd") != sha

    def test_cert_valid_matrix(self, tmp_path):
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        cert = killer_certificate({"test_x"}, ownership, str(proj))
        assert certificate_valid(cert, str(proj)) is True
        # killer 测试修改 → False
        (proj / "tests" / "test_x.gd").write_text(
            'extends GutTest\n\nconst H := preload("res://helpers/h.gd")\n\n'
            "func test_x():\n\tprint(1)\n")
        assert certificate_valid(cert, str(proj)) is False
        # killer 删除 → False
        (proj / "tests" / "test_x.gd").unlink()
        assert certificate_valid(cert, str(proj)) is False
        # 空证书 / 非法结构 → False
        assert certificate_valid([], str(proj)) is False
        assert certificate_valid("junk", str(proj)) is False

    def test_cert_dep_change_only_invalidates(self, tmp_path):
        """依赖（helper）变化 → 校验失败仅该 entry 重跑（复用规则定向性）。"""
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        cert = killer_certificate({"test_x"}, ownership, str(proj))
        (proj / "helpers" / "h.gd").write_text("extends RefCounted\n# v2\n")
        assert certificate_valid(cert, str(proj)) is False

    def test_new_test_does_not_invalidate_old_killed(self, tmp_path):
        """kill 单调性：新增测试文件不改 killer 自身/闭包 → 证书仍有效。"""
        proj = self._fixture(tmp_path)
        ownership = TestOwnershipIndex(str(proj))
        cert = killer_certificate({"test_x"}, ownership, str(proj))
        (proj / "tests" / "test_new.gd").write_text("func test_new():\n\tpass\n")
        assert certificate_valid(cert, str(proj)) is True


class TestCertReuseDiff:
    def _mutants(self, src, label="x.gd"):
        from godot_qa_toolkit.mutation.ops import _collect_mutations
        from godot_qa_toolkit.mutation.scopemanifest import build_manifest
        _, _, tagged, scopes = build_manifest(src, label,
                                              _collect_mutations(src, label))
        return tagged, scopes

    def test_killed_without_cert_reruns(self):
        src = "func add(a, b):\n\treturn a + b\n"
        mutants, scopes = self._mutants(src)
        sid = mutants[0].stable_id
        prior = {sid: {"verdict": "killed", "reason": "r", "line": 2,
                       "column": 11, "kind": "AOR", "original": "+",
                       "mutated": "-", "scope_hash": "S",
                       "timeout_streak": 0}}
        plan = plan_diff("H", {"func:add": "S"}, {"header_hash": "H",
                                                  "scopes": {"func:add": "S"}},
                         mutants, prior)
        assert plan.triggers[sid] == "cert_missing"
        assert sid not in plan.reused

    def test_killer_changed_reruns_only_that_entry(self):
        """killer 改/删/依赖变 → 仅该 entry 重跑（其余 killed 照常复用）。"""
        src = ("func add(a, b):\n\treturn a + b\n\n"
               "func sub(a, b):\n\treturn a - b\n")
        mutants, scopes = self._mutants(src)
        def _entry(sid, cert):
            m = next(x for x in mutants if x.stable_id == sid)
            return {"verdict": "killed", "reason": "r", "line": m.line,
                    "column": m.column, "kind": m.kind, "original": m.original,
                    "mutated": m.mutated, "scope_hash": scopes[m.scope_id],
                    "timeout_streak": 0, "killed_by": cert}
        good = [{"test_name": "t", "script_path": "res://tests/ok.gd",
                 "script_sha1": "s", "deps_sha1": "d"}]
        bad = [{"test_name": "t", "script_path": "res://tests/gone.gd",
                "script_sha1": "s", "deps_sha1": "d"}]
        prior = {mutants[0].stable_id: _entry(mutants[0].stable_id, bad),
                 mutants[-1].stable_id: _entry(mutants[-1].stable_id, good)}
        scope_map = {m.scope_id: scopes[m.scope_id] for m in mutants}
        plan = plan_diff("H", scope_map,
                         {"header_hash": "H", "scopes": scope_map},
                         mutants, prior,
                         cert_validator=lambda kb, root=None: kb is good)
        assert plan.triggers[mutants[0].stable_id] == "killer_changed"
        assert plan.triggers[mutants[-1].stable_id] == "reused:killed"

    def test_new_tests_do_not_trigger_rerun(self):
        """新增测试不失效旧 killed：证书校验只碰 killer 闭包（validator
        收到 killed_by 与项目根——不收测试树指纹）。"""
        src = "func add(a, b):\n\treturn a + b\n"
        mutants, scopes = self._mutants(src)
        sid = mutants[0].stable_id
        cert = CERT
        prior = {sid: {"verdict": "killed", "reason": "r", "line": 2,
                       "column": 11, "kind": "AOR", "original": "+",
                       "mutated": "-", "scope_hash": "S",
                       "timeout_streak": 0, "killed_by": cert}}
        seen = {}
        def validator(kb, *a, **kw):
            seen["called"] = seen.get("called", 0) + 1
            return kb is cert
        plan = plan_diff("H", {"func:add": "S"}, {"header_hash": "H",
                                                  "scopes": {"func:add": "S"}},
                         mutants, prior, cert_validator=validator)
        assert plan.triggers[sid] == "reused:killed"
        assert seen["called"] == 1


class TestOpsBumpDisclosure:
    def test_superseded_resume_store_disclosed_not_deleted(self, tmp_path,
                                                           capsys):
        """OPS bump：旧 v2 store（ops_version 不符）不读不写不删 + 披露。"""
        proj = _write_project(tmp_path, with_gut=False)
        store_dir = proj / ".qa-cache" / "gqt-mutation-resume-v2" / "oldkey"
        store_dir.mkdir(parents=True)
        manifest = {"schema": "see1420-resume/2", "ops_version": "see1420/1",
                    "file_key": "oldkey"}
        (store_dir / "manifest.json").write_text(json.dumps(manifest),
                                                 encoding="utf-8")
        entry = store_dir / "x.json"
        entry.write_text(json.dumps({"schema": "see1420-resume/2",
                                     "verdict": "killed"}),
                         encoding="utf-8")
        disclose_superseded(proj, store_dir.parent,
                            proj / ".qa-cache" / "gqt-coverage")
        err = capsys.readouterr().err
        assert f"ops_version != {OPS_VERSION}" in err
        assert "neither read, written, nor deleted" in err
        assert entry.exists() and (store_dir / "manifest.json").exists(), \
            "旧条目不删"

    def test_current_ops_store_not_disclosed(self, tmp_path, capsys):
        proj = _write_project(tmp_path, with_gut=False)
        store_dir = proj / ".qa-cache" / "gqt-mutation-resume-v2" / "k"
        store_dir.mkdir(parents=True)
        (store_dir / "manifest.json").write_text(json.dumps(
            {"schema": "see1420-resume/2", "ops_version": OPS_VERSION,
             "file_key": "k"}), encoding="utf-8")
        disclose_superseded(proj, store_dir.parent,
                            proj / ".qa-cache" / "gqt-coverage")
        assert "ops_version !=" not in capsys.readouterr().err

    def test_superseded_coverage_store_disclosed(self, tmp_path, capsys):
        """coverage store 无 inject_scheme 身份（旧 schema）→ 披露。"""
        proj = _write_project(tmp_path, with_gut=False)
        cov_dir = proj / ".qa-cache" / "gqt-coverage"
        cov_dir.mkdir(parents=True)
        (cov_dir / "old.json").write_text(json.dumps(
            {"schema": "see1420-coverage/1", "covered_lines": [2]}),
            encoding="utf-8")
        disclose_superseded(proj, proj / ".qa-cache" / "noresume", cov_dir)
        assert "lack" in capsys.readouterr().err

    def test_file_key_includes_identity(self, tmp_path):
        proj = _write_project(tmp_path, with_gut=False)
        target = proj / "calc.gd"
        target.write_text("func add():\n\treturn 1\n")
        kind = "linux"
        k1 = ResumeStoreV2(str(proj), str(target), "res://tests/",
                           binary_kind=kind,
                           identity=identity_pack(str(proj), kind,
                                                  godot_bin="/no/g")).key
        k2 = ResumeStoreV2(str(proj), str(target), "res://tests/",
                           binary_kind=kind,
                           identity=identity_pack(str(proj), kind,
                                                  godot_bin="/no/g")).key
        k3 = ResumeStoreV2(str(proj), str(target), "res://tests/",
                           binary_kind=kind,
                           identity=dict(identity_pack(
                               str(proj), kind, godot_bin="/no/g"),
                               gut_addon_hash="changed")).key
        assert k1 == k2, "同身份同 key"
        assert k3 != k1, "身份漂移 → 新 key（旧 store 自然失效）"

    def test_manifest_carries_identity(self, tmp_path):
        proj = _write_project(tmp_path, with_gut=False)
        target = proj / "calc.gd"
        target.write_text("func add():\n\treturn 1\n")
        ident = identity_pack(str(proj), "linux", godot_bin="/no/g")
        store = ResumeStoreV2(str(proj), str(target), "res://tests/",
                              binary_kind="linux", identity=ident)
        store.write_manifest("h", {}, "s", "calc.gd")
        doc = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        assert doc["inject_scheme"] == INJECT_SCHEME
        assert doc["ops_version"] == OPS_VERSION
        assert doc["identity_sha"] == identity_sha1(ident)
