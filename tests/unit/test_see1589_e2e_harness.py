"""SEE-1589 hardener（⑤）：`run_see1589_e2e.py` 判据辅助逻辑的判别力。

§SPEC-012 的 E2E harness 是真实副本上的机器判定器；其**纯辅助判据**必须
可被单测钉死（否则 harness 自身假绿不会被发现）：

- `Checks.ok`：任一项非 PASS 即整体 FAIL（不得 any/恒真）；
- `_summary`：统一从 flat summary 或 results[0].summary 取度量；
- `_zero_ref_target`：只选「文件存在且映射无测试」的目标；
- `_prune_vendored_js` / `_normalize_symlinks`：副本归一化必须真实生效；
- `_map_facts`：从归属映射解析目标行与测试目录。

顶层一次 E2E 全跑（m1/h1/b 判据）由 §SPEC-012 机器运行验证（QA 在本 head
独立复验 16/16），本文件只钉住可离线判定的辅助函数。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "integration"))

import run_see1589_e2e as e2e  # noqa: E402


# --------------------------------------------------------------------------
# Checks.ok
# --------------------------------------------------------------------------
def test_checks_ok_requires_all_pass():
    c = e2e.Checks()
    c.add("a", True, "ok")
    c.add("b", True, "ok")
    assert c.ok is True
    c.add("c", False, "boom")
    assert c.ok is False, "任一非 PASS 必须使整体 FAIL"


# --------------------------------------------------------------------------
# _summary
# --------------------------------------------------------------------------
def test_summary_flat_doc():
    assert e2e._summary({"tool": "mutation", "ok": True,
                         "summary": {"file": "a.gd"}}) == {"file": "a.gd"}


def test_summary_results_envelope_uses_first_result():
    doc = {"tool": "mutation", "ok": True,
           "results": [{"summary": {"file": "a.gd", "mutants": 3}},
                       {"summary": {"file": "b.gd", "mutants": 9}}]}
    assert e2e._summary(doc) == {"file": "a.gd", "mutants": 3}


def test_summary_none_is_empty():
    assert e2e._summary(None) == {}


# --------------------------------------------------------------------------
# _zero_ref_target
# --------------------------------------------------------------------------
def test_zero_ref_target_skips_files_with_mapped_tests(tmp_path):
    kol = tmp_path / "kol"
    kol.mkdir()
    (kol / "a.gd").write_text("extends Node\n")
    (kol / "b.gd").write_text("extends Node\n")
    facts = {"target_files": ["a.gd", "b.gd"],
             "line_to_tests": {"a.gd": {"4": ["t.gd::t1"]}}}
    assert e2e._zero_ref_target(kol, facts) == "b.gd"


# --------------------------------------------------------------------------
# _prune_vendored_js
# --------------------------------------------------------------------------
def test_prune_vendored_js_removes_node_modules(tmp_path):
    kol = tmp_path / "kol"
    js = kol / "addons" / "x" / "server" / "node_modules" / "pkg"
    js.mkdir(parents=True)
    (js / "index.js").write_text("x")
    stats = e2e._prune_vendored_js(kol)
    assert stats["node_modules_pruned"] == 1
    assert not (kol / "addons" / "x" / "server" / "node_modules").exists()


# --------------------------------------------------------------------------
# _normalize_symlinks
# --------------------------------------------------------------------------
def test_normalize_symlinks_derefs_and_removes_dangling(tmp_path):
    kol = tmp_path / "kol"
    kol.mkdir()
    real = kol / "real.gd"
    real.write_text("extends Node\n", encoding="utf-8")
    link = kol / "link.gd"
    link.symlink_to(real)
    dangling = kol / "dangling.gd"
    dangling.symlink_to(kol / "missing.gd")
    stats = e2e._normalize_symlinks(kol)
    assert stats["symlinks_dereferenced"] == 1
    assert stats["dangling_links_removed"] == 1
    assert not link.is_symlink() and link.read_text(encoding="utf-8") \
        == "extends Node\n"


# --------------------------------------------------------------------------
# _map_facts
# --------------------------------------------------------------------------
def test_map_facts_extracts_lines_and_test_dirs(tmp_path):
    amap = tmp_path / "map.json"
    amap.write_text(
        '{"arms": {"gut_full": {'
        '"line_to_tests": {"utils/calc.gd": {"4": ["t.gd::t1"], '
        '"7": ["t.gd::t1"]}}, '
        '"file_to_tests": {"utils/calc.gd": ["test_a.gd::t1"]}}},'
        '"tests_face": {"tests/unit/test_a.gd": "hash"},'
        '"target_files": ["utils/calc.gd"]}',
        encoding="utf-8")
    facts = e2e._map_facts(str(amap), "utils/calc.gd")
    assert facts["lines"] == [4, 7]
    assert facts["test_dirs"] == ["res://tests/unit/"]
    assert facts["target_files"] == ["utils/calc.gd"]
