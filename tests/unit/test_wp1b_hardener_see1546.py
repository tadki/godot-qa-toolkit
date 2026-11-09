"""SEE-1546 WP1b hardener: coverage closure and contract pins for the registry
change surface (§SPEC-017~020). Test-side only; product code untouched.

Pins the guard/defensive lanes the functional scenarios do not reach: the
registry walk's malformed-node branches, the manifest ownership seams
(unowned probes, unknown spans, paren-depth census), the parity task
plumbing (collect/serial-fallback/aggregate closure), the snapshot escape
and identity-candidate rows, and the CLI error/success lanes of
`gqt parity` / `gqt registry diff` in-process.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gdtoolkit.parser import parser as gd_parser
from lark import Tree
from lark.lexer import Token

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.complexity.parity import (
    _aggregate_results,
    _collect_tasks,
    _lane_for,
    _run_tasks,
    parity_paths,
)
from godot_qa_toolkit.registry import functions as reg_functions
from godot_qa_toolkit.registry.functions import (
    _first_name,
    _start_line,
    build_file_registry,
)
from godot_qa_toolkit.registry.manifest import (
    _statement_lines,
    build_probe_manifest_v2,
)
from godot_qa_toolkit.registry.snapshot import (
    DEFAULT_ESCAPE_DIRS,
    _in_escape_dir,
    diff_registries,
)

GD_SNIPPET = "func a():\n\treturn 1\n"
LAMBDA_SNIPPET = (
    "func host(x):\n"
    "\tvar f := func():\n"
    "\t\tif x:\n"
    "\t\t\treturn 1\n"
    "\t\treturn 0\n"
    "\treturn f\n"
)


# --------------------------------------------------------------------------
# registry.functions: walk guards (§SPEC-017)
# --------------------------------------------------------------------------


def test_first_name_returns_none_without_name_token():
    assert _first_name(Tree("func_def", [Token("LPAR", "(")])) is None


def test_first_name_skips_non_name_tokens():
    header = Tree("func_header", [Token("LPAR", "("), Token("NAME", "foo")])
    assert _first_name(Tree("func_def", [header])) == "foo"


def test_start_line_agrees_between_metadata_modes():
    """Leaf-token lines and tree-meta lines must agree — the meta branch of
    _start_line exists for metadata parses and must not shift the span."""
    tree = gd_parser.parse(GD_SNIPPET, gather_metadata=True)
    func_def = [s for s in tree.iter_subtrees() if s.data == "func_def"][0]
    assert _start_line(func_def) == 1


DUP_SNIPPET = "func a():\n\treturn 1\n\nfunc a():\n\treturn 2\n"


def test_duplicate_names_get_occurrence_suffix():
    """The identity key's fourth component: same-class occurrence order turns
    the second same-name function into `#2` instead of colliding."""
    snapshot = build_file_registry(DUP_SNIPPET, file_label="x.gd")
    fids = [e["fid"] for e in snapshot["entries"] if e["kind"] == "func_def"]
    assert fids == ["x.gd::a", "x.gd::a#2"]


def test_parity_pairs_duplicate_names_by_occurrence():
    from godot_qa_toolkit.complexity.parity import pair_file

    report = pair_file(DUP_SNIPPET, file_label="x.gd")
    rows = [r for r in report["rows"] if r["name"] == "a"]
    assert len(rows) == 2
    assert [r["fid"] for r in rows] == ["x.gd::a", "x.gd::a#2"]
    assert all(r["pairing"] == "occurrence" for r in rows)
    assert [r["radon_cc"] for r in rows] == [1, 1]
    assert all(r["lane"] == "match" for r in rows)


class _StubParser:
    def __init__(self, tree: Tree):
        self._tree = tree

    def parse(self, _src: str, **_kwargs) -> Tree:
        return self._tree


def test_walk_registers_module_level_lambda(monkeypatch):
    """Module-level lambdas are a language-unreachable placeholder lane
    (plan §4.4); injected synthetically they must register as standalone
    `<lambda#k>` entries with a host-less fid and never crash."""
    lam = Tree("lambda", [Token("NAME", "x"), Token("NUMBER", "1")])
    monkeypatch.setattr(
        reg_functions, "gdtoolkit_parser", _StubParser(Tree("start", [lam]))
    )
    snapshot = build_file_registry(GD_SNIPPET, file_label="x.gd")
    assert snapshot["parse"] == "ok"
    lambdas = [e for e in snapshot["entries"] if e["kind"] == "lambda"]
    assert len(lambdas) == 1
    row = lambdas[0]
    assert row["name"] == "<lambda>"
    assert row["fid"] == "x.gd::.<lambda#1>"
    assert row["host_fid"] == "x.gd::"
    assert row["occurrence"] == 1


def test_static_wrapper_without_func_def_child_is_skipped(monkeypatch):
    tree = Tree("start", [Tree("static_func_def", [])])
    monkeypatch.setattr(reg_functions, "gdtoolkit_parser", _StubParser(tree))
    snapshot = build_file_registry(GD_SNIPPET, file_label="x.gd")
    assert snapshot["entries"] == []


def test_unnamed_class_def_never_becomes_entry_or_scope(monkeypatch):
    """A class_def with no NAME (anywhere in its subtree) registers neither
    an inner class nor a scope segment, and its subtree is not walked at all
    — only named classes contribute (WP1a D2 discipline carried over)."""
    tree = Tree("start", [Tree("class_def", [Tree("func_def", [])])])
    monkeypatch.setattr(reg_functions, "gdtoolkit_parser", _StubParser(tree))
    snapshot = build_file_registry(GD_SNIPPET, file_label="x.gd")
    assert snapshot["inner_classes"] == []
    assert snapshot["entries"] == []


# --------------------------------------------------------------------------
# registry.manifest: ownership and census seams (§SPEC-018)
# --------------------------------------------------------------------------


def test_statement_lines_skip_paren_depth_lines():
    """Only paren-depth-0 statement lines belong to the census — a doctored
    depth table must exclude them rather than miscount."""
    tree = gd_parser.parse(GD_SNIPPET, gather_metadata=True)
    func_def = [s for s in tree.iter_subtrees() if s.data == "func_def"][0]
    assert _statement_lines(func_def, [0, 0]) == (2,)
    assert _statement_lines(func_def, [0, 1]) == ()


def _registry_with_unknown_span() -> dict:
    return {
        "file": "x.gd",
        "entries": [
            {
                "fid": "x.gd::ghost",
                "kind": "func_def",
                "instrumentable": True,
                "start_line": 999,
                "end_line": 1000,
            }
        ],
    }


def test_manifest_unknown_span_yields_empty_census_row():
    """A registry span that no re-parsed node claims keeps its measured row
    (t_lines 0) instead of crashing — census stays complete."""
    result = build_probe_manifest_v2(GD_SNIPPET, _registry_with_unknown_span())
    rows = {r["fid"]: r for r in result["per_function"]}
    assert rows["x.gd::ghost"]["t_lines"] == 0
    assert result["census"]["probes_total"] > 0


def test_manifest_unowned_probe_is_counted(monkeypatch):
    """A probe line outside every registry span is counted unowned and lands
    in the probe plan with fid=None — ownership gaps stay fail-visible."""
    import godot_qa_toolkit.registry.manifest as manifest_mod
    from godot_qa_toolkit.coverage.runner import ExecutableLine

    real = manifest_mod._collect_executable_lines

    def with_orphan(src_arg):
        return real(src_arg) + [ExecutableLine(line=99, node="expr_stmt")]

    monkeypatch.setattr(manifest_mod, "_collect_executable_lines", with_orphan)
    registry = build_file_registry(GD_SNIPPET, file_label="x.gd")
    result = build_probe_manifest_v2(GD_SNIPPET, registry)
    assert result["census"]["unowned"] == 1
    assert result["census"]["owners_complete"] is False
    assert any(row["fid"] is None and row["line"] == 99 for row in result["probe_plan"])


def test_manifest_ambiguous_spans_fail_visible():
    """Two identical spans (same start and end, different fid) are genuine
    ambiguity: the probe stays unowned and the census reports it — never a
    guessed pairing."""
    entries = [
        {
            "fid": "x.gd::dup1",
            "kind": "func_def",
            "instrumentable": True,
            "start_line": 1,
            "end_line": 2,
        },
        {
            "fid": "x.gd::dup2",
            "kind": "func_def",
            "instrumentable": True,
            "start_line": 1,
            "end_line": 2,
        },
    ]
    result = build_probe_manifest_v2(GD_SNIPPET, {"file": "x.gd", "entries": entries})
    assert result["census"]["ambiguous"] == 1
    assert result["census"]["owners_complete"] is False
    probe = [r for r in result["probe_plan"] if r["line"] == 2][0]
    assert probe["fid"] is None


def test_run_tasks_falls_back_to_serial_on_pool_breakage(tmp_path, monkeypatch):
    """obs-1 guard: a broken process pool must degrade to the serial path and
    still return the complete result set (equivalence, not an error)."""
    import concurrent.futures
    import concurrent.futures.process

    solo = tmp_path / "solo.gd"
    solo.write_text(GD_SNIPPET, encoding="utf-8")

    class _BrokenPool:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, tasks):
            raise concurrent.futures.process.BrokenProcessPool()

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _BrokenPool)
    results = _run_tasks([(str(solo), "solo.gd")])
    assert len(results) == 1
    label, report, registry = results[0]
    assert label == "solo.gd"
    assert report["lanes"]["match"] == 1
    assert registry["entries"][0]["fid"] == "solo.gd::a"


# --------------------------------------------------------------------------
# complexity.parity: task plumbing and lanes (§SPEC-019)
# --------------------------------------------------------------------------


def test_lane_for_missing_in_radon():
    assert _lane_for(3, None, 1) == "missing_in_radon"
    assert _lane_for(3, 3, 0) == "match"
    assert _lane_for(3, 2, 1) == "lambda_gap"
    assert _lane_for(3, 2, 2) == "mismatch"


def test_collect_tasks_globs_dedups_and_falls_back(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "a.gd").write_text(GD_SNIPPET, encoding="utf-8")
    (root / "sub" / "b.gd").write_text(GD_SNIPPET, encoding="utf-8")
    outside = tmp_path / "outside.gd"
    outside.write_text(GD_SNIPPET, encoding="utf-8")

    tasks = _collect_tasks(
        [root, outside, root / "sub" / "b.gd"], project_root=str(root)
    )
    labels = sorted(label for _, label in tasks)
    assert labels == ["a.gd", "outside.gd", "sub/b.gd"]
    # the duplicated explicit path was deduplicated by resolved path
    assert len(tasks) == 3


def test_run_tasks_serial_for_single_task(tmp_path):
    solo = tmp_path / "solo.gd"
    solo.write_text(GD_SNIPPET, encoding="utf-8")
    results = _run_tasks([(str(solo), "solo.gd")])
    assert len(results) == 1
    label, report, registry = results[0]
    assert label == "solo.gd"
    assert report["lanes"]["match"] == 1
    assert registry["entries"][0]["fid"] == "solo.gd::a"


def test_aggregate_results_closure_counts_and_sort():
    ok_a = (
        "b.gd",
        {
            "file": "b.gd",
            "func_entries": 2,
            "lambda_entries": 1,
            "parse": "ok",
            "lanes": {"match": 2, "lambda_gap": 1},
        },
        {
            "entries": [
                {"kind": "func_def", "lex_scope": [], "lambda_gap": 1},
                {"kind": "func_def", "lex_scope": [], "lambda_gap": 0},
                {"kind": "lambda", "lex_scope": []},
            ],
            "inner_classes": [],
        },
    )
    err_b = (
        "a.gd",
        {
            "file": "a.gd",
            "func_entries": 0,
            "lambda_entries": 0,
            "parse": "error",
            "lanes": {},
        },
        {"entries": [], "inner_classes": ["Inner"]},
    )
    file_reports, snapshot_files, summary = _aggregate_results([ok_a, err_b])
    assert [r["file"] for r in file_reports] == ["a.gd", "b.gd"]
    assert summary["files"] == 2
    assert summary["unparseable"] == 1
    assert summary["inner_class_files"] == 1
    assert summary["inner_class_method_entries"] == 0
    assert summary["lambda_gap_total"] == 1
    assert summary["closure_ok"] is True
    assert set(snapshot_files) == {"a.gd", "b.gd"}

    broken = (
        "c.gd",
        {
            "file": "c.gd",
            "func_entries": 1,
            "lambda_entries": 0,
            "parse": "ok",
            "lanes": {"match": 5},
        },
        {"entries": [{"kind": "func_def", "lex_scope": [], "lambda_gap": 0}], "inner_classes": []},
    )
    _, _, summary_bad = _aggregate_results([broken])
    assert summary_bad["closure_ok"] is False


def test_parity_paths_end_to_end(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "plain.gd").write_text(GD_SNIPPET, encoding="utf-8")
    (root / "lamb.gd").write_text(LAMBDA_SNIPPET, encoding="utf-8")
    report = parity_paths([root], project_root=str(root))
    assert report["tool"] == "parity" and report["ok"] is True
    assert report["summary"]["func_entries"] == 2
    assert report["summary"]["lambda_entries"] == 1
    assert report["summary"]["closure_ok"] is True
    assert report["summary"]["lambda_gap_total"] >= 1
    assert [f["file"] for f in report["files"]] == ["lamb.gd", "plain.gd"]
    assert "_snapshot_files" in report


# --------------------------------------------------------------------------
# registry.snapshot: escape dirs and identity candidates (§SPEC-020)
# --------------------------------------------------------------------------


def test_in_escape_dir_matches_default_dirs():
    assert _in_escape_dir("acceptance/generated/x.gd::f", DEFAULT_ESCAPE_DIRS) is True
    assert _in_escape_dir("addons/x.gd::f", DEFAULT_ESCAPE_DIRS) is True
    assert _in_escape_dir(".godot/x.gd::f", DEFAULT_ESCAPE_DIRS) is True
    assert _in_escape_dir("systems/x.gd::f", DEFAULT_ESCAPE_DIRS) is False


def test_in_escape_dir_preserves_dot_slash_prefix_verbatim():
    """Actual contract for the defensive "./" lane: the prefix is preserved
    verbatim, so escape dirs do NOT match dot-slash fids. Unreachable from
    CLI labels (always project-root-relative, never "./") — quirk registered,
    pinned here so the lane cannot silently flip."""
    assert _in_escape_dir("./addons/x.gd::f", DEFAULT_ESCAPE_DIRS) is False
    assert _in_escape_dir("./systems/x.gd::f", DEFAULT_ESCAPE_DIRS) is False


def _entry(fid: str, body_hash: str, kind: str = "func_def", scope: list | None = None) -> dict:
    return {
        "fid": fid,
        "kind": kind,
        "lex_scope": scope if scope is not None else [],
        "body_hash": body_hash,
    }


def test_diff_identity_changed_candidate_and_governance_net():
    old = {"files": {"a.gd": {"entries": [_entry("a.gd::f", "H1"), _entry("a.gd::keep", "K1")]}}}
    new = {
        "files": {
            "a.gd": {
                "entries": [
                    _entry("a.gd::f_renamed", "H1"),
                    _entry("a.gd::keep", "K1"),
                ]
            }
        }
    }
    diff = diff_registries(old, new)
    rows = {r["fid"]: r for r in diff["rows"]}
    assert rows["a.gd::keep"]["status"] == "unchanged"
    assert rows["a.gd::f"]["status"] == "removed_identity_candidate"
    assert rows["a.gd::f"]["new_fid"] == "a.gd::f_renamed"
    added_row = rows["a.gd::f_renamed"]
    assert added_row["status"] == "identity_changed_candidate"
    assert added_row["evidence"] == "body_hash_identical"
    assert added_row["human_final"] is True
    summary = diff["summary"]
    # §SPEC-020 jq gate: countable_added == added - scope_escape
    assert summary["governance_net"]["countable_added"] == (
        summary["added"] - summary["scope_escape"]
    )


# --------------------------------------------------------------------------
# CLI lanes: gqt parity / gqt registry diff (§SPEC-017/020 in-process)
# --------------------------------------------------------------------------


def _parse(*argv: str):
    return build_parser().parse_args(list(argv))


def test_cmd_parity_missing_paths_contract(tmp_path, capsys):
    args = _parse("parity", str(tmp_path / "nope.gd"), "--project-root", str(tmp_path))
    rc = cli_mod.cmd_parity(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "parity"
    assert "paths not found" in out["failures"][0]["reason"]


def test_cmd_parity_success_writes_report_and_registry(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "solo.gd").write_text(GD_SNIPPET, encoding="utf-8")
    report_path = tmp_path / "parity.json"
    registry_path = tmp_path / "reg.json"
    args = _parse(
        "parity", str(root), "--project-root", str(root),
        "--report", str(report_path), "--registry-out", str(registry_path),
    )
    rc = cli_mod.cmd_parity(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["tool"] == "parity"
    assert out["summary"]["closure_ok"] is True
    assert out["summary"]["registry_out"] == str(registry_path)
    snapshot = json.loads(registry_path.read_text(encoding="utf-8"))
    assert snapshot["summary"] == {
        "func_entries": 1,
        "lambda_entries": 0,
        "unparseable": 0,
        "inner_class_files": 0,
    }
    assert "solo.gd" in snapshot["files"]
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["summary"]["closure_ok"] is True


def test_cmd_parity_without_registry_out_skips_snapshot(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "solo.gd").write_text(GD_SNIPPET, encoding="utf-8")
    report_path = tmp_path / "parity.json"
    args = _parse(
        "parity", str(root), "--project-root", str(root), "--report", str(report_path)
    )
    rc = cli_mod.cmd_parity(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    assert out["summary"]["registry_out"] is None


def test_cmd_registry_fail_loud_on_missing_snapshot(tmp_path, capsys):
    args = _parse(
        "registry", "diff",
        str(tmp_path / "missing.json"), str(tmp_path / "also-missing.json"),
    )
    rc = cli_mod.cmd_registry(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "registry"
    assert "snapshot load failed" in out["failures"][0]["reason"]


def test_cmd_registry_success_writes_diff(tmp_path, capsys):
    old = {"files": {"a.gd": {"entries": [_entry("a.gd::f", "H1")]}}}
    new = {"files": {"a.gd": {"entries": [_entry("a.gd::f", "H2")]}}}
    old_path = tmp_path / "old.json"
    new_path = tmp_path / "new.json"
    out_path = tmp_path / "diff.json"
    old_path.write_text(json.dumps(old), encoding="utf-8")
    new_path.write_text(json.dumps(new), encoding="utf-8")
    args = _parse(
        "registry", "diff", str(old_path), str(new_path), "--out", str(out_path)
    )
    rc = cli_mod.cmd_registry(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True and out["tool"] == "registry"
    assert out["diff_version"] == "func-registry-diff/1"
    assert out["failures"] == []
    assert out["summary"]["edited"] == 1
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert written["diff_version"] == "func-registry-diff/1"
    assert written["failures"] == []
    assert written["summary"]["edited"] == 1
    assert written["summary"]["governance_net"]["countable_added"] == (
        written["summary"]["added"] - written["summary"]["scope_escape"]
    )
