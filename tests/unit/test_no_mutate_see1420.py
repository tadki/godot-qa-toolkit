"""§SPEC-1420-09：`# gqt: no-mutate` 注释过滤（定案 L6）。

函数级（至下一顶层声明）+ 行级；被过滤位点不产 mutant。并入同次 schema
bump（过滤先行——无位点 scope 不进 manifest）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.scopemanifest import build_manifest

SRC = ("extends Node\n"
       "\n"
       "func keep(a):\n"
       "\treturn a + 1\n"
       "\n"
       "func skip_me(a):  # gqt: no-mutate\n"
       "\treturn a + 2\n"
       "\n"
       "func keep_too(a):\n"
       "\treturn a + 3\n")


def _ids(src):
    _, _, tagged, _ = build_manifest(src, "x.gd",
                                     _collect_mutations(src, "x.gd"))
    return tagged


class TestNoMutateFuncLevel:
    def test_inline_marker_suppresses_whole_func(self):
        tagged = _ids(SRC)
        assert all(not m.stable_id.startswith("x.gd#skip_me")
                   for m in tagged)
        assert any(m.scope_id == "keep" for m in tagged)
        assert any(m.scope_id == "keep_too" for m in tagged)

    def test_marker_line_above_func_suppresses_whole_func(self):
        src = ("func a(x):\n"
               "\treturn x + 1\n"
               "\n"
               "# gqt: no-mutate\n"
               "func b(x):\n"
               "\treturn x + 2\n")
        tagged = _ids(src)
        assert all(m.scope_id != "b" for m in tagged)
        assert any(m.scope_id == "a" for m in tagged)

    def test_suppression_extends_to_next_top_level_decl(self):
        """函数级抑制域 = 至下一顶层声明（多行函数体全覆盖）。"""
        src = ("func a(x):\n"
               "\treturn x + 1\n"
               "\n"
               "func b(x):  # gqt: no-mutate\n"
               "\tif x > 0:\n"
               "\t\treturn x + 2\n"
               "\treturn x - 3\n"
               "\n"
               "func c(x):\n"
               "\treturn x + 4\n")
        tagged = _ids(src)
        assert all(m.scope_id != "b" for m in tagged)
        assert any(m.scope_id == "a" for m in tagged)
        assert any(m.scope_id == "c" for m in tagged)


class TestNoMutateLineLevel:
    def test_marker_on_statement_line_suppresses_that_line_only(self):
        src = ("func a(x):\n"
               "\treturn x + 1  # gqt: no-mutate\n")
        tagged = _ids(src)
        assert tagged == []  # 该函数唯一位点在被抑制行上

    def test_line_level_inside_func(self):
        src = ("func a(x):\n"
               "\tif x > 0:  # gqt: no-mutate\n"
               "\t\treturn x + 1\n"
               "\treturn x - 2\n")
        tagged = _ids(src)
        # 行 2（守卫行）被抑制；行 3/4 的位点保留
        assert all(m.line != 2 for m in tagged)
        assert any(m.line == 3 for m in tagged)
        assert any(m.line == 4 for m in tagged)

    def test_no_mutate_scope_excluded_from_manifest(self):
        """过滤先行：整函数被抑制 → 无位点 scope 不进 manifest（D2）。"""
        src = ("func a(x):\n"
               "\treturn x + 1\n"
               "\n"
               "func b(x):  # gqt: no-mutate\n"
               "\treturn x + 2\n")
        _, _, _, site_scopes = build_manifest(
            src, "x.gd", _collect_mutations(src, "x.gd"))
        assert "a" in site_scopes
        assert "b" not in site_scopes
