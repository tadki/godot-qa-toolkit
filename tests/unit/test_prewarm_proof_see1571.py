"""SEE-1571 §SPEC-509：E1 prewarm 新鲜度证明关键判定面钉子。

钉死行为（蓝本 = §SPEC-507 定案设计 + B2b-A 修订，质量门全闭后的转正实现）：
- 新鲜跳过（双摘要一致才 skip，stderr 披露 proof_id + 双摘要前缀）；
- 输入变更必重导入；NO_CERT fail-closed 类（畸形 sidecar / 无 importer 行 /
  声明产物缺失 / 逃逸路径）；
- K7：非 keep sidecar 零产物声明 → TreeAmbiguity（豁免仅限 keep）；
- `[deps]` 段产物声明解析（csv_translation 形态）；
- G12：wrapper 链解析至最终二进制、全链内容入证（链上任一环节替换即失配）；
- keep 语义（不入 provider 闭包、零 remap 合法）；版本不匹配拒签。
"""

import json
import os
import sys
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


def _make_project(tmp_path: Path, with_imports: bool = True) -> Path:
    """自包含测试项目：project.godot + 一个源文件 + 可选 keep sidecar。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(proj / "project.godot", "config_version=5\n")
    _write(proj / "src.gd", "func a():\n\treturn 1\n")
    if with_imports:
        _write(proj / "data" / "t.csv", "k,en\n1,one\n")
        _write(proj / "data" / "t.csv.import",
               '[remap]\n\nimporter="keep"\n')
    return proj


def _fake_import(declared_outputs: dict):
    """模拟 godot --import：创建 .godot 必要产物 + 各 sidecar 声明的产物。"""

    def fake(project_root: str) -> tuple[bool, str]:
        root = Path(project_root)
        godot = root / ".godot"
        (godot / "imported").mkdir(parents=True, exist_ok=True)
        _write(godot / "uid_cache.bin", b"uid-cache-v1")
        _write(godot / "global_script_class_cache.cfg", "cache\n")
        for rel, content in declared_outputs.items():
            _write(root / rel, content)
        return True, "rc=0 clean"

    return fake


@pytest.fixture()
def sealed(monkeypatch):
    """身份 stub + 假导入（创建 .godot 必要产物，无真实 godot 子进程）。"""
    monkeypatch.setattr(pp, "_engine_identity",
                        lambda check_version: dict(STUB_IDENTITY))
    calls = {"n": 0}

    def fake_import(project_root: str) -> tuple[bool, str]:
        calls["n"] += 1
        root = Path(project_root)
        godot = root / ".godot"
        (godot / "imported").mkdir(parents=True, exist_ok=True)
        _write(godot / "uid_cache.bin", b"uid-cache-v1")
        _write(godot / "global_script_class_cache.cfg", "cache\n")
        # 模拟 godot：按 sidecar 声明生成产物（[deps] dest_files 形态）
        for sc in root.rglob("*.import"):
            try:
                text = sc.read_text(encoding="utf-8", errors="strict")
            except OSError:
                continue
            for line in text.splitlines():
                if line.startswith("dest_files=["):
                    import re
                    for item in re.findall(r'"([^"]+)"', line):
                        target = root / item.removeprefix("res://")
                        if "translation" in item:
                            _write(target, "translation-bytes")
        return True, "rc=0 clean"

    monkeypatch.setattr(pp, "_run_import", fake_import)
    monkeypatch.setattr(pp, "_engine_calls", calls, raising=False)
    return calls


def _proof(proj: Path) -> dict:
    path = pp._proof_paths(str(proj))[1]
    return json.loads(path.read_text(encoding="utf-8"))


class TestCalibrationAndFreshSkip:
    def test_double_import_calibration_then_certify(
            self, tmp_path, sealed):
        proj = _make_project(tmp_path)
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] == "imported_certified"
        assert r["import_calls"] == 2  # 双导入定点协议
        proof = _proof(proj)
        assert proof["schema"] == "gqt-prewarm-proof/1"
        assert proof["last_import_rc"] == 0
        assert proof["input_digest"] and proof["output_seal"]

    def test_fresh_skip_when_unchanged(self, tmp_path, sealed, capsys):
        proj = _make_project(tmp_path)
        pp.prewarm_with_proof(str(proj))
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] == "fresh_skip"
        assert r["import_calls"] == 0
        err = capsys.readouterr().err
        assert "fresh_skip" in err
        assert r["proof_id"] in err                     # proof_id 披露
        proof = _proof(proj)
        assert proof["input_digest"][:8] in err          # 双摘要前缀披露
        assert proof["output_seal"][:8] in err

    def test_input_change_forces_reimport(self, tmp_path, sealed):
        proj = _make_project(tmp_path)
        pp.prewarm_with_proof(str(proj))
        _write(proj / "src.gd", "func a():\n\treturn 2\n")  # 内容变更
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] != "fresh_skip"
        assert r["import_calls"] >= 1

    def test_escape_hatch_always_imports(self, tmp_path, sealed,
                                         monkeypatch, capsys):
        proj = _make_project(tmp_path)
        pp.prewarm_with_proof(str(proj))                 # 先签证
        monkeypatch.setenv("GQT_PREWARM_SENTINEL", "0")
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] == "disabled"
        assert r["import_calls"] == 1
        assert "escape hatch" in capsys.readouterr().err
        monkeypatch.setenv("GQT_PREWARM_SENTINEL", "false")
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] == "disabled"


class VersionMismatchFixtures:
    pass


class TestVersionMismatch:
    def test_old_registry_version_rejected(self, tmp_path, sealed):
        """版本推进（B2b-A）：旧版证不被视为已验证（拒签 → 重导入）。"""
        proj = _make_project(tmp_path)
        pp.prewarm_with_proof(str(proj))
        proof = _proof(proj)
        proof["providers_version"] = "b2a/1"             # 旧版证
        path = pp._proof_paths(str(proj))[1]
        path.write_text(json.dumps(proof), encoding="utf-8")
        cert, reason = pp._load_proof(str(proj))
        assert cert is None
        assert "provider registry version mismatch" in reason


class TestKeepSemantics:
    def test_keep_excluded_from_provider_closure(self, tmp_path, sealed):
        proj = _make_project(tmp_path)
        files = pp._walk_files(proj.resolve())
        providers, unsupported = pp._enumerate_providers(
            proj.resolve(), files)
        assert "keep" not in providers
        assert unsupported is None

    def test_keep_zero_remap_outputs_legal(self, tmp_path, sealed):
        """keep 零 remap 声明合法（不判歧义），state 可完整计算。"""
        proj = _make_project(tmp_path)
        godot = proj / ".godot"
        (godot / "imported").mkdir(parents=True, exist_ok=True)
        _write(godot / "uid_cache.bin", b"u")
        _write(godot / "global_script_class_cache.cfg", "c")
        state = pp._scan_state(str(proj))
        assert state["input_files"] > 0

    def test_keep_content_change_invalidates(self, tmp_path, sealed):
        proj = _make_project(tmp_path)
        pp.prewarm_with_proof(str(proj))
        side = proj / "data" / "t.csv.import"
        side.write_text('[remap]\n\nimporter="keep"\n# drift\n',
                        encoding="utf-8")
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] != "fresh_skip"


class TestK7NonKeepZeroDeclaration:
    def test_non_keep_zero_declaration_is_ambiguity(self, tmp_path):
        """K7：非 keep sidecar 零产物声明 → TreeAmbiguity（fail-closed），
        keep 的零输出豁免不得扩大到全部 sidecar。"""
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "fake.png", b"\x89PNG\r\n\x1a\n")
        _write(proj / "fake.png.import", '[remap]\n\nimporter="texture"\n')
        with pytest.raises(pp.TreeAmbiguity, match="without remap outputs"):
            pp._scan_state(str(proj))

    def test_zero_declaration_keep_alone_passes(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        godot = proj / ".godot"
        (godot / "imported").mkdir(parents=True, exist_ok=True)
        _write(godot / "uid_cache.bin", b"u")
        _write(godot / "global_script_class_cache.cfg", "c")
        _write(proj / "d.csv", "k\n1\n")
        _write(proj / "d.csv.import", '[remap]\n\nimporter="keep"\n')
        state = pp._scan_state(str(proj))
        assert state["input_files"] > 0


class TestDepsSectionParsing:
    def test_deps_dest_files_parsed(self, tmp_path):
        """csv_translation 形态：产物声明在 [deps]（[remap] 无 path 行）。"""
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "loc.csv", "k,en\n1,one\n")
        _write(proj / "loc.csv.import",
               '[remap]\n\nimporter="csv_translation"\ntype="Translation"\n\n'
               '[deps]\n\ndest_files=["res://loc.en.translation"]\n')
        root = proj.resolve()
        outs = pp._import_remap_outputs(root, proj / "loc.csv.import")
        assert [o.name for o in outs] == ["loc.en.translation"]

    def test_declared_output_missing_is_ambiguity(self, tmp_path):
        """D2：声明存在产物缺失 → ambiguity（fail-closed）。"""
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "loc.csv.import",
               '[remap]\n\nimporter="csv_translation"\n\n'
               '[deps]\n\ndest_files=["res://loc.en.translation"]\n')
        with pytest.raises(pp.TreeAmbiguity, match="remap output missing"):
            pp._scan_state(str(proj))


class TestNoCertFailClosed:
    def test_unreadable_import_sidecar(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "x.png.import", b"\xff\xfe\x00garbage")
        with pytest.raises(pp.TreeAmbiguity, match="unreadable|importer"):
            pp._scan_state(str(proj))

    def test_importer_line_missing(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "x.png.import", '[remap]\n\npath="res://x.png"\n')
        with pytest.raises(pp.TreeAmbiguity, match="without importer field"):
            pp._scan_state(str(proj))

    def test_symlink_in_tree(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        os.symlink(proj / "src.gd", proj / "link.gd")
        with pytest.raises(pp.TreeAmbiguity, match="symlink"):
            pp._scan_state(str(proj))

    def test_pre_import_scan_without_cert_not_required(
            self, tmp_path, sealed):
        """无证（首次导入前）不扫描即导入——S0 检查仅在有证热路径发生。"""
        proj = _make_project(tmp_path)
        r = pp.prewarm_with_proof(str(proj))
        assert r["decision"] == "imported_certified"


class TestG12ChainResolution:
    def _chain_project(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        proj = _make_project(tmp_path)
        bin_dir = proj / "bin"
        bin_dir.mkdir()
        outer = bin_dir / "outer"
        inner = bin_dir / "inner"
        fin = bin_dir / "fin"
        outer.write_text(f'#!/bin/sh\nexec "{inner}" "$@"\n')
        inner.write_text(f'#!/bin/sh\nexec "{fin}" "$@"\n')
        for p in (outer, inner):
            p.chmod(0o755)
        return proj, outer, inner, fin

    def test_chain_resolves_to_final_binary(self, tmp_path):
        proj, outer, inner, fin = self._chain_project(tmp_path)
        fin.write_bytes(b"\x7fELF\x00final-binary-payload")  # 含 NUL = 实体
        fin.chmod(0o755)
        chain = pp._resolve_chain(outer)
        assert [c.name for c in chain] == ["outer", "inner", "fin"]

    def test_shebang_not_selected_as_target(self, tmp_path):
        """B2 存活根因回归钉：shebang 解释器不得充当链目标截断链。"""
        proj, outer, inner, fin = self._chain_project(tmp_path)
        fin.write_bytes(b"\x7fELF-final-binary")
        fin.chmod(0o755)
        targets = pp._godot_targets(outer)
        assert [t.name for t in targets] == ["inner"]

    def test_unresolvable_chain_is_no_cert(self, tmp_path):
        """链无法判定终端实体（变量化路径）→ TreeAmbiguity → NO_CERT。"""
        proj, outer, inner, fin = self._chain_project(tmp_path)
        inner.write_text('#!/bin/sh\nexec "$ENGINE" "$@"\n')
        with pytest.raises(pp.TreeAmbiguity,
                           match="cannot reach terminal binary"):
            pp._resolve_chain(outer)

    def test_chain_cycle_is_no_cert(self, tmp_path):
        proj, outer, inner, fin = self._chain_project(tmp_path)
        outer.write_text(f'#!/bin/sh\nexec "{inner}" "$@"\n')
        inner.write_text(f'#!/bin/sh\nexec "{outer}" "$@"\n')
        with pytest.raises(pp.TreeAmbiguity, match="cycle"):
            pp._resolve_chain(outer)

    def test_final_binary_swap_detected_via_hot_check(
            self, tmp_path, monkeypatch, capsys):
        """B2 存活例：fin 换内容 → chain_sha256 失配 → 旧证拒签。"""
        proj, outer, inner, fin = self._chain_project(tmp_path)
        fin.write_bytes(b"\x7fELF\x00final-binary-payload")
        fin.chmod(0o755)
        monkeypatch.setenv("GQT_GODOT_BIN", str(outer))
        identity = pp._engine_identity(check_version=False)
        pp._publish_proof(str(proj), {
            "input_digest": "i" * 64, "input_files": 1,
            "output_digest": "o" * 64, "output_files": 1,
        }, identity, "proof-chain-1")
        cert, reason = pp._load_proof(str(proj))
        assert cert is not None and reason is None
        fin.write_bytes(b"\x7fELF\x00swapped-to-something-else")
        cert2, reason2 = pp._load_proof(str(proj))
        assert cert2 is None
        assert "chain_sha256" in reason2


class TestProviderRegistry:
    def test_unknown_provider_blocks_cert(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "z.png", b"\x89PNG\r\n\x1a\n")
        _write(proj / "z.png.import",
               '[remap]\n\nimporter="exotic"\npath="res://z.ctex"\n')
        _write(proj / "z.ctex", b"art")
        files = pp._walk_files(proj.resolve())
        providers, unsupported = pp._enumerate_providers(
            proj.resolve(), files)
        assert unsupported is not None
        assert "exotic" in unsupported

    def test_editor_import_plugin_blocks_cert(self, tmp_path):
        proj = _make_project(tmp_path, with_imports=False)
        _write(proj / "my_importer.gd",
               "extends EditorImportPlugin\nfunc _import():\n\tpass\n")
        files = pp._walk_files(proj.resolve())
        providers, unsupported = pp._enumerate_providers(
            proj.resolve(), files)
        assert unsupported is not None
        assert "import plugin" in unsupported
