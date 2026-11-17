"""SPEC-001~003/007: coverage 正确性与分支覆盖 unit tests (SEE-1312).

TDD 配对：先 RED 再 GREEN。mock GUT 模式沿既有 test_coverage.py 惯例。
"""

import json
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



def _sink_writer_spy(monkeypatch, proj, content: str) -> dict:
    """PR-B: capture this run's UUID sink name at _instrument time and write
    `content` into it (the fake GUT would produce the hits)."""
    import godot_qa_toolkit.coverage.runner as c

    captured = {"written": False}
    real = c._instrument

    def spy(src, sink_name):
        captured["sink"] = proj / sink_name
        captured["sink"].write_text(content, encoding="utf-8")
        captured["written"] = True
        return real(src, sink_name)

    monkeypatch.setattr(c, "_instrument", spy)
    return captured


def _mapped_project(tmp_path, gd_text="func add(a, b):\n\tvar c = a + b\n\treturn c\n"):
    import attribution_fixtures as fx

    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "calc.gd"
    target.write_text(gd_text, encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.gd").write_text("extends Node\n", encoding="utf-8")
    fx.write_map(proj, {"calc.gd": ["test_x.gd::test_a"]})
    return proj, target


def _no_godot(monkeypatch):
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.detect_binary_kind",
        lambda *a, **k: "linux")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.identity.identity_pack",
        lambda *a, **k: {"kind": "linux"})


def _write_raw_map(project_root, rel, instrumentable, covered_lines,
                   probes_fired):
    """Raw test-attribution/6 doc with full control（哨兵/车道测试用）。"""
    import hashlib

    import attribution_fixtures as fx
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    fx.install_recipe(project_root)
    recipe, error = build_recipe(project_root)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    ident = "test_x.gd::test_a"
    arm = {"test_to_files": {ident: [rel]}, "script_to_files": {},
           "tests": 1, "scripts": 1, "files": 1, "pairs": 1,
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {},
           "file_to_tests": {rel: [ident]},
           "file_to_scripts": {rel: ["test_x.gd"]},
           "line_pairs": len(covered_lines),
           "script_line_pairs": len(covered_lines),
           "test_to_lines": {ident: {rel: list(covered_lines)}},
           "script_to_lines": {},
           "line_to_tests": {rel: {str(covered_lines[0]): [ident]}}}
    doc = {"schema_version": "test-attribution/6",
           "report_recipe_hash": raw, "recipe_hash_pristine": raw,
           "project_root": str(project_root), "identity": ident,
           "causal": {"enabled": False},
           "file_sha256": {rel: hashlib.sha256(
               (Path(project_root) / rel).read_bytes()).hexdigest()},
           "target_files": [rel],
           "tests_face": {"tests/test_x.gd": hashlib.sha256(
               (Path(project_root) / "tests" / "test_x.gd").read_bytes()).hexdigest()},
           "instrumentable_lines": {rel: list(instrumentable)},
           "arms": {"gut_full": arm},
           "arms_state": {"gut_full": {"state": "valid",
                                       "invalid_reason": None, "bundles": 1,
                                       "pairs": len(covered_lines),
                                       "probes_fired": probes_fired,
                                       "vacuous": False}},
           "notes": []}
    out = Path(project_root) / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

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

    def test_probe_writes_ids_not_paths(self):
        # ID-keyed 数据面：探针写整数 probe_id，路径信息不进数据面
        from godot_qa_toolkit.coverage import runner as c
        instrumented, _ = _instrument("func f():\n\treturn true\n", self.SINK)
        # 探针调用传整数 id
        assert "__qa_cov_probe_(0)" in instrumented or "__qa_cov_probe_0(" in instrumented

    def test_id_keyed_hits_decoded_back_to_lines(self, tmp_path, monkeypatch):
        """映射行事实（covered=[2]）解码回行号集合：hits_lines 与 covered 一致。"""
        proj, target = _mapped_project(tmp_path)
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        assert result["summary"]["hits_lines"] == [2]
        assert result["summary"]["covered_lines"] == 1

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
        """执行证据哨兵：映射 probes_fired=0 且存在有效行事实 → run_error，
        不得产出可信 0% 覆盖分数（§SPEC-025/027）。"""
        proj, target = _mapped_project(tmp_path)
        _no_godot(monkeypatch)
        _write_raw_map(proj, "calc.gd", [2, 3], covered_lines=[2],
                       probes_fired=0)
        result = run_coverage(str(target), str(proj))
        assert result["ok"] is False
        assert result["summary"].get("run_error") is True
        assert "probes_fired=0" in result["summary"]["error"]

    def test_probes_fired_reported_in_summary(self, tmp_path, monkeypatch):
        proj, target = _mapped_project(tmp_path)
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        # probes_fired 来自映射 arms_state（不伪造）
        assert result["summary"].get("probes_fired") == 640
        assert result["summary"].get("probes_source") == "map_arms_state"

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
        proj, target = _mapped_project(
            tmp_path,
            gd_text="func f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n")
        _no_godot(monkeypatch)
        result = run_coverage(str(target), str(proj))
        s = result["summary"]
        # observational：字段必须在，且不影响 ok 判定
        assert "branch_coverage_percent" in s
        # 无 else → 仅 if 一个分支（行 3）；covered={2} 不含行 3
        assert s["branch_total"] == 1
        assert s["branch_covered"] == 0
