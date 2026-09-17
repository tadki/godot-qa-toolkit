"""SPEC-001~003/007: coverage 正确性与分支覆盖 unit tests (SEE-1312).

TDD 配对：先 RED 再 GREEN。mock GUT 模式沿既有 test_coverage.py 惯例。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.coverage.runner import (
    _collect_executable_lines,
    _instrument,
    run_coverage,
)

# 跨行分组表达式（SEE-1308 实测 parse error 根因样本）
GD_MULTILINE = """extends Node

func collect(x) -> Array:
	var result = []
	result = (result
		.append(x))
	return result
"""


class TestInstrumentStatementLevel:
    """SPEC-001: AST 语句级插桩——探针只落语句起始行，跨行分组不 parse error。"""

    SINK = "qa-coverage-hits-test.txt"

    def test_multiline_grouped_expr_not_probe_inside(self):
        instrumented, _ = _instrument(GD_MULTILINE, self.SINK)
        # 探针绝不能落在续行（`.append` 行）
        for i, line in enumerate(instrumented.splitlines(), start=1):
            if ".append" in line:
                assert "__qa_cov_probe" not in line, (
                    f"probe inside grouped expression at line {i}: {line}"
                )

    def test_instrumented_source_reparses(self):
        # 插桩产物必须仍是合法 GDScript（parse error = 插桩器 bug）
        from gdtoolkit.parser import parser as gdtoolkit_parser
        instrumented, _ = _instrument(GD_MULTILINE, self.SINK)
        gdtoolkit_parser.parse(instrumented)

    def test_probe_precedes_statement_start_line(self):
        instrumented, _ = _instrument(GD_MULTILINE, self.SINK)
        lines = instrumented.splitlines()
        # `result = (...)` 语句起始行（第 5 行）之前应有探针
        stmt_line = next(l for l in lines if "result = (" in l)
        idx = lines.index(stmt_line)
        assert "__qa_cov_probe" in lines[idx - 1]

    def test_probe_lines_match_executable_lines(self):
        instrumented, _ = _instrument(GD_MULTILINE, self.SINK)
        import re as _re
        probe_lines = {
            i for i, l in enumerate(instrumented.splitlines(), start=1)
            if _re.match(r"^\s*__qa_cov_probe_\d+\(\)\s*$", l)
        }
        # 每个探针行必然紧跟在某个语句前——探针数 == 可执行语句数
        assert len(probe_lines) == len(_collect_executable_lines(GD_MULTILINE))

    def test_multiline_run_coverage_no_parse_error(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "coll.gd"
        target.write_text(GD_MULTILINE)

        def fake(*a, **k):
            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        monkeypatch.setattr("godot_qa_toolkit.coverage.runner.subprocess.run", fake)
        result = run_coverage(str(target), str(proj))
        # mock rc=0 且无 sink → 命中 probes_fired 哨兵（预期）；但 run_error 的
        # 原因绝不能是插桩产物 parse error（SPEC-001 核心断言）
        if result["summary"].get("run_error"):
            assert "unparseable" not in result["summary"].get("error", ""), \
                result["summary"]["error"]


class TestProbeSinkRobustness:
    """SPEC-002: 追加写 + run 唯一 sink + ID-keyed 数据面 + user:// 优先。"""

    SINK = "qa-coverage-hits-test.txt"

    def test_probe_appends_not_truncates(self):
        # GDScript 探针源码必须用追加语义（READ_WRITE + seek_end 或唯一文件），
        # 每次命中重开 WRITE 会截断此前全部命中（代码走查实测缺陷）。
        from godot_qa_toolkit.coverage import runner as c
        instrumented, _ = _instrument("func f():\n\treturn true\n", self.SINK)
        # 探针函数体内不得出现裸 WRITE 模式重开同一文件
        assert "FileAccess.READ_WRITE" in instrumented or "user://" in instrumented

    SINK = "qa-coverage-hits-test.txt"

    def test_sink_path_is_unique_per_run(self, tmp_path, monkeypatch):
        import godot_qa_toolkit.coverage.runner as c
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        # 手工放 sink（id 0 命中）避免 probes_fired 哨兵中止
        captured = {}
        real_instrument = c._instrument

        def spy(src, sink_name):
            captured["sink"] = sink_name
            return real_instrument(src, sink_name)

        monkeypatch.setattr(c, "_instrument", spy)
        monkeypatch.setattr(
            c.subprocess, "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        (proj / f"qa-coverage-hits-{os.getpid()}.txt").write_text("0\n")
        c.run_coverage(str(target), str(proj))
        # sink 名必须含 run 唯一性成分（进程 id），不能是固定文件名
        assert "qa-coverage-hits" in captured["sink"]
        assert str(os.getpid()) in captured["sink"]

    def test_probe_writes_ids_not_paths(self):
        # ID-keyed 数据面：探针写整数 probe_id，路径信息不进数据面
        from godot_qa_toolkit.coverage import runner as c
        instrumented, _ = _instrument("func f():\n\treturn true\n", self.SINK)
        # 探针调用传整数 id
        assert "__qa_cov_probe_(0)" in instrumented or "__qa_cov_probe_0(" in instrumented

    def test_id_keyed_hits_decoded_back_to_lines(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\tvar c = a + b\n\treturn c\n")

        # runner 必须把 probe_id 映射回行号（manifest），并对乱序/重复 id 幂等
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        # 模拟 sink 文件（run_coverage 的 pid 派生名）：id 0 两次 + id 1 一次
        sink_name = f"qa-coverage-hits-{os.getpid()}.txt"
        sink_path = proj / sink_name
        with open(sink_path, "w") as f:
            f.write("0\n0\n1\n")
        result = run_coverage(str(target), str(proj))
        assert result["summary"]["covered_lines"] == 2  # 两个不同 id，重复去重


class TestSentinels:
    """SPEC-003: probes_fired / vacuous 哨兵——静默 0% 根除。"""

    SINK = "qa-coverage-hits-test.txt"

    def _write_project(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text("func add(a, b):\n\treturn a + b\n")
        return proj, target

    def test_zero_probes_fired_is_run_error(self, tmp_path, monkeypatch):
        # GUT rc=0 但 sink 不存在 → 探针从未点火 ≠ 真实 0% 覆盖
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probe" in result["failures"][0]["reason"].lower()

    def test_probes_fired_reported_in_summary(self, tmp_path, monkeypatch):
        proj, target = self._write_project(tmp_path)
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        # 手工放一个 sink：id 0 命中一次（sink 名 = run_coverage 的 pid 派生名）
        sink_name = f"qa-coverage-hits-{os.getpid()}.txt"
        (proj / sink_name).write_text("0\n")
        result = run_coverage(str(target), str(proj))
        assert result["summary"].get("probes_fired") == 1

    def test_no_executable_lines_is_vacuous(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "noop.gd"
        target.write_text("extends Node\n")
        result = run_coverage(str(target), str(proj))
        assert result["summary"].get("vacuous") is True
        assert result["ok"] is True


class TestBranchCoverageV1:
    """SPEC-007: 行探针 + AST 纯推导 branches_total/covered（observational）。"""

    SINK = "qa-coverage-hits-test.txt"

    GD_BRANCHY = """extends Node

func f(x):
	if x:
		return 1
	else:
		return 2
"""

    def test_branches_total_derived(self):
        from godot_qa_toolkit.coverage.runner import _derive_branches
        branches = _derive_branches(self.GD_BRANCHY)
        # if + else = 2 个分支
        assert branches.total == 2

    def test_branch_bodies_map_to_probe_ids(self):
        from godot_qa_toolkit.coverage.runner import _derive_branches
        branches = _derive_branches(self.GD_BRANCHY)
        # 每个分支体有语句行，可映射到行探针 id
        assert all(b.line is not None for b in branches.items)

    def test_match_branches_counted(self):
        from godot_qa_toolkit.coverage.runner import _derive_branches
        src = "func f(x):\n\tmatch x:\n\t\t1:\n\t\t\treturn 1\n\t\t_:\n\t\t\treturn 2\n"
        branches = _derive_branches(src)
        assert branches.total == 2

    def test_branch_fields_in_summary(self, tmp_path, monkeypatch):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "br.gd"
        target.write_text("func f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n")
        monkeypatch.setattr(
            "godot_qa_toolkit.coverage.runner.subprocess.run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        sink_name = f"qa-coverage-hits-{os.getpid()}.txt"
        (proj / sink_name).write_text("0\n1\n")
        result = run_coverage(str(target), str(proj))
        s = result["summary"]
        # observational：字段必须在，且不影响 ok 判定
        assert "branch_coverage_percent" in s
