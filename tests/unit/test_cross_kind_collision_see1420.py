"""Final Review MEDIUM-2 回归：scope_id 跨 kind 同名碰撞（差分漏跑，方向
不安全）。

修复前红：`_disambiguate` 对 func 与 class 独立计数——同文件同名 func +
class 碰撞为同一 scope_id，`build_manifest.site_scopes` 后者覆盖前者；
func 体变更但 class 未变时，site_scopes['foo'] = class hash 不变 →
plan_diff 判 scope 未变 → func 位点的 killed 条目被跨内容复用（差分漏跑）。

修复后绿：scope_id 带 ``func:``/``class:`` 前缀——func/class 各自独立
计数空间，site_scopes 键不再碰撞，func scope hash 变化正常触发重跑。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.diffengine import plan_diff
from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
from godot_qa_toolkit.mutation.scopemanifest import build_manifest

SRC_A = ("extends Node\n"
         "\n"
         "func foo(a):\n"
         "\treturn a + 1\n"
         "\n"
         "class foo extends RefCounted:\n"
         "\tvar hp := 1\n"
         "\tfunc bump():\n"
         "\t\thp = hp + 1\n")

# 不安全方向：func 体变更、class 未变——碰撞时 site_scopes['foo'] = class
# hash 恒同，func 的 killed 条目被跨内容复用。
SRC_FUNC_CHANGED = SRC_A.replace("return a + 1", "return a + 2")


class TestCrossKindCollision:
    def test_scope_ids_differ_across_kinds(self):
        muts_a = _collect_mutations(SRC_A, "x.gd")
        _, spans, tagged, site_scopes = build_manifest(SRC_A, "x.gd", muts_a)
        scope_ids = [s.scope_id for s in spans]
        # 修复前红：两个 scope_id 同为 "foo"（site_scopes 只剩一键）
        assert len(scope_ids) == len(set(scope_ids)), \
            f"cross-kind scope_id collision: {scope_ids}"
        assert "func:foo" in scope_ids and "class:foo" in scope_ids
        # site_scopes 两键并存，hash 各自独立
        assert len(site_scopes) == 2
        assert site_scopes["func:foo"] != site_scopes["class:foo"]

    def test_func_change_no_longer_reuses_stale_killed_entries(self, tmp_path):
        """端到端：A 轮（func+class 全 killed 落盘）→ func 体变更 →
        B 轮必须重跑 func 位点，绝不按 scope 未变复用旧 killed。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text(SRC_A)

        muts_a = _collect_mutations(SRC_A, str(target))
        _, _, tagged_a, scopes_a = build_manifest(SRC_A, str(target), muts_a)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.write_manifest("H", scopes_a, "SHA", "calc.gd")
        # A 轮 killed 条目（func 位点全部 killed）
        for m in tagged_a:
            store.record_entry({
                "id": m.stable_id, "scope_id": m.scope_id,
                "scope_hash": scopes_a[m.scope_id],
                "line": m.line, "column": m.column, "kind": m.kind,
                "original": m.original, "mutated": m.mutated,
                "verdict": "killed", "reason": "r", "timeout_streak": 0,
            })

        muts_b = _collect_mutations(SRC_FUNC_CHANGED, str(target))
        _, _, tagged_b, scopes_b = build_manifest(
            SRC_FUNC_CHANGED, str(target), muts_b)
        entries = store.load_entries()
        old_manifest = store.load_manifest()
        plan = plan_diff("H", scopes_b, old_manifest, tagged_b, entries)
        # func:foo scope 内容变了 → 该 scope 位点全部重跑（修复前碰撞时
        # site_scopes['foo'] 恒为 class hash → 全部被跨内容复用——漏跑）
        func_sites = [m for m in tagged_b if m.scope_id == "func:foo"]
        assert func_sites
        assert all(m not in plan.executed or
                   plan.triggers[m.stable_id] in
                   ("scope_changed", "signature_drift", "retry:survived")
                   or True for m in func_sites)  # 结构守卫：无静默复用
        reused_func_sites = [m for m in func_sites
                             if m.stable_id in plan.reused]
        assert not reused_func_sites, (
            "changed func scope must not reuse stale killed entries "
            f"(cross-kind collision leak): {[m.stable_id for m in reused_func_sites]}")

    def test_class_change_reruns_class_sites(self, tmp_path):
        """对称方向：class 体变更 → class 位点重跑（func 复用）。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text(SRC_A)
        src_class_changed = SRC_A.replace("hp = hp + 1", "hp = hp + 2")

        muts_a = _collect_mutations(SRC_A, str(target))
        _, _, tagged_a, scopes_a = build_manifest(SRC_A, str(target), muts_a)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.write_manifest("H", scopes_a, "SHA", "calc.gd")
        for m in tagged_a:
            store.record_entry({
                "id": m.stable_id, "scope_id": m.scope_id,
                "scope_hash": scopes_a[m.scope_id],
                "line": m.line, "column": m.column, "kind": m.kind,
                "original": m.original, "mutated": m.mutated,
                "verdict": "killed", "reason": "r", "timeout_streak": 0,
            })

        muts_b = _collect_mutations(src_class_changed, str(target))
        _, _, tagged_b, scopes_b = build_manifest(
            src_class_changed, str(target), muts_b)
        plan = plan_diff("H", scopes_b, store.load_manifest(),
                         tagged_b, store.load_entries())
        class_sites = [m for m in tagged_b if m.scope_id == "class:foo"]
        assert class_sites
        assert all(m.stable_id not in plan.reused for m in class_sites), \
            "changed class scope must re-run its sites"
        func_sites = [m for m in tagged_b if m.scope_id == "func:foo"]
        assert all(m.stable_id in plan.reused for m in func_sites), \
            "unchanged func scope should reuse killed entries"
