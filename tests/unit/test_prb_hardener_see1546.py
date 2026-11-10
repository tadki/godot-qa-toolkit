"""SEE-1546 WP2 PR-B hardener: evidence lifecycle, sink guards, publicized
helpers, and the §SPEC-024/029 debt gates. Test-side only.

Covers the run_coverage evidence hooks offline (mocked GUT): started record →
bundle lands → sink cleared, abort/no-probe/empty-file/vacuous lanes, the
reparse-generation guard, --recover corner records, prune-sinks dir edges,
and the publicized lines/gd_ast_utils helpers.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from pathlib import Path

import pytest
from lark import Tree
from lark.lexer import Token

from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.coverage.evidence import (
    build_run_record,
    evidence_dir_for,
    make_run_uuid,
    read_run_record,
    recover_bundle,
    sink_name_for,
    write_run_record,
)

GD = "func f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n"


# --------------------------------------------------------------------------
# evidence.py helper guards
# --------------------------------------------------------------------------


def test_evidence_dir_for_none_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert evidence_dir_for(None) == Path.cwd()
    assert evidence_dir_for(str(tmp_path / "ev")) == tmp_path / "ev"


def test_read_run_record_missing_returns_none(tmp_path):
    assert read_run_record(tmp_path, "never-was") is None


def test_read_run_record_corrupt_reports_record_corrupt(tmp_path):
    (tmp_path / "bad.record.json").write_text("{not json", encoding="utf-8")
    record = read_run_record(tmp_path, "bad")
    assert record["status"] == "record_corrupt"
    assert record["run_uuid"] == "bad"


def test_recover_reports_unreadable_records(tmp_path):
    """§SPEC-025: --recover never crashes on malformed records — each lands
    in errors with the unreadable-record reason."""
    (tmp_path / "bad.record.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "arr.record.json").write_text("[1, 2]", encoding="utf-8")
    result = recover_bundle(tmp_path)
    assert result["recovered"] == []
    assert len(result["errors"]) == 2
    assert all(e.startswith("unreadable record:") for e in result["errors"])


def test_recover_skips_bundle_written_records(tmp_path):
    write_run_record(tmp_path, {"run_uuid": "u-done", "status": "bundle_written"})
    result = recover_bundle(tmp_path)
    assert result == {"recovered": [], "errors": []}


def test_recover_reports_missing_sink(tmp_path):
    """A started record whose sink is gone cannot be recovered — explicit
    error naming the sink, never a silent skip."""
    record = build_run_record(
        run_uuid="u-nosink", file_path="x.gd", gd_source=GD, tests_glob=None,
        probe_manifest={"probe_plan": [
            {"probe_id": 0, "line": 2, "fid": "x.gd::f", "owner_kind": "func"},
        ], "per_function": [
            {"fid": "x.gd::f", "t_lines": 1, "i_lines": 0, "t_equal_i": False,
             "t_line_ids": [2]},
        ]},
    )
    write_run_record(tmp_path, record)
    result = recover_bundle(tmp_path)
    assert result["recovered"] == []
    assert any("sink" in e and "missing" in e for e in result["errors"])


def test_recover_bundle_reports_missing_dir():
    result = recover_bundle(Path("/nonexistent/see1546-ev-dir"))
    assert result["recovered"] == []
    assert result["errors"] == ["evidence dir missing"]


def test_recover_ignores_invalid_probe_ids_in_sink(tmp_path):
    """Recovery attributes only plan-valid probe ids; garbage ids in the sink
    are skipped, never attributed."""
    uuid = "u-garbage"
    record = build_run_record(
        run_uuid=uuid, file_path="x.gd", gd_source=GD, tests_glob=None,
        probe_manifest={"probe_plan": [
            {"probe_id": 0, "line": 2, "fid": "x.gd::f", "owner_kind": "func"},
            {"probe_id": 1, "line": 3, "fid": "x.gd::f", "owner_kind": "func"},
        ], "per_function": [
            {"fid": "x.gd::f", "t_lines": 2, "i_lines": 2, "t_equal_i": True,
             "t_line_ids": [2, 3]},
        ]},
    )
    write_run_record(tmp_path, record)
    (tmp_path / sink_name_for(uuid)).write_text("0\n999\nnot-a-number\n", encoding="utf-8")

    result = recover_bundle(tmp_path)
    assert result["errors"] == []
    bundle = json.loads(Path(result["recovered"][0]["bundle"]).read_text(encoding="utf-8"))
    assert [c["covered"] for c in bundle["C"]] == [1]


def test_prune_sinks_with_user_dir_none_sweeps_project_root(tmp_path, monkeypatch):
    stale = tmp_path / "qa-coverage-hits-stale.txt"
    stale.write_text("x\n", encoding="utf-8")
    stale_time = time.time() - 9 * 86400
    os.utime(stale, (stale_time, stale_time))
    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner._resolve_user_sink_dir", lambda root: None
    )
    result = pytest.importorskip("godot_qa_toolkit.coverage.evidence").prune_sinks(
        str(tmp_path), retention_days=7
    )
    assert result["dirs_checked"] == [str(tmp_path)]
    assert any(d.endswith("stale.txt") for d in result["deleted"])


def test_prune_sinks_skips_nonexistent_user_dir(tmp_path, monkeypatch):
    stale = tmp_path / "qa-coverage-hits-stale.txt"
    stale.write_text("x\n", encoding="utf-8")
    stale_time = time.time() - 9 * 86400
    os.utime(stale, (stale_time, stale_time))
    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner._resolve_user_sink_dir",
        lambda root: str(tmp_path / "no-such-dir"),
    )
    from godot_qa_toolkit.coverage.evidence import prune_sinks

    result = prune_sinks(str(tmp_path), retention_days=7)
    assert str(tmp_path / "no-such-dir") in result["dirs_checked"]
    assert any(d.endswith("stale.txt") for d in result["deleted"])


def test_prune_sinks_survives_stat_errors(tmp_path, monkeypatch):
    stale = tmp_path / "qa-coverage-hits-stale.txt"
    stale.write_text("x\n", encoding="utf-8")
    stale_time = time.time() - 9 * 86400
    os.utime(stale, (stale_time, stale_time))
    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner._resolve_user_sink_dir", lambda root: None
    )

    real_stat = pathlib.Path.stat

    def boom(self, *args, **kwargs):
        if "qa-coverage-hits-" in str(self):
            raise OSError("stat unavailable")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "stat", boom)
    from godot_qa_toolkit.coverage.evidence import prune_sinks

    result = prune_sinks(str(tmp_path), retention_days=7)
    assert result["deleted"] == []
    assert stale in list(tmp_path.iterdir())


# --------------------------------------------------------------------------
# runner evidence lifecycle (mocked GUT — the §SPEC-025 hooks, offline)
# --------------------------------------------------------------------------


def _coverage_via_cli(tmp_path, monkeypatch, gd_text, evidence_out, gut_returns=None):
    """Drive cmd_coverage in-process with _run_gut_for_coverage mocked so the
    evidence lifecycle runs without Godot."""
    from godot_qa_toolkit.coverage import runner as runner_mod
    import io
    from contextlib import redirect_stdout

    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True, exist_ok=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    target = proj / "calc.gd"
    target.write_text(gd_text, encoding="utf-8")
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)

    if gut_returns == "success":
        monkeypatch.setattr(runner_mod, "_run_gut_for_coverage", lambda *a, **k: None)
        monkeypatch.setattr(runner_mod, "_read_hits", lambda *a, **k: ({2}, 1))
    elif gut_returns == "no_probe":
        monkeypatch.setattr(runner_mod, "_run_gut_for_coverage", lambda *a, **k: None)
        monkeypatch.setattr(runner_mod, "_read_hits", lambda *a, **k: ({}, 0))
    elif gut_returns == "abort_run_error":
        def abort(*a, **k):
            return {
                "tool": "coverage", "ok": False,
                "summary": {"file": str(target), "total_lines": 3,
                            "covered_lines": 0, "coverage_percent": 0.0,
                            "min_percent": 0, "run_error": True, "timeout": False},
                "failures": [{"reason": "godot launch failed"}],
            }
        monkeypatch.setattr(runner_mod, "_run_gut_for_coverage", abort)
    elif gut_returns == "bad_reparse":
        monkeypatch.setattr(runner_mod, "_instrument", lambda src, sink: ("!!!not gd!!!", {}))

    args = build_parser().parse_args([
        "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(evidence_out),
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    payload = json.loads(buf.getvalue())
    return ev, rc, payload


def test_evidence_lifecycle_success_lands_bundle_and_clears(tmp_path, monkeypatch):
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, ev := tmp_path / "ev", "success"
    )
    assert rc == 0 and payload["ok"] is True
    records = sorted(ev.glob("*.record.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "bundle_written"
    assert record["probe_manifest"]
    assert record["sink_path"]
    bundles = [q for q in ev.glob("*.json") if not q.name.endswith(".record.json")]
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle["recovered"] is False and bundle["sentinels"]["gut_ok"] is True


def test_evidence_lifecycle_abort_lands_failure_bundle(tmp_path, monkeypatch):
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev", "abort_run_error"
    )
    assert rc != 0 and payload["ok"] is False
    assert payload["summary"]["run_error"] is True
    record = json.loads(next(ev.glob("*.record.json")).read_text(encoding="utf-8"))
    assert record["status"] == "run_error"
    bundle = json.loads((ev / f"{record['run_uuid']}.json").read_text(encoding="utf-8"))
    assert bundle["sentinels"]["gut_ok"] is False
    assert bundle["sentinels"]["run_error"] is True
    assert bundle["file_state"] == "bundle_only"


def test_evidence_lifecycle_no_probe_lands_bundle_keeps_sink_lane(tmp_path, monkeypatch):
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev", "no_probe"
    )
    assert rc != 0 and payload["ok"] is False
    record = json.loads(next(ev.glob("*.record.json")).read_text(encoding="utf-8"))
    assert record["status"] == "no_probe"
    bundle = json.loads((ev / f"{record['run_uuid']}.json").read_text(encoding="utf-8"))
    assert bundle["sentinels"]["probes_fired"] == 0


def test_evidence_lifecycle_vacuous_file_lands_bundle_without_gut(tmp_path):
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)
    empty = "extends RefCounted\n"
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "empty.gd"
    target.write_text(empty, encoding="utf-8")
    args = build_parser().parse_args([
        "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(ev),
    ])
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    payload = json.loads(buf.getvalue())
    assert rc == 0 and payload["ok"] is True
    bundle = json.loads(next(ev.glob("*.json")).read_text(encoding="utf-8"))
    assert bundle["sentinels"]["vacuous"] is True
    assert bundle["file_state"] == "vacuous"


def test_evidence_lifecycle_read_error_is_structural(tmp_path):
    """§SPEC-028: a non-UTF-8 target fails structurally (ok=false, run_error
    lane), never crashes — even with evidence_out enabled."""
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "bad.gd"
    target.write_bytes(b"\xff\xfe\x00 not utf-8")
    args = build_parser().parse_args([
        "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(ev),
    ])
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    payload = json.loads(buf.getvalue())
    assert rc != 0 and payload["ok"] is False
    assert payload["summary"]["run_error"] is True
    assert ev.glob("*.record.json")


def test_evidence_reparse_generation_guard_is_structural(tmp_path, monkeypatch):
    """A corrupted instrumented artifact must fail at generation time
    (run_error) instead of landing on disk to produce fake 0% data."""
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev", "bad_reparse"
    )
    assert rc != 0 and payload["ok"] is False
    assert payload["summary"]["run_error"] is True
    assert payload["summary"]["error"].startswith(
        "instrumented source failed to parse:"
    )
    assert payload["failures"][0]["reason"].startswith(
        "instrumentation produced unparseable source"
    )
    assert not list(ev.glob("*.record.json"))
    assert not list(ev.glob("*.json"))


# --------------------------------------------------------------------------
# publicized helpers (§SPEC-029)
# --------------------------------------------------------------------------


def test_max_leaf_line_synthetic_no_meta_tree_is_zero():
    from godot_qa_toolkit.gd_ast_utils import max_leaf_line

    assert max_leaf_line(Tree("a", [])) == 0


def test_bundle_entry_states_pin_inner_class_rows():
    """§SPEC-026: inner-class entries land in entry_states as
    partial_instrumentation (pruned by design), keeping T set coverage
    honest."""
    from godot_qa_toolkit.coverage.evidence import build_bundle

    src = (
        "class Inner:\n"
        "\tfunc m():\n"
        "\t\tpass\n"
        "\n"
        "func f():\n"
        "\treturn 1\n"
    )
    bundle = build_bundle(
        run_uuid="u-inner", file_path="x.gd", gd_source=src, tests_glob=None,
        hits_lines=[6], probes_fired=1, gut_ok=True, run_error=False,
        timeout=False, zero_tests=False, vacuous=False,
    )
    states = {s["fid"]: s for s in bundle["entry_states"]}
    inner_rows = [s for s in bundle["entry_states"]
                  if s["state"] == "partial_instrumentation"]
    assert inner_rows, "inner-class entry must pin a partial_instrumentation row"
    assert all(r["t_equal_i"] is False for r in inner_rows)
    assert states["x.gd::f"]["state"] == "measured"
    inner_t = [r for r in bundle["T"] if "Inner" in r["fid"]]
    assert inner_t and all(r["t_lines"] == 0 for r in inner_t)
    inner_i = [r for r in bundle["I"] if "Inner" in r["fid"]]
    assert inner_i and all(r["i_lines"] == 0 for r in inner_i)
    f_t = next(r for r in bundle["T"] if r["fid"] == "x.gd::f")
    assert f_t["t_lines"] == 1 and f_t["line_ids"] == [6]


def test_max_leaf_line_matches_parse_and_handles_children():
    from gdtoolkit.parser import parser as gd_parser

    from godot_qa_toolkit.gd_ast_utils import max_leaf_line

    tree = gd_parser.parse(GD, gather_metadata=True)
    assert max_leaf_line(tree) == 4  # last leaf token line of the snippet
    assert max_leaf_line(Tree("a", [])) == 0
    assert max_leaf_line(Tree("a", [Tree("b", []), Token("NAME", "x")])) == 0


def test_resolve_user_sink_dir_returns_parent_when_sink_exists(tmp_path, monkeypatch):
    """§SPEC-029 publicized seam consumer: prune/recover sweep the real
    user:// dir once the sink exists there (HOME redirected to a fixture)."""
    from godot_qa_toolkit.coverage.runner import _resolve_user_sink_dir

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text(
        'config/name = "See1546FakeApp"\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    sink_dir = home / ".local" / "share" / "godot" / "app_userdata" / "See1546FakeApp"
    sink_dir.mkdir(parents=True)
    (sink_dir / "qa-coverage-hits-probe.txt").write_text("0\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    assert _resolve_user_sink_dir(str(proj)) == str(sink_dir)


def test_same_process_sequential_runs_get_distinct_sinks(tmp_path, monkeypatch):
    """§SPEC-025 offline anchor for the D-候选-1 empirical gate: two
    sequential run_coverage calls in ONE process must use different sink
    names (the PID sink era made run2 inherit run1's hits)."""
    from godot_qa_toolkit.coverage import runner as runner_mod

    captured = []
    real_instrument = runner_mod._instrument

    def spy(src, sink_name):
        captured.append(sink_name)
        return real_instrument(src, sink_name)

    monkeypatch.setattr(runner_mod, "_instrument", spy)
    ev1, rc1, _ = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev1", "success"
    )
    ev2, rc2, _ = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev2", "success"
    )
    assert rc1 == 0 and rc2 == 0
    assert len(captured) == 2 and captured[0] != captured[1]


def test_prune_sinks_prefix_gate_keeps_old_foreign_files(tmp_path):
    """Prefix filter is load-bearing in BOTH directions: a plain `*` glob
    would delete an expired file that does not carry the sink prefix."""
    from godot_qa_toolkit.coverage.evidence import prune_sinks

    stale_foreign = tmp_path / "unrelated-stale.txt"
    stale_foreign.write_text("x\n", encoding="utf-8")
    stale_time = time.time() - 9 * 86400
    os.utime(stale_foreign, (stale_time, stale_time))
    stale_sink = tmp_path / "qa-coverage-hits-stale.txt"
    stale_sink.write_text("y\n", encoding="utf-8")
    os.utime(stale_sink, (stale_time, stale_time))

    result = prune_sinks(str(tmp_path), retention_days=7)
    assert any(d.endswith("qa-coverage-hits-stale.txt") for d in result["deleted"])
    assert stale_foreign.is_file()


def test_bundle_measured_row_reports_t_ne_i_false(monkeypatch):
    """§SPEC-026: a measured (instrumentable) entry whose planned probes lost
    a line versus the statement census must report t_equal_i=False — never a
    pinned True (E10)."""
    import godot_qa_toolkit.registry.manifest as manifest_mod
    from godot_qa_toolkit.coverage.evidence import build_bundle

    real = manifest_mod.collect_executable_lines

    def drop_last(src):
        rows = real(src)
        return rows[:-1] if len(rows) > 1 else rows

    monkeypatch.setattr(manifest_mod, "collect_executable_lines", drop_last)
    bundle = build_bundle(
        run_uuid="u-mis", file_path="x.gd", gd_source=GD, tests_glob=None,
        hits_lines=[2], probes_fired=2, gut_ok=True, run_error=False,
        timeout=False, zero_tests=False, vacuous=False,
    )
    f_state = next(s for s in bundle["entry_states"] if s["fid"] == "x.gd::f")
    # instrumentable entry with T≠I degrades to partial_instrumentation —
    # never a pinned True (E10)
    assert f_state["state"] == "partial_instrumentation"
    assert f_state["t_equal_i"] is False


# --------------------------------------------------------------------------
# D-性能-1 (§SPEC-030): single-parse reuse guards
# --------------------------------------------------------------------------


def test_build_bundle_uses_injected_registry_and_manifest(monkeypatch):
    """§SPEC-030 (D-性能-1): when the run path injects registry/probe_manifest,
    build_bundle must NOT re-derive them (a second lark parse per run is the
    cost this optimization removes)."""
    from godot_qa_toolkit.coverage import evidence as evidence_mod
    from godot_qa_toolkit.coverage.evidence import build_bundle

    calls = {"registry": 0, "manifest": 0}
    real_registry = evidence_mod.build_file_registry
    real_manifest = evidence_mod.build_probe_manifest_v2

    def counting_registry(src, file_label):
        calls["registry"] += 1
        return real_registry(src, file_label)

    def counting_manifest(src, registry):
        calls["manifest"] += 1
        return real_manifest(src, registry)

    monkeypatch.setattr(evidence_mod, "build_file_registry", counting_registry)
    monkeypatch.setattr(evidence_mod, "build_probe_manifest_v2", counting_manifest)

    registry = real_registry(GD, file_label="x.gd")
    manifest = real_manifest(GD, registry)
    bundle = build_bundle(
        run_uuid="u-inject", file_path="x.gd", gd_source=GD,
        tests_glob=None, hits_lines=[2], probes_fired=1, gut_ok=True,
        run_error=False, timeout=False, zero_tests=False, vacuous=False,
        registry=registry, probe_manifest=manifest,
    )
    assert calls == {"registry": 0, "manifest": 0}
    f_row = next(s for s in bundle["entry_states"] if s["fid"] == "x.gd::f")
    assert f_row["state"] == "measured"


def test_runner_evidence_path_builds_registry_once_per_run(tmp_path, monkeypatch):
    """Three consumption points (run record, abort/success bundles) must share
    ONE registry build per run — a second build is the doubled lark parse
    D-性能-1 removed (mutation ②)."""
    import godot_qa_toolkit.registry.functions as registry_functions
    import godot_qa_toolkit.registry.manifest as registry_manifest
    from godot_qa_toolkit import coverage as coverage_pkg
    from godot_qa_toolkit.coverage import runner as runner_mod
    import io
    from contextlib import redirect_stdout

    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    target = proj / "calc.gd"
    target.write_text(GD, encoding="utf-8")
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)

    real_build = registry_functions.build_file_registry
    real_manifest = registry_manifest.build_probe_manifest_v2
    builds = []
    manifest_builds = []

    def counting_build(src, file_label):
        builds.append(1)
        return real_build(src, file_label=file_label)

    def counting_manifest(src, registry):
        manifest_builds.append(1)
        return real_manifest(src, registry)

    # count across BOTH namespaces: runner's direct call and any build_bundle
    # re-derivation (evidence holds its own module-level binding).
    # B3: the probe-manifest/2 build is counted the same way — the D-性能-1
    # contract is ONE parse per run, which covers BOTH builders.
    monkeypatch.setattr(registry_functions, "build_file_registry", counting_build)
    monkeypatch.setattr(coverage_pkg.evidence, "build_file_registry", counting_build)
    monkeypatch.setattr(registry_manifest, "build_probe_manifest_v2", counting_manifest)
    monkeypatch.setattr(coverage_pkg.evidence, "build_probe_manifest_v2", counting_manifest)
    monkeypatch.setattr(runner_mod, "_run_gut_for_coverage", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "_read_hits", lambda *a, **k: ({2}, 1))

    args = build_parser().parse_args([
        "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(ev),
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    assert len(builds) == 1
    assert len(manifest_builds) == 1


def test_recover_finds_user_sink_via_project_root(tmp_path, monkeypatch):
    """D-性能-1 wiring: recovery resolves the run's user:// sink through
    project_root and rebuilds the probe-manifest/2-shaped attribution (C rows
    keyed by fid with plan lines) — never a degraded row list (mutation ③)."""
    from godot_qa_toolkit.coverage.evidence import (
        build_run_record,
        recover_bundle,
        sink_name_for,
        write_run_record,
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text(
        'config/name = "See1546RecoverApp"\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    user_dir = home / ".local" / "share" / "godot" / "app_userdata" / "See1546RecoverApp"
    user_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    uuid = "u-user-sink"
    record = build_run_record(
        run_uuid=uuid, file_path="x.gd", gd_source=GD, tests_glob=None,
        probe_manifest={"probe_plan": [
            {"probe_id": 0, "line": 2, "fid": "x.gd::f", "owner_kind": "func"},
        ], "per_function": [
            {"fid": "x.gd::f", "t_lines": 1, "i_lines": 1, "t_equal_i": True,
             "t_line_ids": [2]},
        ]},
    )
    evidence = tmp_path / "ev"
    evidence.mkdir()
    write_run_record(evidence, record)
    (user_dir / sink_name_for(uuid)).write_text("0\n", encoding="utf-8")

    result = recover_bundle(evidence, project_root=str(proj))
    assert result["errors"] == []
    assert len(result["recovered"]) == 1
    bundle = json.loads(
        Path(result["recovered"][0]["bundle"]).read_text(encoding="utf-8")
    )
    assert bundle["recovered"] is True
    assert [c["fid"] for c in bundle["C"]] == ["x.gd::f"]
    assert [c["covered"] for c in bundle["C"]] == [1]
    assert bundle["probe_manifest"]["inline"]["probe_plan"][0]["fid"] == "x.gd::f"


# --------------------------------------------------------------------------
# B1 (QA FAIL): §SPEC-029 dual-direction debt gate, in-suite anchors.
# The §SPEC-029 grep gate is machine-proven; these pin the same semantics in
# the suite so re-introducing EITHER private import direction turns red.
# --------------------------------------------------------------------------


def test_spec029_registry_private_runner_import_gate():
    """D1 anchor: registry/ must not import private coverage.runner symbols
    (`_node_line` et al.) — public registry.lines names replaced them."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent.parent
    proc = subprocess.run(
        ["grep", "-rnE", r"from ..coverage.runner import _",
         "src/godot_qa_toolkit/registry/"],
        cwd=str(repo), capture_output=True,
    )
    assert proc.returncode == 1, proc.stdout.decode()


def test_spec029_risk_private_scopemanifest_import_gate():
    """D2 anchor: risk/ must not import private mutation.scopemanifest
    symbols (`_max_leaf_line` et al.) — gd_ast_utils is the neutral home."""
    import subprocess

    repo = Path(__file__).resolve().parent.parent.parent
    proc = subprocess.run(
        ["grep", "-rnE", r"from ..mutation.scopemanifest import _",
         "src/godot_qa_toolkit/risk/"],
        cwd=str(repo), capture_output=True,
    )
    assert proc.returncode == 1, proc.stdout.decode()


# --------------------------------------------------------------------------
# B2 (QA FAIL): run-record atomic write anchor — the record must land via a
# temp file + Path.replace, so a crash mid-write can never leave a
# half-written record that recovery would misread.
# --------------------------------------------------------------------------


def test_write_run_record_replaces_atomically(tmp_path, monkeypatch):
    import godot_qa_toolkit.coverage.evidence as evidence_mod

    replace_calls = []
    real_replace = pathlib.Path.replace

    def spy_replace(self, target):
        replace_calls.append(str(self))
        return real_replace(self, target)

    monkeypatch.setattr(pathlib.Path, "replace", spy_replace)
    record = {"run_uuid": "u-atomic", "status": "started"}
    evidence_mod.write_run_record(tmp_path, record)

    final = tmp_path / "u-atomic.record.json"
    assert final.is_file()
    assert json.loads(final.read_text(encoding="utf-8")) == record
    # the temp file was consumed via Path.replace (atomic rename)
    assert replace_calls == [str(tmp_path / "u-atomic.record.tmp")]
    assert not (tmp_path / "u-atomic.record.tmp").exists()
