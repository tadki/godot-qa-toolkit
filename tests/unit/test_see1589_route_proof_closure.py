"""SEE-1589 B §SPEC-001/§SPEC-002 依据：路由闭包精度 + 证明摘要对齐。

两处代码级根因判据：
1. 注释/字符串里的名字不是 compile-time 引用边——计入会让闭包虚胖并
   （经 autoload 链）把可 worker 的真实目标误判冷；真实标识符引用仍计边。
2. `prewarm_proof._scan_state` 与 `shadow._populate` 的排除域必须逐项对齐：
   嵌套 `addons/<submodule>/.git` 曾在影子槽被排除、证书摘要却计入，
   导致 guard 恒失配 → 每轮 `proof-drift-persists` + farm 重建。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import prewarm_proof, shadow
from godot_qa_toolkit.mutation.channel_router import (
    CHANNEL_COLD,
    CHANNEL_WORKER,
    compute_reload_closure,
    build_reference_graph,
    route_target,
)


def _write_router_project(tmp_path, consumer_body):
    proj = tmp_path / "router"
    (proj / "autoload").mkdir(parents=True, exist_ok=True)
    (proj / "lib").mkdir(parents=True, exist_ok=True)
    (proj / "project.godot").write_text(
        'config_version=5\n\n[autoload]\n\n'
        'Bus="*res://autoload/bus.gd"\n', encoding="utf-8")
    (proj / "autoload" / "bus.gd").write_text(
        'extends Node\n'
        'const Consumer := preload("res://lib/consumer.gd")\n',
        encoding="utf-8")
    (proj / "lib" / "consumer.gd").write_text(consumer_body, encoding="utf-8")
    (proj / "lib" / "util.gd").write_text(
        "class_name Util\nextends RefCounted\n", encoding="utf-8")
    return proj


def test_comment_mention_is_not_a_compile_time_edge(tmp_path):
    proj = _write_router_project(
        tmp_path,
        "# Util is only named here in a comment — not a reference.\n"
        "extends RefCounted\n")
    decision = route_target(str(proj), "res://lib/util.gd")
    assert decision.channel == CHANNEL_WORKER, decision.reasons
    assert decision.closure == ("res://lib/util.gd",)


def test_string_mention_is_not_a_compile_time_edge(tmp_path):
    proj = _write_router_project(
        tmp_path,
        'extends RefCounted\nconst LABEL := "Util"\n')
    decision = route_target(str(proj), "res://lib/util.gd")
    assert decision.channel == CHANNEL_WORKER, decision.reasons


def test_real_identifier_reference_still_makes_cold(tmp_path):
    proj = _write_router_project(
        tmp_path,
        "extends RefCounted\nvar u = Util.new()\n")
    decision = route_target(str(proj), "res://lib/util.gd")
    assert decision.channel == CHANNEL_COLD, decision.reasons
    assert any("autoload/bus.gd" in r for r in decision.reasons), \
        f"真实引用应经 consumer → autoload 链判冷: {decision.reasons}"


def test_nested_vcs_entries_are_excluded_from_both_digests(tmp_path):
    proj = tmp_path / "kol-like"
    (proj / ".godot" / "imported").mkdir(parents=True)
    (proj / ".godot" / "uid_cache.bin").write_bytes(b"uid")
    (proj / ".godot" / "global_script_class_cache.cfg").write_text("{}")
    (proj / "addons" / "vendored").mkdir(parents=True)
    (proj / "addons" / "vendored" / ".git").write_text(
        "gitdir: ../.git/modules/vendored\n")
    (proj / "addons" / "vendored" / "plugin.gd").write_text(
        "extends Node\n")
    (proj / "nested" / ".qa-cache").mkdir(parents=True)
    (proj / "nested" / ".qa-cache" / "junk.bin").write_bytes(b"x")
    (proj / "project.godot").write_text("config_version=5\n")

    slot_root, _stats = shadow.create_slot(str(proj), "linux")
    try:
        project_state = prewarm_proof._scan_state(str(proj))
        slot_state = prewarm_proof._scan_state(str(slot_root))
        assert project_state["input_digest"] == slot_state["input_digest"]
        assert project_state["input_files"] == slot_state["input_files"]
        assert project_state["output_digest"] == slot_state["output_digest"]
    finally:
        import shutil
        shutil.rmtree(slot_root, ignore_errors=True)


def test_closure_is_reverse_reachable_set(tmp_path):
    proj = _write_router_project(
        tmp_path,
        "extends RefCounted\nvar u = Util.new()\n")
    graph = build_reference_graph(str(proj))
    closure = compute_reload_closure(graph, "res://lib/util.gd")
    assert "res://lib/consumer.gd" in closure
    assert "res://autoload/bus.gd" in closure


def test_relink_same_inode_leaves_no_temp_file(tmp_path):
    """SEE-1589 B 回归：同 inode 重链时 os.replace 是 no-op，临时文件必须清掉。

    旧实现泄漏 `systems/.../.gqt_relink_*.tmp` 进槽树 → 槽摘要漂移 →
    每轮 proof-drift 整槽重建（实测记录见 issue 附件）。
    """
    repo = tmp_path / "repo"
    slot = tmp_path / "slot"
    (repo / "systems" / "container").mkdir(parents=True)
    (slot / "systems" / "container").mkdir(parents=True)
    target = repo / "systems" / "container" / "data_source.gd"
    target.write_text("extends RefCounted\n", encoding="utf-8")

    shadow.relink_from_repo(str(repo), str(slot),
                            "systems/container/data_source.gd")
    # 第二次：槽目标已是同一 inode 的硬链接 → rename no-op 路径
    shadow.relink_from_repo(str(repo), str(slot),
                            "systems/container/data_source.gd")

    leftovers = list((slot / "systems" / "container").glob(".gqt_relink_*"))
    assert leftovers == [], f"relink 临时文件残留: {leftovers}"
    assert (slot / "systems" / "container" / "data_source.gd").read_text(
        encoding="utf-8") == "extends RefCounted\n"
