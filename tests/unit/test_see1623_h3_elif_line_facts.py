"""SEE-1623 H3 — elif 条件行漏映射（假 uncovered）行为钉。

根因（SEE-1622 Harti BUG-4）：`EXECUTABLE_NODE_NAMES` 含 `if_branch` 但缺
`elif_branch` —— elif 条件行真实会被求值（卡尺绕过 prefilter 后能实际
killed/survived），却被排除在探针面/归属事实外：coverage 报 uncovered、
mutation prefilter 跳过其位点，门禁静默缩水且非确定。

行为契约（修复后）：
1. elif 条件行进入可插桩行集（if_branch 对称；else 头行仍排除——无求值
   语义、无变异位点）。
2. 语法安全保持：探针绝不物理插在 elif/else 头行前（硬化反例：插桩文件
   Parse Error）——elif 头行探针锚定到分支体首语句行发射。
3. 块插桩几何零变化：块入口探针数不含 elif 头行；块展开（唯一投影口）
   把 elif 头行并入其分支体首语句所在块——体首语句命中即记 elif 行覆盖
   （条件为真语义，保守不虚报 false 路径求值）。
"""

from __future__ import annotations

from gdtoolkit.parser import parser as gdtoolkit_parser

from godot_qa_toolkit.coverage.instrument import (
    _blocks_from_execs,
    _collect_executable_lines,
    _instrument,
    _instrument_blocks,
)
from godot_qa_toolkit.measurement.attribution import _probe_surface_lines
from godot_qa_toolkit.registry.lines import collect_executable_lines

_SRC = ("extends RefCounted\n"
        "\n"
        "func grade(score: int) -> String:\n"
        "\tif score >= 90:\n"
        "\t\treturn \"A\"\n"
        "\telif score >= 60:\n"
        "\t\treturn \"B\"\n"
        "\telse:\n"
        "\t\treturn \"C\"\n")
_ELIF_LINE = 6
_ELSE_LINE = 8
_BODY_LINE = 7


class TestElifHeadInLineSet:
    def test_elif_head_line_collected(self):
        els = collect_executable_lines(_SRC)
        by_line = {e.line: e.node for e in els}
        assert by_line.get(_ELIF_LINE) == "elif_branch"

    def test_else_head_line_still_excluded(self):
        els = collect_executable_lines(_SRC)
        assert not any(e.node == "else_branch" for e in els)

    def test_if_branch_symmetry_unchanged(self):
        els = collect_executable_lines(_SRC)
        by_line = {e.line: e.node for e in els}
        assert by_line.get(4) == "if_branch"


class TestProbePlacementSafety:
    def test_direct_manifest_includes_elif_head(self):
        _instrumented, manifest = _instrument(_SRC, "qa-coverage-hits-t.txt")
        assert _ELIF_LINE in manifest

    def test_direct_instrumented_source_reparses(self):
        instrumented, _m = _instrument(_SRC, "qa-coverage-hits-t.txt")
        gdtoolkit_parser.parse(instrumented)

    def test_block_starts_exclude_elif_head(self):
        _tpl, starts, _bmap = _instrument_blocks(_SRC, "qa-coverage-hits-t.txt")
        assert _ELIF_LINE not in starts
        assert _ELSE_LINE not in starts

    def test_block_expansion_credits_elif_head_with_body(self):
        _tpl, starts, bmap = _instrument_blocks(_SRC, "qa-coverage-hits-t.txt")
        surface = _probe_surface_lines(starts, bmap)
        assert _ELIF_LINE in surface
        anchor_block = next(v for v in bmap.values() if _ELIF_LINE in v)
        assert _BODY_LINE in anchor_block

    def test_block_instrumented_source_reparses(self):
        tpl, _s, _b = _instrument_blocks(_SRC, "qa-coverage-hits-t.txt")
        gdtoolkit_parser.parse(tpl)


class TestBlocksGeometry:
    def test_elif_head_not_solo_block(self):
        """elif 头行不得独立成块（独立块 = 独立探针 = 头行前插桩）。"""
        _tpl, _starts, bmap = _instrument_blocks(_SRC,
                                                 "qa-coverage-hits-t.txt")
        solo = [v for v in bmap.values() if v == [_ELIF_LINE]]
        assert solo == []
