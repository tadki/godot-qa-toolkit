"""SEE-1416 §SPEC-1416-8 hardener 对抗测试：杀死首轮人工注入存活变异。

首轮 28 个手注变异中 7 个存活，本文件逐一对症补刀：
- R03  line_text_of 下界放开（line=0/-1 应返回 ""）
- R05  splitlines→split("\\n")（CRLF 源行文本不得带 \\r）
- R08  truncated 边界 `>`→`>=`（恰好 5 条不得标记 truncated）
- R13  投影 f[k]→f.get(k)（残缺记录必须 fail-fast KeyError）
- R19  分片 stop+1（slice 右开区间判别——tidy 测试夹具不判别所暴露）
- R26  attention cap `>=`→`>`（第 6 条不得进场）
- R27  attention 去 `if k in f` 过滤（残缺记录容错语义钉死）
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor, reporting
from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.reporting import (
    SURVIVOR_DETAIL_KEYS,
    _record_invalid_mutants,
    line_text_of,
    survivors_projection,
)


def _failure(file="a.gd", line=2):
    return {k: k for k in SURVIVOR_DETAIL_KEYS} | {
        "file": file, "line": line, "verdict": "survived"}


class TestLineTextBoundary:
    """R03/R05：line_text_of 的越界与 CRLF 语义。"""

    def test_zero_and_negative_line_yield_empty(self):
        src = "func f():\n\tpass\n"
        assert line_text_of(src, 0) == ""
        assert line_text_of(src, -1) == ""

    def test_crlf_source_line_text_has_no_cr(self):
        src = "func f():\r\n\treturn 1\r\n"
        assert line_text_of(src, 2) == "\treturn 1"
        assert "\r" not in line_text_of(src, 1)


class TestSurvivorsProjectionBoundary:
    """R08/R13：截断边界与记录形态严格性。"""

    def test_exactly_cap_failures_not_truncated(self):
        failures = [_failure(line=i) for i in range(1, 6)]
        survivors, truncated = survivors_projection(failures)
        assert len(survivors) == 5
        assert truncated is False

    def test_over_cap_failures_truncated(self):
        failures = [_failure(line=i) for i in range(1, 7)]
        survivors, truncated = survivors_projection(failures)
        assert len(survivors) == 5
        assert truncated is True

    def test_partial_record_fails_fast(self):
        # 投影契约：记录由 _mutant_record 全形态产出；缺键即数据 bug，
        # 必须 KeyError 当场暴露而非静默投影 None。
        with pytest.raises(KeyError):
            survivors_projection([{"file": "a.gd", "verdict": "survived"}])


class TestInvalidSliceDiscrimination:
    """R19：分片右开区间——tidy 夹具 (1,3)/3 项不判别 stop+1，改用 (1,2)。"""

    def test_slice_stop_is_exclusive(self, monkeypatch):
        muts = [Mutant(file="c.gd", line=2, column=c, operator="op",
                       original="x", mutated="y", kind="K")
                for c in (1, 2, 3)]
        monkeypatch.setattr(
            reporting, "_split_invalid_mutants",
            lambda src, mutants: ([], list(mutants)))
        results, invalid_count = _record_invalid_mutants(
            "x", muts, mutant_slice=(1, 2))
        assert invalid_count == 1
        assert [r["column"] for r in results] == [2]


class TestAttentionCap:
    """R26/R27：跨文件 attention 的 cap 与残缺记录容错。"""

    def test_attention_caps_at_five(self):
        results = [{"failures": [_failure(file="a.gd", line=i)
                                 for i in range(1, 5)]},
                   {"failures": [_failure(file="b.gd", line=i)
                                 for i in range(1, 5)]}]
        assert len(executor._attention_details(results)) == 5

    def test_attention_tolerates_partial_records(self):
        # error/no_sites 契约之外的残缺 verdict 记录：投影容忍缺键
        # （只投影在场键），不得整轮崩溃。
        results = [{"failures": [{"verdict": "survived", "file": "a.gd"}]}]
        assert executor._attention_details(results) == [
            {"file": "a.gd", "verdict": "survived"}]
