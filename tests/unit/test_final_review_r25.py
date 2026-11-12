"""SEE-1562 R25 — §SPEC-070 Final Review 阻断修复。

FR-1 备份/恢复信任边界（私有备份根 + ownership/mode + root 限定 +
symlink 拒绝 + 原子恢复）；FR-2 shadow 不与 base 共享可写 inode；
FR-3 root 外目标转 CanonicalError；FR-4 块压缩近似显式披露；
FR-5 全部 LOW（union:null 重复身份、arm_failures 死代码、diagnose
ok/--godot-bin、sink_path、str 行号契约、docs 旧阈值）。
"""

from __future__ import annotations

import io
import json
import os
import subprocess as sp
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from godot_qa_toolkit.canonical.orchestrator import (
    CanonicalError,
    _backup_base,
    _load_baseline_report,
    _prepare_shadow,
    _recover_stale_backups,
    _rel_within_root,
    run_canonical,
)
from godot_qa_toolkit.canonical.recipe import build_recipe
from godot_qa_toolkit.cli import build_parser

from .test_canonical_report_see1546 import FakeGodot

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig = sp.run
    yield
    sp.run = orig


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="fr25-fixture"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n")
    feats = root / "acceptance" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(TARGET_SRC)
    return root


def _recipe(project: Path) -> dict:
    recipe, error = build_recipe(project)
    assert error is None
    return recipe


def _targets(project: Path) -> list[str]:
    return sorted(str(p) for p in (project / "src_game").glob("*.gd"))


# ------------------------------------------------- FR-1 备份/恢复信任边界

def test_backup_base_is_private_mode_0700():
    base = _backup_base()
    assert base.is_dir()
    assert not base.is_symlink()
    st = base.stat()
    assert st.st_mode & 0o077 == 0, oct(st.st_mode & 0o777)
    if hasattr(os, "getuid"):
        assert st.st_uid == os.getuid()


def test_recovery_rejects_out_of_root_and_symlink_targets(
        project, tmp_path):
    """files/created 中的 root 外路径与 symlink 叶子 → 记录整体跳过，
    任意覆写/删除不可达；root 内合法条目单独成记录时正常恢复。"""
    outside = tmp_path / "outside.gd"
    outside.write_text("OUTSIDE-ORIGINAL")
    evil = tmp_path / "evil-target.gd"
    evil.write_text("EVIL-ORIGINAL")
    link = project / "src_game" / "link.gd"
    link.write_text("LINK-ORIGINAL")
    link_target = tmp_path / "link-victim.txt"
    link_target.write_text("VICTIM")
    link.unlink()
    link.symlink_to(link_target)

    base = _backup_base()
    d = base / "r25-stale-evil"
    d.mkdir(mode=0o700)
    (d / "started.json").write_text(json.dumps({
        "run_uuid": "r25-stale-evil",
        "project_root": str(project),
        "files": {
            str(project / "src_game" / "cell.gd"): "REWRITTEN",
            str(outside): "REWRITTEN",
            str(evil): "REWRITTEN",
            str(link): "REWRITTEN",
        },
        "created": [str(tmp_path / "created-evil.txt")],
    }), encoding="utf-8")

    restored = _recover_stale_backups(project)
    assert restored == 0  # 含越界/ symlink 条目的记录整体拒绝
    assert (project / "src_game" / "cell.gd").read_text() == TARGET_SRC
    assert outside.read_text() == "OUTSIDE-ORIGINAL"
    assert evil.read_text() == "EVIL-ORIGINAL"
    assert link_target.read_text() == "VICTIM"
    assert d.exists()  # 不可信记录不删除（不掩盖篡改痕迹）
    import shutil as _sh
    _sh.rmtree(d)


def test_recovery_restores_contained_record_atomically(project):
    base = _backup_base()
    d = base / "r25-stale-clean"
    d.mkdir(mode=0o700)
    (d / "started.json").write_text(json.dumps({
        "run_uuid": "r25-stale-clean",
        "project_root": str(project),
        "files": {str(project / "src_game" / "cell.gd"): "ORIGINAL"},
        "created": [str(project / "addons" / "gqt_cov_flusher.gd")],
    }), encoding="utf-8")
    (project / "src_game" / "cell.gd").write_text("INSTRUMENTED")
    created = project / "addons" / "gqt_cov_flusher.gd"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("finalizer")

    restored = _recover_stale_backups(project)
    assert restored == 1
    assert (project / "src_game" / "cell.gd").read_text() == "ORIGINAL"
    assert not created.exists()
    assert not d.exists()
    leftovers = list(project.glob("src_game/.*gqt-tmp*"))
    assert leftovers == []  # 原子写零临时残留


def test_recovery_skips_foreign_writable_dir(project, tmp_path):
    """备份目录开放组/他者写位（或属主非当前用户）→ 不信任、不恢复。"""
    base = _backup_base()
    d = base / "r25-stale-loose"
    d.mkdir(mode=0o700, exist_ok=True)
    (d / "started.json").write_text(json.dumps({
        "run_uuid": "r25-stale-loose",
        "project_root": str(project),
        "files": {str(project / "src_game" / "cell.gd"): "REWRITTEN"},
    }), encoding="utf-8")
    os.chmod(d, 0o777)
    try:
        restored = _recover_stale_backups(project)
        assert restored == 0
        assert (project / "src_game" / "cell.gd").read_text() == TARGET_SRC
        assert d.exists()
    finally:
        import shutil as _sh
        os.chmod(d, 0o700)
        _sh.rmtree(d, ignore_errors=True)  # 共享私有根不留给其他测试


# --------------------------------------------------- FR-2 shadow inode 隔离

def test_shadow_does_not_share_writable_inodes(project):
    shadow = _prepare_shadow(project, "r25shadow", 0)
    try:
        base_file = project / "src_game" / "cell.gd"
        shadow_file = shadow / "src_game" / "cell.gd"
        assert os.stat(shadow_file).st_nlink == 1, (
            "shadow 不得与 base 共享 inode（cp -al hardlink farm 已禁用）")
        shadow_file.write_text("RUNTIME-WRITE")
        assert base_file.read_text() == TARGET_SRC, (
            "shadow 运行期写不得写穿 base")
    finally:
        import shutil as _sh
        _sh.rmtree(shadow, ignore_errors=True)


# ------------------------------------------------------------- FR-3

def test_rel_within_root_raises_domain_error(project):
    outside = str(project.parent / "out.gd")
    with pytest.raises(CanonicalError, match="outside project root"):
        _rel_within_root(outside, project)
    assert _rel_within_root(str(project / "src_game" / "cell.gd"),
                            project) == "src_game/cell.gd"


def test_run_canonical_root_external_target_is_domain_error(
        project, tmp_path):
    outside = tmp_path / "out.gd"
    outside.write_text("extends RefCounted\nfunc x():\n\treturn 1\n")
    with pytest.raises(CanonicalError, match="outside project root"):
        run_canonical(project, [str(outside)], _recipe(project))


def test_cli_root_external_target_emits_tool_json(project, tmp_path):
    outside_dir = tmp_path / "outside-root"
    outside_dir.mkdir()
    (outside_dir / "out.gd").write_text(
        "extends RefCounted\nfunc x():\n\treturn 1\n")
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(_recipe(project)), encoding="utf-8")
    args = build_parser().parse_args([
        "canonical", str(outside_dir),
        "--project-root", str(project),
        "--recipe", str(recipe_path), "--out", str(tmp_path / "out.json")])
    orig = sp.run
    sp.run = FakeGodot(project)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = args.func(args)
    finally:
        sp.run = orig
    assert code == 1
    payload = json.loads(buf.getvalue())
    assert payload["tool"] == "canonical" and payload["ok"] is False
    assert payload["summary"]["canonical_error"] is True
    assert "outside project root" in payload["summary"]["error"]


# ------------------------------------------------------------- FR-4

def test_report_discloses_block_semantics(project, tmp_path):
    # CI 无 godot PATH：GUT 腿必须由 FakeGodot 驱动（不得依赖本机二进制）
    sp.run = FakeGodot(project)
    report = run_canonical(project, _targets(project), _recipe(project))
    sem = report["coverage_semantics"]
    assert sem["block_compressed_files"] >= 1
    assert sem["statement_files"] >= 0
    assert "approximation" in sem["semantics"]
    assert "not per-statement exact" in sem["semantics"]
    # §SPEC-070/R26 FR-4 record 级闭合：块压缩文件的每条 record 与
    # crap_line 均为 block_entry_approximation，不再冒充精确
    # statement_start_line；statement 文件保持精确语义；顶层为 mixed
    crap = report["crap"]
    counts = crap["coverage_semantics_counts"]
    assert counts["block_entry_approximation"] \
        == sem["block_compressed_files"]
    if counts["statement_start_line"] and counts["block_entry_approximation"]:
        assert crap["coverage_semantics"] == "mixed"
    else:
        assert crap["coverage_semantics"] == (
            "block_entry_approximation"
            if counts["block_entry_approximation"]
            else "statement_start_line")
    by_fid_sem = {(r["function_id"], r["coverage_semantics"],
                  r["crap_line"]["coverage_semantics"])
                  for r in crap["records"]}
    sems = {s for (_f, s, _cl) in by_fid_sem}
    assert sems <= {"block_entry_approximation", "statement_start_line"}
    assert "block_entry_approximation" in sems
    assert all(cl == s for (_f, s, cl) in by_fid_sem)


# ------------------------------------------------------------- FR-5

def test_baseline_duplicate_file_via_null_union_rejected(tmp_path):
    doc = {
        "unions": [
            {"file": "res://a.gd", "union": {"per_fid": [
                {"fid": "f", "covered_line_ids": [1], "total": 1}]}},
            {"file": "res://a.gd", "union": None},
        ],
        "crap": {"records": []},
    }
    p = tmp_path / "b.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CanonicalError, match="duplicate"):
        _load_baseline_report(str(p))


def test_baseline_non_digit_string_line_id_rejected(tmp_path):
    doc = {
        "unions": [{"file": "res://a.gd", "union": {"per_fid": [
            {"fid": "f", "covered_line_ids": ["abc"], "total": 1}]}}],
        "crap": {"records": []},
    }
    p = tmp_path / "b.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CanonicalError, match="covered_line_ids"):
        _load_baseline_report(str(p))
    # 数字字符串仍合法（sink 落盘形态）
    doc["unions"][0]["union"]["per_fid"][0]["covered_line_ids"] = ["0", "1"]
    p.write_text(json.dumps(doc), encoding="utf-8")
    lines, _ = _load_baseline_report(str(p))
    assert lines["res://a.gd"]["f"] == (frozenset({"0", "1"}), 1)


def test_diagnose_gate_flags_timed_out_feature():
    from godot_qa_toolkit.cli import _diagnose_gate
    ok, failures = _diagnose_gate({"features": [
        {"key": "a", "instrumented": {"timed_out": False}},
        {"key": "b", "instrumented": {"timed_out": True}},
    ]})
    assert ok is False
    assert any("b" in f["reason"] for f in failures)
    ok, failures = _diagnose_gate({"features": [
        {"key": "a", "instrumented": {"timed_out": False}}]})
    assert ok is True and failures == []


def test_cli_diagnose_forwards_godot_bin(project, tmp_path):
    """--godot-bin 不再是死旗标：显式非法路径在此早验证并转统一 JSON
    契约（ok=false），先于任何 GUT 执行。"""
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(_recipe(project)), encoding="utf-8")
    args = build_parser().parse_args([
        "canonical-diagnose", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(recipe_path), "--features", "demo",
        "--out", str(tmp_path / "diag.json"),
        "--godot-bin", str(tmp_path / "nonexistent-godot")])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    assert code == 1
    payload = json.loads(buf.getvalue())
    assert payload["tool"] == "canonical-diagnose"
    assert payload["ok"] is False


def test_canonical_evidence_record_has_no_phantom_sink_path(
        project, tmp_path):
    # CI 无 godot PATH：GUT 腿必须由 FakeGodot 驱动
    sp.run = FakeGodot(project)
    evidence = tmp_path / "evidence"
    run_canonical(project, _targets(project), _recipe(project),
                  evidence_out=str(evidence))
    records = [p for p in evidence.glob("*.json")
               if "run_record" in p.name or "record" in p.name]
    assert records, "evidence run records must exist"
    for p in records:
        doc = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and "run_uuid" in doc:
            assert "sink_path" not in doc, (
                "canonical evidence record 不得携带指向不存在位置的 sink_path")


# ---------------------------------------- R26：FR-1 残余加固反例

def test_atomic_write_excl_0600_no_leftover(project):
    """O_CREAT|O_EXCL|O_NOFOLLOW + 0600 + 随机后缀：临时名不可抢注、
    不跟随 symlink、replace 后零残留、终态 0600。"""
    from godot_qa_toolkit.canonical.orchestrator import _atomic_write_text
    target = project / "src_game" / "cell.gd"
    _atomic_write_text(target, "ATOMIC-CONTENT")
    assert target.read_text() == "ATOMIC-CONTENT"
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(project.glob("src_game/.*gqt-tmp*")) == []
    # 目标位置为 symlink 时：替换 symlink 本身（不跟随写穿目标文件）
    victim = tmp_path2 = project.parent / "r26-victim.txt"
    victim.write_text("VICTIM")
    link = project / "src_game" / "linked.gd"
    link.symlink_to(victim)
    _atomic_write_text(link, "NEW-CONTENT")
    assert victim.read_text() == "VICTIM"  # 未写穿
    assert link.read_text() == "NEW-CONTENT"  # symlink 被常规文件替换


def test_backup_base_rejects_symlink(monkeypatch):
    base_parent = Path(tempfile.gettempdir())
    uid = os.getuid()
    base = base_parent / f"gqt-canonical-backups-{uid}"
    import shutil as _sh
    real_dir = base.parent / f"gqt-r26-real-{os.getpid()}"
    real_dir.mkdir(mode=0o700, exist_ok=True)
    if base.is_symlink() or base.exists():
        # 保护性清理既有 base（测试专用沙箱语义；lstat 校验会拒绝 symlink）
        if base.is_symlink():
            base.unlink()
        elif base.is_dir():
            _sh.rmtree(base)
        else:
            base.unlink()
    base.symlink_to(real_dir)
    try:
        with pytest.raises(CanonicalError, match="private directory|not a"):
            _backup_base()
    finally:
        base.unlink()
        _sh.rmtree(real_dir, ignore_errors=True)


def test_backup_base_no_uid_fails_loud(monkeypatch):
    """无 uid 的非 POSIX 平台：显式 CanonicalError，不做不安全回退。"""
    monkeypatch.delattr(os, "getuid", raising=False)
    with pytest.raises(CanonicalError, match="POSIX uid"):
        _backup_base()


def test_backup_base_rejects_tampered_mode():
    base = _backup_base()
    import shutil as _sh
    os.chmod(base, 0o755)
    try:
        with pytest.raises(CanonicalError, match="not private"):
            _backup_base()
    finally:
        os.chmod(base, 0o700)
