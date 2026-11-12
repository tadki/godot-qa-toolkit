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
    shadow, _disc = _prepare_shadow(project, "r25shadow", 0)
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


# ------------------------------------------------- §SPEC-071 H1 symlink 写穿

def test_shadow_dereferences_symlinks_zero_write_through(
        project, tmp_path):
    """§SPEC-071 H1（Revy R26-H1 反例闭合）：shadow 内任意复制的 symlink
    （绝对/相对）解引用为常规文件/实拷贝——运行期写零写穿 base；悬空
    链接移除并经 disclosure 显式披露。"""
    victim = tmp_path / "victim.gd"
    victim.write_text("BASE-ORIGINAL")
    base_file = project / "src_game" / "cell.gd"
    # 绝对 symlink → base victim（R26-H1 原反例）
    (project / "src_game" / "linked.gd").symlink_to(victim)
    # 相对 symlink → project 内另一文件
    (project / "src_game" / "rel.gd").symlink_to("cell.gd")
    # 悬空 symlink
    (project / "src_game" / "dangling.gd").symlink_to(
        tmp_path / "nonexistent.gd")

    shadow, disc = _prepare_shadow(project, "r27shadow", 0)
    try:
        assert disc["symlinks_dereferenced"] >= 2
        assert disc["dangling_links_removed"] >= 1
        # shadow 内零 symlink 残留
        assert not any(p.is_symlink() for p in shadow.rglob("*"))
        # 原反例路径：写入不再写穿 base victim
        linked = shadow / "src_game" / "linked.gd"
        assert linked.is_file() and not linked.is_symlink()
        linked.write_text("PWNED")
        assert victim.read_text() == "BASE-ORIGINAL"
        # 相对链接同样解引用为自有 inode
        relf = shadow / "src_game" / "rel.gd"
        relf.write_text("REL-WRITE")
        assert base_file.read_text() == TARGET_SRC
        # 悬空链接已移除
        assert not (shadow / "src_game" / "dangling.gd").exists()
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
    assert counts.get("block_entry_approximation", 0) \
        == sem["block_compressed_files"]
    if counts.get("statement_start_line") and \
            counts.get("block_entry_approximation"):
        assert crap["coverage_semantics"] == "mixed"
    else:
        assert crap["coverage_semantics"] == (
            "block_entry_approximation"
            if counts.get("block_entry_approximation")
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
    """O_CREAT|O_EXCL|O_NOFOLLOW + 0600 + 随机后缀（经 pinned dirfd）：
    临时名不可抢注、不跟随 symlink、replace 后零残留、终态 0600。"""
    from godot_qa_toolkit.canonical.orchestrator import \
        _atomic_write_via_dir_fd
    src_dir = project / "src_game"
    target = src_dir / "cell.gd"
    dir_fd = os.open(src_dir, os.O_RDONLY | os.O_NOFOLLOW
                     | os.O_DIRECTORY)
    try:
        _atomic_write_via_dir_fd(dir_fd, "cell.gd", "ATOMIC-CONTENT")
    finally:
        os.close(dir_fd)
    assert target.read_text() == "ATOMIC-CONTENT"
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(src_dir.glob(".*gqt-tmp*")) == []


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


# ---------------------------------------- §SPEC-071 H2 竞态注入（确定性）

def test_recovery_parent_symlink_swap_outside_zero_change(
        project, tmp_path):
    """§SPEC-071 H2（Revy R26-H2 注入形态，确定性）：恢复时刻 src_game
    已被换为指向 root 外目录的 symlink——预过滤 resolve 即越界，记录整体
    跳过，root 外文件零改动。"""
    import shutil as _sh
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_cell = outside / "cell.gd"
    outside_cell.write_text("OUTSIDE-ORIGINAL")

    base = _backup_base()
    d = base / "r27-race-outside"
    d.mkdir(mode=0o700)
    (d / "started.json").write_text(json.dumps({
        "run_uuid": "r27-race-outside",
        "project_root": str(project),
        "files": {str(project / "src_game" / "cell.gd"): "REWRITTEN"},
    }), encoding="utf-8")

    real_dir = project / "src_game_real"
    _sh.move(str(project / "src_game"), str(real_dir))
    (project / "src_game").symlink_to(outside)
    try:
        restored = _recover_stale_backups(project)
        assert restored == 0
        assert outside_cell.read_text() == "OUTSIDE-ORIGINAL"
    finally:
        (project / "src_game").unlink()
        _sh.move(str(real_dir), str(project / "src_game"))
        _sh.rmtree(d, ignore_errors=True)


def test_recovery_in_root_symlink_component_resolves_in_root(
        project, tmp_path):
    """§SPEC-071 H2：组件 symlink 解析后在 root 内——resolve 规范化把
    恢复落点映射到 root 内 resolved 位置（写入落点在 root 内，root 外
    零改动）；resolve 之后沿 resolved 路线插入的 symlink 会被 dirfd
    逐组件 O_NOFOLLOW 走廊 ELOOP 拒绝（无 pathname 重解析窗口）。"""
    import shutil as _sh
    internal = project / "internal"
    internal.mkdir()
    internal_cell = internal / "cell.gd"
    internal_cell.write_text("INTERNAL-ORIGINAL")

    base = _backup_base()
    d = base / "r27-race-inside"
    d.mkdir(mode=0o700)
    (d / "started.json").write_text(json.dumps({
        "run_uuid": "r27-race-inside",
        "project_root": str(project),
        "files": {str(project / "src_game" / "cell.gd"): "REWRITTEN"},
    }), encoding="utf-8")

    real_dir = project / "src_game_real"
    _sh.move(str(project / "src_game"), str(real_dir))
    (project / "src_game").symlink_to(internal)
    try:
        restored = _recover_stale_backups(project)
        # 恢复经 resolved 路线落在 root 内位置；root 外零改动
        assert restored == 1
        assert internal_cell.read_text() == "REWRITTEN"
        outside_probe = tmp_path / "outside-probe.txt"
        assert not outside_probe.exists()
    finally:
        (project / "src_game").unlink()
        _sh.move(str(real_dir), str(project / "src_game"))
        _sh.rmtree(d, ignore_errors=True)


# ---------------------------------------- §SPEC-071 L1 marker schema

def test_recovery_skips_malformed_markers_and_does_not_block(
        project, tmp_path):
    """§SPEC-071 L1：marker 根非 object / created 非 list / project_godot
    非 str 等非法形态 → 不可信记录跳过（不写不删、无裸异常），且不阻塞
    后续合法恢复。"""
    base = _backup_base()
    cell = project / "src_game" / "cell.gd"
    cell.write_text("CURRENT")

    d_bad_list = base / "r27-l1-list"
    d_bad_list.mkdir(mode=0o700)
    (d_bad_list / "started.json").write_text('["not", "a", "dict"]',
                                             encoding="utf-8")
    d_bad_created = base / "r27-l1-created"
    d_bad_created.mkdir(mode=0o700)
    (d_bad_created / "started.json").write_text(json.dumps({
        "run_uuid": "r27-l1-created", "project_root": str(project),
        "files": {str(cell): "REWRITTEN"}, "created": 7,
    }), encoding="utf-8")
    d_bad_pg = base / "r27-l1-pg"
    d_bad_pg.mkdir(mode=0o700)
    (d_bad_pg / "started.json").write_text(json.dumps({
        "run_uuid": "r27-l1-pg", "project_root": str(project),
        "files": {str(cell): "REWRITTEN"}, "project_godot": 7,
    }), encoding="utf-8")
    d_good = base / "r27-l1-good"
    d_good.mkdir(mode=0o700)
    (d_good / "started.json").write_text(json.dumps({
        "run_uuid": "r27-l1-good", "project_root": str(project),
        "files": {str(cell): "ORIGINAL"},
        "created": [str(project / "addons" / "gqt_cov_flusher.gd")],
    }), encoding="utf-8")

    restored = _recover_stale_backups(project)
    assert restored == 1  # 仅合法记录恢复，坏 marker 不阻塞
    assert cell.read_text() == "ORIGINAL"
    assert not (project / "addons" / "gqt_cov_flusher.gd").exists()
    for d in (d_bad_list, d_bad_created, d_bad_pg):
        assert d.exists()  # 不可信记录保留痕迹
        import shutil as _sh
        _sh.rmtree(d, ignore_errors=True)


# ---------------------------------------- §SPEC-071 L2 ghost 回放语义

def test_ghost_replay_semantics_reconciled_with_current_form():
    """§SPEC-071 L2：ghost 回放 record/crap_line 的 coverage_semantics 与
    本轮该文件实测形态对账；rel 不在本轮测量面时保留 baseline 原值并
    显式标注 carried。"""
    from godot_qa_toolkit.canonical.orchestrator import (
        _crap_replay_vanished_baseline,
        _SEM_BLOCK,
        _SEM_STATEMENT,
    )
    prepared = {
        "src_game/blocky.gd": {"block_map": {0: [1, 2]}},
        "src_game/plain.gd": {"block_map": {}},
    }
    baseline_records = {
        "src_game/blocky.gd::f": {
            "function_id": "src_game/blocky.gd::f",
            "score_status": "scored",
            "coverage_semantics": _SEM_STATEMENT,
            "crap_line": {"score": 1.0,
                          "coverage_semantics": _SEM_STATEMENT},
        },
        "src_game/gone.gd::g": {
            "function_id": "src_game/gone.gd::g",
            "score_status": "scored",
            "coverage_semantics": _SEM_STATEMENT,
            "crap_line": {"score": 2.0,
                          "coverage_semantics": _SEM_STATEMENT},
        },
    }
    records: list = []
    lanes: dict = {}
    _crap_replay_vanished_baseline(records, lanes, baseline_records,
                                   emitted_fids=set(), prepared=prepared)
    by_fid = {r["function_id"]: r for r in records}
    blocky = by_fid["src_game/blocky.gd::f"]
    assert blocky["coverage_semantics"] == _SEM_BLOCK
    assert blocky["crap_line"]["coverage_semantics"] == _SEM_BLOCK
    gone = by_fid["src_game/gone.gd::g"]
    assert gone.get("coverage_semantics_carried_from_baseline") is True
    assert gone["coverage_semantics"] == _SEM_STATEMENT
    assert gone["crap_line"]["coverage_semantics"] == _SEM_STATEMENT
    assert lanes["unstable"] == 2


# ---------------------------------------- §SPEC-071 L3 文档/负行号

def test_docs_and_negative_line_ids_aligned():
    """§SPEC-071 L3：docs 与 orchestrator docstring 均为 per-uid 私有
    备份根；负整数/负整值浮点行号一律 False（与 docstring 对齐）。"""
    from godot_qa_toolkit.canonical.orchestrator import _is_baseline_line_id
    assert _is_baseline_line_id(-1) is False
    assert _is_baseline_line_id(-2.0) is False
    assert _is_baseline_line_id(0) is True
    assert _is_baseline_line_id(3) is True
    assert _is_baseline_line_id("12") is True
    assert _is_baseline_line_id("abc") is False
    repo = Path(__file__).resolve().parents[2]
    contract = (repo / "docs" / "contract.md").read_text(encoding="utf-8")
    assert "gqt-canonical-backups-<uid>" in contract
    assert "/tmp/gqt-canonical-backups/" not in contract
    mod_src = (repo / "src" / "godot_qa_toolkit" / "canonical"
               / "orchestrator.py").read_text(encoding="utf-8")
    assert "gqt-canonical-backups-<uid>" in mod_src
    assert "/tmp/gqt-canonical-backups/" not in mod_src


# ------------------------- §SPEC-072 快照无关 no-write-through（真实序列）

def test_shadow_symlink_decoupled_from_any_snapshot(project, tmp_path):
    """§SPEC-072（Revy R27 残余真实序列闭合）：首次 shadow 建立后才向
    base 新增 symlink——第二次 shadow 仍解引用（无跨 run 缓存可陈旧），
    disclosure 非零且准确，写入零写穿 base。"""
    victim = tmp_path / "victim.gd"
    victim.write_text("BASE-ORIGINAL")
    base_cell = project / "src_game" / "cell.gd"

    sh1, disc1 = _prepare_shadow(project, "r28seq", 0)
    assert disc1 == {"symlinks_dereferenced": 0,
                     "dangling_links_removed": 0}
    import shutil as _sh
    _sh.rmtree(sh1, ignore_errors=True)

    # 真实代码序列：预扫时点之后新增 symlink（同进程二次 run 情景）
    (project / "src_game" / "linked.gd").symlink_to(victim)

    sh2, disc2 = _prepare_shadow(project, "r28seq", 1)
    try:
        assert disc2["symlinks_dereferenced"] >= 1
        assert disc2["dangling_links_removed"] == 0
        assert not any(p.is_symlink() for p in sh2.rglob("*"))
        linked = sh2 / "src_game" / "linked.gd"
        assert linked.is_file() and not linked.is_symlink()
        linked.write_text("PWNED")
        assert victim.read_text() == "BASE-ORIGINAL"
        assert base_cell.read_text() == TARGET_SRC
    finally:
        _sh.rmtree(sh2, ignore_errors=True)


def test_shadow_dereferences_links_inside_godot_cache(project, tmp_path):
    """§SPEC-072：`.godot` 内的 symlink 同样被 post-copy 检测覆盖
    （旧实现按预扫剪枝 .godot，属与契约相关的缺口——现关闭）。"""
    victim = tmp_path / "cache-victim.txt"
    victim.write_text("CACHE-ORIGINAL")
    cache = project / ".godot" / "imported"
    cache.mkdir(parents=True)
    (cache / "linked.dat").symlink_to(victim)

    shadow, disc = _prepare_shadow(project, "r28godot", 0)
    try:
        assert disc["symlinks_dereferenced"] >= 1
        assert not any(p.is_symlink() for p in (shadow / ".godot").rglob("*"))
        written = shadow / ".godot" / "imported" / "linked.dat"
        written.write_text("PWNED")
        assert victim.read_text() == "CACHE-ORIGINAL"
    finally:
        import shutil as _sh
        _sh.rmtree(shadow, ignore_errors=True)


def test_shadow_removes_late_dangling_link_and_discloses(project, tmp_path):
    """§SPEC-072：首次 shadow 后新增的悬空链接在下次 shadow 中被移除并
    计入 disclosure（行为确定、计数真实）。"""
    sh1, _ = _prepare_shadow(project, "r28dang", 0)
    import shutil as _sh
    _sh.rmtree(sh1, ignore_errors=True)
    (project / "src_game" / "dangling.gd").symlink_to(
        tmp_path / "nonexistent.gd")
    sh2, disc = _prepare_shadow(project, "r28dang", 1)
    try:
        assert disc["dangling_links_removed"] >= 1
        assert not (sh2 / "src_game" / "dangling.gd").exists()
        assert not any(p.is_symlink() for p in sh2.rglob("*"))
    finally:
        _sh.rmtree(sh2, ignore_errors=True)


# ------------------------- §SPEC-073 目录链接解引用语义（R28 反例闭合）

def test_shadow_dereferences_dir_link_with_content_alive(project):
    """§SPEC-073（Revy R28-HIGH 反例闭合）：有效目录 symlink 解引用为
    保留全部内容的实拷贝目录，计入 symlinks_dereferenced——不得静默
    删除或虚报 dangling。"""
    base_dir = project / "other"
    base_dir.mkdir()
    (base_dir / "f.txt").write_text("TARGET")
    (project / "src_game" / "dirlink").symlink_to(base_dir)

    shadow, disc = _prepare_shadow(project, "r29dir", 0)
    try:
        link = shadow / "src_game" / "dirlink"
        assert link.is_dir() and not link.is_symlink(), (
            "有效目录链接必须解引用为实拷贝目录（不得删除）")
        assert (link / "f.txt").read_text() == "TARGET", (
            "目录链接内容必须存活")
        assert disc["symlinks_dereferenced"] == 1
        assert disc["dangling_links_removed"] == 0
    finally:
        import shutil as _sh
        _sh.rmtree(shadow, ignore_errors=True)


def test_shadow_dir_link_inner_links_resolved_and_disclosed(
        project, tmp_path):
    """§SPEC-073 内层边界：目录链接内含文件链接 → 内层解引用计数；
    内含悬空链接 → 内层移除并计入 dangling（确定性披露）；外层目录
    内容存活。"""
    base_dir = project / "outer"
    base_dir.mkdir()
    (base_dir / "keep.txt").write_text("KEEP")
    inner_target = tmp_path / "inner-target.gd"
    inner_target.write_text("INNER-TARGET")
    (base_dir / "inner_file.gd").symlink_to(inner_target)
    (base_dir / "inner_dangling.gd").symlink_to(
        tmp_path / "nonexistent.gd")
    (project / "src_game" / "dirlink").symlink_to(base_dir)

    shadow, disc = _prepare_shadow(project, "r29inner", 0)
    try:
        link = shadow / "src_game" / "dirlink"
        assert link.is_dir() and not link.is_symlink()
        assert (link / "keep.txt").read_text() == "KEEP"
        inner_file = link / "inner_file.gd"
        assert inner_file.is_file() and not inner_file.is_symlink(), (
            "内层文件链接必须解引用为自有 inode")
        assert inner_file.read_text() == "INNER-TARGET"
        assert not (link / "inner_dangling.gd").exists(), (
            "内层悬空链接必须移除")
        # 计数 = 实际处理的 shadow symlink 实例数：同一 base 文件经
        # outer/inner_file.gd 与 dirlink/inner_file.gd 两条 shadow 路径
        # 各出现一次 → 各解引用一次；outer/inner_dangling.gd 与
        # dirlink 内层悬空各移除一次
        assert disc["symlinks_dereferenced"] == 3
        assert disc["dangling_links_removed"] == 2
        assert all(not p.is_symlink() for p in link.rglob("*"))
    finally:
        import shutil as _sh
        _sh.rmtree(shadow, ignore_errors=True)


def test_shadow_symlink_loop_goes_to_dangling_bucket(project):
    """§SPEC-073：循环链接属于「真正不可解析」→ 移除并计入 dangling。"""
    (project / "src_game" / "loop.gd").symlink_to(
        project / "src_game" / "loop.gd")
    shadow, disc = _prepare_shadow(project, "r29loop", 0)
    try:
        assert disc["dangling_links_removed"] == 1
        assert disc["symlinks_dereferenced"] == 0
        assert not (shadow / "src_game" / "loop.gd").is_symlink()
    finally:
        import shutil as _sh
        _sh.rmtree(shadow, ignore_errors=True)


def test_shadow_deref_copy_failure_is_fail_loud(project, tmp_path):
    """§SPEC-073：解引用阶段的内容拷贝失败必须 fail-loud
    （CanonicalError）——不得静默删除有效链接或虚报计数。cp 阶段只复制
    链接本身（无需访问目标），deref copyfile 跟随读取不可读目标时才失败。"""
    import shutil as _sh
    if hasattr(os, "getuid") and os.getuid() == 0:
        return  # root 环境无法构造读取障碍
    # 目标放 project 外：cp 只复制链接本身（无需访问目标），deref
    # copyfile 跟随读取不可读目标时才失败（精确命中 CanonicalError 路径）
    locked_file = tmp_path / "locked-target.gd"
    locked_file.write_text("SECRET")
    (project / "src_game" / "locked.gd").symlink_to(locked_file)
    os.chmod(locked_file, 0o000)
    try:
        with pytest.raises(CanonicalError, match="dereference failed"):
            _prepare_shadow(project, "r29lock", 0)
    finally:
        os.chmod(locked_file, 0o644)
