"""SEE-1416 §SPEC-1416-7 tidy 覆盖率治理：reporting.py 静态预检 invalid 记录路径。

全量单测（558）中该路径仅经 subprocess worker 命中、进程内零覆盖——
本文件以细胞级测试补齐 reporting.py 至 100%，不改任何产品行为。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation import reporting
from godot_qa_toolkit.mutation.reporting import (
    _mutant_record,
    _record_invalid_mutants,
    _valid_mutants,
    line_text_of,
    survivors_projection,
)


class TestRecordInvalidMutantsCoverage:
    """静态预检 invalid_mutant 记录循环（§SPEC-1416-2 扩列路径）。"""

    def _fake_invalid_mutant(self, src_name="calc.gd", line=2):
        return Mutant(file=src_name, line=line, column=8, operator="cond_not",
                      original="x", mutated="not x", kind="TERNARY")

    def test_invalid_mutant_records_full_shape_in_process(self, monkeypatch):
        # Arrange：伪造静态预检产出 1 个 invalid mutant
        invalid = [self._fake_invalid_mutant()]
        monkeypatch.setattr(
            reporting, "_split_invalid_mutants",
            lambda src, mutants: (([], invalid ), mutants))
        src = "func f(x):\n\treturn 1 if x else 2\n"

        # Act
        results, invalid_count = _record_invalid_mutants(src, invalid)

        # Assert：全量记录形态（§SPEC-1416-2 同构列）
        assert invalid_count == 1
        r = results[0]
        assert r["verdict"] == "invalid_mutant"
        assert r["file"] == "calc.gd"
        assert r["line"] == 2
        assert r["column"] == 8
        assert r["operator"] == "cond_not"
        assert r["kind"] == "TERNARY"
        assert r["original"] == "x"
        assert r["mutated"] == "not x"
        assert "operator bug" in r["reason"]
        # line_text 与被测源文件对应行一致（§SPEC-1416-2 判据）
        assert r["line_text"] == src.splitlines()[1]

    def test_invalid_mutant_slice_out_of_range_yields_no_records(self, monkeypatch):
        # Arrange：分片区间越界 → scoped 为空 → 零 invalid（合并层不重复计数语义）
        invalid = [self._fake_invalid_mutant()]
        seen_slices = {}
        monkeypatch.setattr(
            reporting, "_split_invalid_mutants",
            lambda src, mutants: seen_slices.setdefault(
                len(mutants), ([], list(mutants)))
            if mutants else ([], []))
        muts = [self._fake_invalid_mutant() for _ in range(1)]

        # Act
        results, invalid_count = _record_invalid_mutants("x", muts,
                                                         mutant_slice=(5, 9))

        # Assert
        assert results == [] and invalid_count == 0

    def test_invalid_mutant_slice_scopes_records(self, monkeypatch):
        # Arrange：3 个 mutant，分片只管 [1, 3)——预检只针对该片
        muts = [self._fake_invalid_mutant(col) for col in range(1, 4)]
        monkeypatch.setattr(
            reporting, "_split_invalid_mutants",
            lambda src, mutants: ([], list(mutants)))
        src = "func f(x):\n\treturn 1 if x else 2\n"

        # Act
        results, invalid_count = _record_invalid_mutants(src, muts,
                                                         mutant_slice=(1, 3))

        # Assert：只预检分片区间 2 个（column 8 与 8 的第二三个）
        assert invalid_count == 2
        assert {r["column"] for r in results} == {8}
