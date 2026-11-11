"""SEE-1546 WP2 PR-B: sink lifecycle v2 and coverage-evidence/2 contract
(§SPEC-025/026)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from godot_qa_toolkit.coverage.evidence import (
    EVIDENCE_VERSION,
    build_bundle,
    build_run_record,
    make_run_uuid,
    prune_sinks,
    recover_bundle,
    sink_name_for,
    write_run_record,
)
from godot_qa_toolkit.coverage.runner import run_coverage

GD = "func f(x):\n\tif x:\n\t\treturn 1\n\treturn 2\n"


def test_run_uuid_and_sink_names_are_unique_per_call():
    """§SPEC-025: two runs in the SAME process get different UUIDs and
    different sink names (D-候选-1: the PID name was identical and made the
    second run inherit the first run's hits)."""
    first = make_run_uuid()
    second = make_run_uuid()
    assert first != second
    assert sink_name_for(first) != sink_name_for(second)
    assert sink_name_for(first).startswith("qa-coverage-hits-")


def test_bundle_shape_top_level_keys():
    bundle = build_bundle(
        run_uuid="u-1", file_path="x.gd", gd_source=GD,
        tests_glob="res://tests/unit/", hits_lines=[2, 3], probes_fired=2,
        gut_ok=True, run_error=False, timeout=False, zero_tests=False,
        vacuous=False,
    )
    for key in ("evidence_version", "run_uuid", "recovered", "target",
                "recipe", "probe_manifest", "T", "I", "C", "census_gaps",
                "sentinels", "entry_states", "file_state"):
        assert key in bundle
    assert bundle["evidence_version"] == EVIDENCE_VERSION


def test_bundle_closure_invariants():
    """§SPEC-026: C ⊆ I line sets, ΣI = probes_total, entry_states cover the
    instrumentable T set."""
    bundle = build_bundle(
        run_uuid="u-2", file_path="x.gd", gd_source=GD,
        tests_glob="res://tests/unit/", hits_lines=[2], probes_fired=3,
        gut_ok=True, run_error=False, timeout=False, zero_tests=False,
        vacuous=False,
    )
    i_total = sum(r["i_lines"] for r in bundle["I"])
    assert i_total == bundle["sentinels"]["probes_fired"]
    plan_fids = {p["fid"] for p in
                 bundle["probe_manifest"]["inline"]["probe_plan"]}
    for c_row, i_row in zip(bundle["C"], bundle["I"]):
        assert c_row["fid"] == i_row["fid"]
        assert set(c_row["covered_line_ids"]) <= set(
            next(t["line_ids"] for t in bundle["T"] if t["fid"] == c_row["fid"])
        )
    states = {s["fid"] for s in bundle["entry_states"]}
    assert plan_fids <= states


def test_bundle_scores_never_appear():
    """§SPEC-026/§SPEC-011: the bundle is pure evidence — no score fields,
    no thresholds, no CI semantics."""
    bundle = build_bundle(
        run_uuid="u-3", file_path="x.gd", gd_source=GD, tests_glob=None,
        hits_lines=[1, 2], probes_fired=2, gut_ok=True, run_error=False,
        timeout=False, zero_tests=False, vacuous=False,
    )
    assert "score" not in json.dumps(bundle)
    assert bundle["recipe"]["arm"] == "gut_full"


def test_vacuous_bundle_lands_and_never_scores(tmp_path: Path):
    """§SPEC-026: vacuous file → bundle lands with vacuous sentinel and no
    score-worthy coverage numbers."""
    empty = "extends RefCounted\n"
    bundle = build_bundle(
        run_uuid="u-4", file_path="empty.gd", gd_source=empty, tests_glob=None,
        hits_lines=[], probes_fired=0, gut_ok=True, run_error=False,
        timeout=False, zero_tests=False, vacuous=True,
    )
    assert bundle["sentinels"]["vacuous"] is True
    assert bundle["file_state"] == "vacuous"
    assert all(r["t_lines"] == 0 for r in bundle["T"])


def test_run_error_and_timeout_bundles_land_without_scores():
    for sentinel in ({"run_error": True, "timeout": False},
                     {"run_error": False, "timeout": True}):
        bundle = build_bundle(
            run_uuid="u-5", file_path="x.gd", gd_source=GD, tests_glob=None,
            hits_lines=[], probes_fired=0, gut_ok=False, **sentinel,
            zero_tests=False, vacuous=False,
        )
        assert bundle["file_state"] == "bundle_only"
        assert bundle["sentinels"]["gut_ok"] is False
        assert "score" not in json.dumps(bundle)


def test_recover_matches_online_path_and_marks_recovered(tmp_path: Path):
    """§SPEC-025: a started record + sink on disk → --recover produces the
    same bundle the online path would (recovered=true), no GUT re-run."""
    evidence = tmp_path / "ev"
    evidence.mkdir()
    run_uuid = make_run_uuid()
    record = build_run_record(
        run_uuid=run_uuid, file_path="x.gd", gd_source=GD,
        tests_glob=None, probe_manifest={},
    )
    write_run_record(evidence, record)
    # recovery reads the sink from the evidence dir (recorded sink_path or
    # the sibling file) — simulate a completed-but-unbundled run
    (evidence / sink_name_for(run_uuid)).write_text("0\n1\n", encoding="utf-8")
    record = json.loads(
        (evidence / f"{run_uuid}.record.json").read_text(encoding="utf-8")
    )
    record["probe_manifest"] = {"probe_plan": [
        {"probe_id": 0, "line": 1, "fid": "x.gd::f", "owner_kind": "func"},
        {"probe_id": 1, "line": 2, "fid": "x.gd::f", "owner_kind": "func"},
    ], "per_function": [
        {"fid": "x.gd::f", "t_lines": 2, "i_lines": 2, "t_equal_i": True,
         "t_line_ids": [1, 2]},
    ]}
    write_run_record(evidence, record)

    result = recover_bundle(evidence)
    assert not result["errors"]
    assert len(result["recovered"]) == 1
    bundle = json.loads(
        Path(result["recovered"][0]["bundle"]).read_text(encoding="utf-8")
    )
    assert bundle["recovered"] is True
    assert [c["covered"] for c in bundle["C"]] == [2]
    updated = json.loads(
        (evidence / f"{run_uuid}.record.json").read_text(encoding="utf-8")
    )
    assert updated["status"] == "bundle_written"


def test_prune_sinks_only_deletes_prefixed_expired(tmp_path: Path):
    """§SPEC-025/W1: --prune-sinks deletes only `qa-coverage-hits-*` files
    past the retention period; fresh sinks and foreign files survive."""
    proj = tmp_path / "proj"
    proj.mkdir()
    old_sink = proj / "qa-coverage-hits-old.txt"
    old_sink.write_text("1\n", encoding="utf-8")
    stale_time = time.time() - 8 * 86400
    os.utime(old_sink, (stale_time, stale_time))
    fresh_sink = proj / "qa-coverage-hits-fresh.txt"
    fresh_sink.write_text("2\n", encoding="utf-8")
    foreign = proj / "unrelated.txt"
    foreign.write_text("3\n", encoding="utf-8")

    result = prune_sinks(str(proj), retention_days=7)
    assert any(d.endswith(old_sink.name) for d in result["deleted"])
    assert fresh_sink.is_file() and foreign.is_file()


def test_stdout_contract_frozen_against_011b87d(tmp_path: Path, monkeypatch):
    """§SPEC-026 stdout freeze: without --evidence-out the summary keys are
    exactly the 011b87d contract (no evidence keys leak into stdout)."""
    import io
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser

    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    monkeypatch.setattr(
        "godot_qa_toolkit.coverage.runner.subprocess.run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    argv = ["coverage", str(target), "--project-root", str(proj),
            "--min-percent", "0"]
    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        parser.parse_args(argv).func(args)
    payload = json.loads(buf.getvalue())
    # the 011b87d stdout contract keys — evidence fields must not leak
    assert set(payload["summary"]) >= {
        "file", "total_lines", "covered_lines", "coverage_percent",
    }
    assert not any("evidence" in k or "run_uuid" in k
                   for k in payload["summary"])



def test_clear_sink_removes_real_sink_path_file(tmp_path: Path):
    """D-WP2B-1: clear_sink with a real sink_path must delete that file
    (and the local fallback) without crashing — RED before the Path.unlink
    fix (os.remove raised NameError; module never imported os)."""
    from godot_qa_toolkit.coverage.evidence import clear_sink

    evidence = tmp_path / "ev"
    evidence.mkdir()
    sink_path = evidence / sink_name_for("u-clear")
    sink_path.write_text("0\n", encoding="utf-8")
    local = tmp_path / "proj" / sink_name_for("u-clear")
    local.parent.mkdir(parents=True)
    local.write_text("0\n", encoding="utf-8")

    clear_sink(str(sink_path), str(tmp_path / "proj"), sink_name_for("u-clear"))

    assert not sink_path.exists()
    assert not local.exists()


def test_clear_sink_missing_paths_stays_silent(tmp_path: Path):
    """sink_path=None and missing local fallback → silent no-op (contract:
    clear is only called after the bundle landed; missing paths are fine)."""
    from godot_qa_toolkit.coverage.evidence import clear_sink

    clear_sink(None, str(tmp_path), "qa-coverage-hits-never.txt")
    clear_sink(str(tmp_path / "missing" / "sink.txt"), str(tmp_path),
               "qa-coverage-hits-never.txt")


def test_clear_sink_removes_user_sink_location(tmp_path: Path, monkeypatch):
    """§SPEC-041 (D1 RED): probes write the sink under user:// — clearing
    must remove that actual write location, not only the project-root
    fallback. Before the fix every Linux-channel run leaked its sink
    (user:// accumulated thousands of stale qa-coverage-hits files)."""
    import godot_qa_toolkit.coverage.runner as runner_mod
    from godot_qa_toolkit.coverage.evidence import clear_sink, sink_name_for

    name = sink_name_for("u-user")
    fake_user = tmp_path / "app_userdata" / "MyProj"
    fake_user.mkdir(parents=True)
    user_sink = fake_user / name
    user_sink.write_text("1\n2\n", encoding="utf-8")
    monkeypatch.setattr(
        runner_mod, "_resolve_user_sink",
        lambda project_root, sink_name: str(fake_user / sink_name))

    clear_sink(None, str(tmp_path / "proj"), name)

    assert not user_sink.exists()


def test_clear_sink_fails_loud_when_user_unlink_fails(
        tmp_path: Path, monkeypatch):
    """§SPEC-041: cleanup failure must be fail-loud, never silent — a sink
    left behind poisons the next run's no-probe sentinel and accumulates
    residue that --prune-sinks (7-day retention) will not touch."""
    import godot_qa_toolkit.coverage.runner as runner_mod
    from godot_qa_toolkit.coverage.evidence import clear_sink, sink_name_for

    name = sink_name_for("u-stuck")
    fake_user = tmp_path / "app_userdata" / "MyProj"
    fake_user.mkdir(parents=True)
    user_sink = fake_user / name
    user_sink.write_text("1\n", encoding="utf-8")
    monkeypatch.setattr(
        runner_mod, "_resolve_user_sink",
        lambda project_root, sink_name: str(fake_user / sink_name))

    def _boom(self):
        raise PermissionError(f"stubbed unlink failure: {self}")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(RuntimeError, match="clear_sink"):
        clear_sink(None, str(tmp_path / "proj"), name)
    assert user_sink.exists()
