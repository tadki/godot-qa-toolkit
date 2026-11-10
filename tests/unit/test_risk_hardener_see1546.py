"""SEE-1546 M2 hardener: coverage closure and contract pins for the risk
change surface (§SPEC-012). Test-side only; product code untouched.

These pin the guard/error lanes the functional scenarios do not reach on a
healthy run: the CLI error lanes of `cmd_risk`, the report-document key-order
freeze, the ranking sort direction, the coverage-runner `hits_lines` exposure
contract, and the metadata-less census skip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from lark import Tree

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.coverage.runner import ExecutableLine, _coverage_report
from godot_qa_toolkit.risk.contract import build_shadow_record
from godot_qa_toolkit.registry.manifest import _statement_lines
from godot_qa_toolkit.risk.shadow import _report_document

GD_SNIPPET = "func a():\n\treturn 1\n"


# --------------------------------------------------------------------------
# coverage runner: hits_lines exposure is the risk layer's per-function
# attribution source — pin it at the contract level.
# --------------------------------------------------------------------------


def test_coverage_report_exposes_hits_lines():
    lines = [
        ExecutableLine(line=2, node="return_stmt"),
        ExecutableLine(line=3, node="expr_stmt"),
    ]
    report = _coverage_report(
        file="x.gd",
        original_src=GD_SNIPPET,
        executable_lines=lines,
        hits={3, 2},
        probes_fired=2,
        min_percent=0.0,
    )
    assert report["summary"]["hits_lines"] == [2, 3]
    assert report["ok"] is True


# --------------------------------------------------------------------------
# identity census: lark Trees without gather_metadata carry no meta, so
# _node_line is None — the census must skip them, not crash or miscount.
# --------------------------------------------------------------------------


def test_function_statement_lines_skips_metadataless_nodes():
    """PR-A re-anchor: the census traversal lives in registry.manifest
    and must skip nodes without `.data` (metadataless/Token shapes)."""
    assert _statement_lines(Tree("pass_stmt", []), [0],
                            skip_subtrees=("lambda",)) == ()


# --------------------------------------------------------------------------
# report document: key order and every literal are the §SPEC-010 JSON
# contract — jq gates and QA pin them, so the freeze must be tested.
# --------------------------------------------------------------------------


def test_report_document_key_order_is_frozen():
    doc = _report_document(
        ranking=[],
        unknown=[],
        core_full=[],
        noise=[],
        negatives=[],
        per_file=[],
        sample_files=0,
        negative_files=0,
    )
    assert list(doc.keys()) == [
        "report",
        "version",
        "evidence_level",
        "diagnostic_only",
        "dynamic_cache",
        "metric",
        "metric_version",
        "unknown",
        "ranking",
        "core_full",
        "noise",
        "acceptance_cost",
        "routing",
        "confidence",
        "negatives",
        "files",
        "records",
    ]
    assert doc["report"] == "shadow-report"
    assert doc["version"] == "shadow/1"
    assert doc["metric"] == "crap_line"
    assert doc["metric_version"] == "crap-line/1"
    assert doc["evidence_level"] == "exploratory"
    assert doc["diagnostic_only"] is True and doc["dynamic_cache"] is False
    assert doc["unknown"]["share"] == 0.0
    assert "never extrapolate" in doc["ranking"]["note"]


def test_risk_static_block_computes_per_file_max_and_share():
    from godot_qa_toolkit.risk.shadow import _risk_static_block

    report = {
        "files": [{"static_seconds": 0.02}, {"static_seconds": 0.01}],
        "core_full": [{"core_wall_s": 1.0, "full_wall_s": 3.0}],
    }
    block = _risk_static_block(report)
    assert block["max_seconds_per_file"] == 0.02
    assert block["max_share_of_total"] == pytest.approx(0.02 / 4.0, abs=1e-4)
    assert _risk_static_block({"files": [], "core_full": []}) == {
        "max_seconds_per_file": 0.0,
        "max_share_of_total": 0.0,
    }


def test_report_ranking_sorts_by_score_descending():
    hot = build_shadow_record(
        function_id="f::hot", name="hot", lex_path="hot",
        start_line=1, end_line=2, cc=4, covered=2, total=4,
        cov_source="gut_full",
    )
    cold = build_shadow_record(
        function_id="f::cold", name="cold", lex_path="cold",
        start_line=4, end_line=5, cc=1, covered=3, total=3,
        cov_source="gut_full",
    )
    doc = _report_document(
        ranking=[cold, hot],
        unknown=[],
        core_full=[],
        noise=[],
        negatives=[],
        per_file=[],
        sample_files=1,
        negative_files=0,
    )
    top = doc["ranking"]["top"]
    assert [row["function_id"] for row in top] == ["f::hot", "f::cold"]
    assert top[0]["score"] > top[1]["score"]


# --------------------------------------------------------------------------
# CLI error/success lanes of cmd_risk (in-process so coverage sees cli.py).
# --------------------------------------------------------------------------


def _parse(*argv: str):
    return build_parser().parse_args(list(argv))


def test_cmd_risk_missing_file_contract(tmp_path, capsys):
    args = _parse("risk", str(tmp_path / "nope.gd"), "--project-root", str(tmp_path))
    rc = cli_mod.cmd_risk(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "risk"
    assert "target files not found" in out["failures"][0]["reason"]


def test_cmd_risk_godot_bin_contract(tmp_path, capsys):
    src = tmp_path / "solo.gd"
    src.write_text(GD_SNIPPET, encoding="utf-8")
    args = _parse(
        "risk", str(src), "--project-root", str(tmp_path),
        "--godot-bin", str(tmp_path / "missing-godot"),
    )
    rc = cli_mod.cmd_risk(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ok"] is False and out["tool"] == "risk"
    assert out["summary"]["error"]


def test_cmd_risk_skip_measure_writes_report(tmp_path, capsys):
    src = tmp_path / "solo.gd"
    src.write_text(GD_SNIPPET, encoding="utf-8")
    perf = tmp_path / "perf.json"
    perf.write_text(
        json.dumps(
            {
                "complexity": {"baseline_median_s": 2.0, "head_median_s": 4.0},
                "coverage": [
                    {"file": "solo.gd", "baseline_median_s": 2.0, "head_median_s": 1.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "shadow-report.json"
    args = _parse(
        "risk", str(src), "--project-root", str(tmp_path),
        "--skip-measure", "--report", str(report_path),
        "--performance-file", str(perf),
    )
    rc = cli_mod.cmd_risk(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ok"] is True and out["tool"] == "risk"
    assert out["summary"]["records"] == 1 and out["summary"]["scored"] == 0
    assert out["summary"]["diagnostic_only"] is True
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    assert doc["diagnostic_only"] is True and doc["dynamic_cache"] is False
    assert doc["records"][0]["score_status"] == "run_error"
    assert doc["performance"]["complexity"]["head_median_ratio"] == pytest.approx(2.0)
    assert doc["performance"]["coverage"][0]["head_median_ratio"] == pytest.approx(0.5)
    assert doc["performance"]["extra_gut_rounds"] == 0
    assert doc["performance"]["risk_static"]["max_seconds_per_file"] >= 0.0


def test_cmd_risk_without_performance_file_uses_zero_baseline(tmp_path, capsys):
    src = tmp_path / "solo.gd"
    src.write_text(GD_SNIPPET, encoding="utf-8")
    report_path = tmp_path / "out.json"
    args = _parse(
        "risk", str(src), "--project-root", str(tmp_path),
        "--skip-measure", "--report", str(report_path),
    )
    rc = cli_mod.cmd_risk(args)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] is True
    doc = json.loads(report_path.read_text(encoding="utf-8"))
    assert doc["performance"]["complexity"]["head_median_ratio"] == 0.0
    assert doc["acceptance_cost"]["samples"] == []


# --------------------------------------------------------------------------
# identity/eligibility fault injection: the executable-line set is the seam
# between the coverage runner and the T census — a diverging set must keep the
# file out of the eligible lane instead of pretending full measurement.
# --------------------------------------------------------------------------


_MULTILINE_FUNC = "func a(x):\n\tif x:\n\t\treturn 1\n\treturn 0\n"


def test_file_profile_flags_t_not_equal_i(monkeypatch):
    """An instrumented line the statement census does not count (T≠I) must
    keep the file ineligible even without any gap-lane marker."""
    import godot_qa_toolkit.registry.manifest as manifest_mod

    real = manifest_mod.collect_executable_lines

    def drop_if_line(src_arg):
        return [row for row in real(src_arg) if row.line != 2]

    monkeypatch.setattr(manifest_mod, "collect_executable_lines", drop_if_line)
    from godot_qa_toolkit.risk.identity import file_profile
    profile = file_profile(_MULTILINE_FUNC, file_label="x.gd")
    assert profile["has_lambda"] is False and profile["has_inner_class"] is False
    assert profile["gap_lane"] is None
    assert profile["t_equal_i"] is False
    assert profile["owners_complete"] is True
    assert profile["eligible"] is False


def test_file_profile_flags_probe_without_owner(monkeypatch):
    """An instrumented line outside every function span leaves ownership
    incomplete — the file must never enter the eligible lane."""
    import godot_qa_toolkit.registry.manifest as manifest_mod

    real = manifest_mod.collect_executable_lines

    def with_orphan(src_arg):
        return real(src_arg) + [ExecutableLine(line=99, node="expr_stmt")]

    monkeypatch.setattr(manifest_mod, "collect_executable_lines", with_orphan)
    from godot_qa_toolkit.risk.identity import file_profile
    profile = file_profile(GD_SNIPPET, file_label="x.gd")
    assert profile["t_equal_i"] is False
    assert profile["owners_complete"] is False
    assert profile["eligible"] is False
    assert profile["gap_lane"] is None
    assert any(row["line"] == 99 and row["function_id"] is None for row in profile["probes"])


def test_file_profile_flags_ambiguous_ownership(monkeypatch):
    """True ownership ambiguity is reported, never guessed — and ambiguous
    attribution keeps the file out of the eligible lane even though the
    probe/line sets still match (T=I)."""
    import godot_qa_toolkit.registry.manifest as manifest_mod

    def all_ambiguous(spans, plan_lines):
        plan = [
            {"probe_id": i, "line": line, "fid": "x.gd::a",
             "owner_kind": "func", "ownership": "ambiguous"}
            for i, line in enumerate(plan_lines)
        ]
        i_by_fid = {"x.gd::a": len(plan_lines)}
        return plan, 0, len(plan_lines), i_by_fid

    monkeypatch.setattr(manifest_mod, "_resolve_ownership", all_ambiguous)
    from godot_qa_toolkit.risk.identity import file_profile
    profile = file_profile(GD_SNIPPET, file_label="x.gd")
    assert profile["t_equal_i"] is True
    assert profile["owners_complete"] is False
    assert profile["eligible"] is False
    assert profile["gap_lane"] is None
    assert all(row["owner_ambiguous"] for row in profile["probes"])


def test_function_census_excludes_paren_depth_lines():
    """The per-function census counts depth-0 statement lines only: a line
    sitting inside an open bracket (paren depth > 0) is not a statement start."""
    from godot_qa_toolkit.registry.manifest import _node_by_span
    from gdtoolkit.parser import parser

    tree = parser.parse(GD_SNIPPET, gather_metadata=True)
    func_def = _node_by_span(tree, 1)
    assert _statement_lines(func_def, [0, 0]) == (2,)
    assert _statement_lines(func_def, [0, 1]) == ()


def test_report_routing_thresholds():
    def record(fid, cc, covered, total):
        return build_shadow_record(
            function_id=fid, name=fid, lex_path=fid,
            start_line=1, end_line=2, cc=cc, covered=covered, total=total,
            cov_source="gut_full",
        )

    tidy = record("tidy", 1, 9, 10)          # c=0.9 >= 0.8
    mid = record("mid", 2, 3, 5)             # c=0.6 in [0.5, 0.8): neither lane
    low = record("low", 1, 1, 4)             # c=0.25 < 0.5
    empty = _report_document(
        ranking=[], unknown=[], core_full=[], noise=[], negatives=[],
        per_file=[], sample_files=0, negative_files=0,
    )
    assert empty["routing"] == {"tidy": 0, "harti_safety_net": 0, "tool_chain_gap": 0}
    doc = _report_document(
        ranking=[tidy, mid, low], unknown=[], core_full=[], noise=[],
        negatives=[], per_file=[], sample_files=1, negative_files=0,
    )
    assert doc["routing"]["tidy"] == 1
    assert doc["routing"]["harti_safety_net"] == 1


def test_run_shadow_flags_unstable_rerun():
    from godot_qa_toolkit.risk.shadow import run_shadow

    calls = {"n": 0}

    def measure(label, source, tests):
        calls["n"] += 1
        hits = [2] if calls["n"] != 3 else []
        return {
            "ok": True,
            "summary": {
                "total_lines": 1,
                "covered_lines": len(hits),
                "probes_fired": 1,
                "hits_lines": hits,
            },
        }

    report = run_shadow(
        eligible_sources={"a.gd": GD_SNIPPET},
        negative_sources={},
        measure=measure,
    )
    assert report["noise"][0]["rerun_identical"] is False
    assert report["noise"][0]["status"] == "unstable"


# --------------------------------------------------------------------------
# WP2 PR-A: identity delegation adapter guards (§SPEC-024 surface)
# --------------------------------------------------------------------------


def test_build_probe_manifest_infers_label_from_identities():
    from godot_qa_toolkit.risk.identity import build_probe_manifest

    rows, lines = build_probe_manifest(
        GD_SNIPPET, identities=[{"function_id": "my.gd::a"}], file_label=None
    )
    assert lines and rows
    assert rows[0]["function_id"].startswith("my.gd::")


def test_build_probe_manifest_explicit_label_skips_inference():
    from godot_qa_toolkit.risk.identity import build_probe_manifest

    rows, lines = build_probe_manifest(GD_SNIPPET, identities=None, file_label="x.gd")
    assert lines and rows
    assert rows[0]["function_id"].startswith("x.gd::")


def test_uses_sanitized_projection_survives_conversion_failure(monkeypatch):
    """A gd2py conversion failure must read as 'no sanitized projection'
    (never crash the eligibility walk)."""
    import godot_qa_toolkit.risk.identity as identity_mod

    def boom(_src):
        raise RuntimeError("conversion exploded")

    monkeypatch.setattr(identity_mod, "convert_code", boom)
    assert identity_mod._uses_sanitized_projection(GD_SNIPPET) is False
    profile = identity_mod.file_profile(GD_SNIPPET, file_label="x.gd")
    assert profile["gap_lane"] is None


def test_build_probe_rows_shape_is_pinned():
    from godot_qa_toolkit.risk.identity import build_probe_manifest

    rows, _ = build_probe_manifest(GD_SNIPPET, identities=None, file_label="x.gd")
    assert set(rows[0]) == {
        "probe_id",
        "line",
        "function_id",
        "owner_ambiguous",
        "owner_kind",
    }


def test_probe_rows_carry_lambda_owner_kind():
    from godot_qa_toolkit.risk.identity import build_probe_manifest

    lam = "func a():\n\tvar f := func():\n\t\treturn 1\n\treturn f\n"
    rows, _ = build_probe_manifest(lam, identities=None, file_label="x.gd")
    kinds = {r["owner_kind"] for r in rows}
    assert kinds == {"func", "lambda"}
    lam_rows = [r for r in rows if r["owner_kind"] == "lambda"]
    assert all(r["function_id"].endswith("<lambda#1>") for r in lam_rows)


def test_span_closure_ends_before_next_structure():
    from godot_qa_toolkit.registry.functions import build_file_registry

    src = "func a():\n\treturn 1\n\nfunc b():\n\treturn 2\n"
    snapshot = build_file_registry(src, "x.gd")
    spans = {e["name"]: (e["start_line"], e["end_line"]) for e in snapshot["entries"]}
    assert spans == {"a": (1, 3), "b": (4, 5)}


def test_span_closure_keeps_pure_punctuation_body_owned():
    """§SPEC-024 span-closure guard, independently reproduced: a body line
    with zero leaf tokens (`return []`) must stay inside the closed span, so
    its probe keeps an owner instead of silently going unowned."""
    from godot_qa_toolkit.registry.functions import build_file_registry
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2

    src = "func a():\n\treturn []\n"
    snapshot = build_file_registry(src, "x.gd")
    row = snapshot["entries"][0]
    assert (row["start_line"], row["end_line"]) == (1, 2)
    manifest = build_probe_manifest_v2(src, snapshot)
    assert manifest["census"]["unowned"] == 0
    probe = manifest["probe_plan"][0]
    assert probe["fid"] == "x.gd::a"
    assert probe["ownership"] == "owned"


def test_probe_plan_ownership_flags_cover_all_states(monkeypatch):
    """probe-manifest/2 per-line ownership flag: owned / unowned / ambiguous
    must be emitted verbatim for consumers (WP2 PR-A contract)."""
    import godot_qa_toolkit.registry.manifest as manifest_mod
    from godot_qa_toolkit.coverage.runner import ExecutableLine
    from godot_qa_toolkit.registry.functions import build_file_registry
    from godot_qa_toolkit.registry.manifest import build_probe_manifest_v2

    dup_entries = [
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
    ambiguous = build_probe_manifest_v2(
        GD_SNIPPET, {"file": "x.gd", "entries": dup_entries}
    )
    assert all(
        row["ownership"] == "ambiguous" and row["fid"] is None
        for row in ambiguous["probe_plan"]
    )

    snap = build_file_registry("func a():\n\treturn []\n", "x.gd")
    owned = build_probe_manifest_v2("func a():\n\treturn []\n", snap)
    assert owned["probe_plan"][0]["ownership"] == "owned"

    real = manifest_mod.collect_executable_lines

    def with_orphan(src_arg):
        return real(src_arg) + [ExecutableLine(line=99, node="expr_stmt")]

    monkeypatch.setattr(manifest_mod, "collect_executable_lines", with_orphan)
    orphan = build_probe_manifest_v2(GD_SNIPPET, snap)
    assert any(
        row["ownership"] == "unowned" and row["fid"] is None
        for row in orphan["probe_plan"]
    )


def test_spec024_private_census_symbol_debt_gate():
    """§SPEC-024 debt gate as a suite assertion: no private coverage.runner
    census symbol may be imported under risk/ (public run_coverage is the
    only legal measurement adapter until PR-C removes it)."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent.parent
    pattern = (
        r"from ..coverage.runner import .*( _EXECUTABLE_NODE_NAMES"
        r"|_collect_executable_lines|_node_line|_paren_depths)"
    )
    proc = subprocess.run(
        ["grep", "-rnE", pattern, "src/godot_qa_toolkit/risk/"],
        cwd=str(repo), capture_output=True,
    )
    assert proc.returncode == 1, proc.stdout.decode()
