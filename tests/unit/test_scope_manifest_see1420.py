"""§SPEC-1420-03：scope manifest 提取单测（定案 D1/D2，表驱动 fixture
与手算清单逐项精确）。

scope 集 = 顶层 func + 内部 class 整体一个黑盒 + module（非 func 顶层声
明）；lambda 归外层 func；header hash 只收非 func 顶层声明、func hash 绝
不进全量重跑触发成分、单向保守；无位点 scope 不进 manifest。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.scopemanifest import (
    build_manifest,
    extract_scopes,
    scope_of_line,
    scopes_map,
)

TOP_LEVEL_FUNCS = """\
extends Node

const MAX = 3

func add(a, b):
\treturn a + b

func sub(a, b):
\treturn a - b
"""

INNER_CLASS = """\
extends Node

func outer(a):
\treturn a + 1

class Inner extends RefCounted:
\tvar hp := 1
\tfunc bump():
\t\thp = hp + 1
"""

LAMBDA_IN_FUNC = """\
extends Node

func host(a):
\tvar f = func(x): return x * 2
\treturn f(a) + 1
"""


class TestScopeSet:
    def test_top_level_funcs_each_one_scope(self):
        hh, spans = extract_scopes(TOP_LEVEL_FUNCS)
        func_ids = [s.scope_id for s in spans if s.kind == "func"]
        assert func_ids == ["add", "sub"]

    def test_inner_class_is_single_black_box_scope(self):
        hh, spans = extract_scopes(INNER_CLASS)
        by_kind = {(s.kind, s.scope_id) for s in spans}
        assert ("func", "outer") in by_kind
        assert ("class", "Inner") in by_kind
        # 黑盒：内部 class 的方法不单独成 scope
        assert not any(s.scope_id == "bump" for s in spans)

    def test_class_span_covers_whole_class_body(self):
        hh, spans = extract_scopes(INNER_CLASS)
        inner = next(s for s in spans if s.scope_id == "Inner")
        lines = INNER_CLASS.splitlines()
        # bump 方法体（黑盒内）行落在 Inner span 内
        bump_line = next(i + 1 for i, t in enumerate(lines)
                         if "hp = hp + 1" in t)
        assert inner.start_line <= bump_line <= inner.end_line

    def test_module_scope_covers_sites_outside_funcs_and_classes(self):
        proj_src = TOP_LEVEL_FUNCS
        hh, spans = extract_scopes(proj_src)
        # MAX = 3 的常量行（第 3 行）不在任何 func/class span → module
        assert scope_of_line(spans, 3) == "module"
        # func 体行归对应 func scope
        assert scope_of_line(spans, 5) == "add"

    def test_lambda_belongs_to_enclosing_func(self):
        hh, spans = extract_scopes(LAMBDA_IN_FUNC)
        lines = LAMBDA_IN_FUNC.splitlines()
        lambda_line = next(i + 1 for i, t in enumerate(lines)
                           if "func(x)" in t)
        assert scope_of_line(spans, lambda_line) == "host"

    def test_same_name_disambiguation(self):
        src = ("func heal(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func heal(b):\n"
               "\treturn b + 2\n")
        hh, spans = extract_scopes(src)
        ids = [s.scope_id for s in spans]
        assert ids == ["heal", "heal#2"]


class TestHeaderHash:
    def test_header_only_covers_non_func_top_level_decls(self):
        src_a = TOP_LEVEL_FUNCS
        src_b = TOP_LEVEL_FUNCS.replace("const MAX = 3", "const MAX = 4")
        ha, _ = extract_scopes(src_a)
        hb, _ = extract_scopes(src_b)
        assert ha != hb  # 顶层声明变更 → header 变

    def test_func_body_change_does_not_move_header(self):
        src_a = TOP_LEVEL_FUNCS
        src_b = TOP_LEVEL_FUNCS.replace("return a + b", "return a + b + 1")
        ha, _ = extract_scopes(src_a)
        hb, _ = extract_scopes(src_b)
        assert ha == hb  # D2：func hash 绝不进全量重跑触发成分

    def test_header_ignores_blank_and_comment_only_lines(self):
        src_a = TOP_LEVEL_FUNCS
        src_b = TOP_LEVEL_FUNCS + "\n# a trailing comment\n"
        ha, _ = extract_scopes(src_a)
        hb, _ = extract_scopes(src_b)
        assert ha == hb

    def test_one_way_conservative(self):
        """单向保守：header 变更只能由非 func 顶层声明触发；func 变更绝不
        反向触发 header（防顶层 const 引用 func 的双向依赖拖垮差分）。"""
        src_a = TOP_LEVEL_FUNCS
        src_b = TOP_LEVEL_FUNCS.replace("return a - b", "return a - b - 1")
        ha, sa = extract_scopes(src_a)
        hb, sb = extract_scopes(src_b)
        assert ha == hb
        assert scopes_map(sa)["sub"] != scopes_map(sb)["sub"]


class TestManifestBuild:
    def test_no_site_scope_excluded_from_manifest(self):
        """无位点 scope 不进 manifest（D2）：纯 pass 函数不产生 scope 条目。"""
        src = ("extends Node\n"
               "\n"
               "func with_site(a):\n"
               "\treturn a + 1\n"
               "\n"
               "func no_site():\n"
               "\tpass\n")
        mutants = _collect_mutations(src, "x.gd")
        _, _, _, site_scopes = build_manifest(src, "x.gd", mutants)
        assert "with_site" in site_scopes
        assert "no_site" not in site_scopes

    def test_module_sites_anchor_to_header_hash(self):
        """module 位点的 scope hash = header hash（D2 同域）——顶层 const
        变更同触 header 重跑，module 差分不 self-invalidate。"""
        src = ("const K = 1 + 2\n"
               "\n"
               "func f(a):\n"
               "\treturn a + 1\n")
        mutants = _collect_mutations(src, "x.gd")
        hh, spans, tagged, site_scopes = build_manifest(src, "x.gd", mutants)
        assert site_scopes.get("module") == hh

    def test_build_manifest_tags_ids_and_filters(self):
        src = ("func add(a, b):\n"
               "\treturn a + b\n")
        mutants = _collect_mutations(src, "x.gd")
        hh, spans, tagged, site_scopes = build_manifest(src, "x.gd", mutants)
        assert all(m.stable_id.startswith("x.gd#add#") for m in tagged)
        ns = sorted(m.n for m in tagged)
        assert ns == list(range(1, len(tagged) + 1))
        assert set(site_scopes) == {"add"}

    def test_comment_insertion_does_not_shift_any_id(self):
        """§SPEC-1420-04 属性断言（scope 域）：插入注释行不位移任何 id。"""
        src_a = ("func add(a, b):\n"
                 "\treturn a + b\n"
                 "\n"
                 "func sub(a, b):\n"
                 "\treturn a - b\n")
        src_b = src_a.replace("func sub", "# a comment\nfunc sub")
        ma = build_manifest(src_a, "x.gd",
                            _collect_mutations(src_a, "x.gd"))[2]
        mb = build_manifest(src_b, "x.gd",
                            _collect_mutations(src_b, "x.gd"))[2]
        key = lambda ms: [(m.kind, m.original, m.mutated, m.scope_id, m.n)
                          for m in ms]  # noqa: E731
        assert key(ma) == key(mb)

    def test_new_statement_shifts_only_its_own_scope(self):
        src_a = ("func add(a, b):\n"
                 "\treturn a + b\n"
                 "\n"
                 "func sub(a, b):\n"
                 "\treturn a - b\n")
        src_b = src_a.replace("return a + b",
                              "var t = a * 2\n\treturn a + b + t")
        ma = build_manifest(src_a, "x.gd",
                            _collect_mutations(src_a, "x.gd"))[2]
        mb = build_manifest(src_b, "x.gd",
                            _collect_mutations(src_b, "x.gd"))[2]
        add_a = [(m.kind, m.original, m.mutated, m.n) for m in ma
                 if m.scope_id == "add"]
        add_b = [(m.kind, m.original, m.mutated, m.n) for m in mb
                 if m.scope_id == "add"]
        assert add_a != add_b  # add 域内位移
        sub_a = [(m.kind, m.original, m.mutated, m.n) for m in ma
                 if m.scope_id == "sub"]
        sub_b = [(m.kind, m.original, m.mutated, m.n) for m in mb
                 if m.scope_id == "sub"]
        assert sub_a == sub_b  # 其他 scope 零位移
