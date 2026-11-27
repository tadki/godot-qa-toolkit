"""SEE-1546 WP2 PR-B hardener: evidence lifecycle (map-derived bundles),
sink-cleanup guards, publicized helpers, and the §SPEC-024/029 debt gates.

SEE-1594 S1：direct coverage 的就地插桩/GUT/sink/`--recover` 执行面已退役；
coverage 答案与 bundle 由**行级映射投影**生成（无第二次 GUT）。本文件验收：
- 成功 → 投影 bundle（gut_ok=True，hits_lines=映射 covered）；
- 失败 → 失败 bundle（gut_ok=False / run_error=True），`measure risk`
  逐文件降级、不整批中止；
- vacuous → 无 GUT 直接 vacuous bundle；
- `--prune-sinks` 仅清理历史 sink 文件（清理错误具名非零）。
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


def _write_raw_map(project_root, instrumentable, covered_lines, probes_fired):
    """Raw test-attribution/6 doc with full control (probes_fired /
    instrumentable surface) for sentinel/lane tests."""
    import hashlib

    import attribution_fixtures as fx
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash

    fx.install_recipe(project_root)
    recipe, error = build_recipe(project_root)
    assert error is None, error
    raw = recipe_hash({k: v for k, v in recipe.items() if k != "recipe_hash"})
    rel = "utils/calc.gd"
    ident = "test_x.gd::test_a"
    arm = {"test_to_files": {ident: [rel]}, "script_to_files": {},
           "tests": 1, "scripts": 1, "files": 1, "pairs": 1,
           "function_pairs": 0, "test_to_functions": {},
           "script_to_functions": {}, "file_to_tests": {rel: [ident]},
           "file_to_scripts": {rel: ["test_x.gd"]},
           "line_pairs": len(covered_lines),
           "script_line_pairs": len(covered_lines),
           "test_to_lines": {ident: {rel: list(covered_lines)}},
           "script_to_lines": {},
           "line_to_tests": {rel: {str(covered_lines[0]): [ident]}}}
    doc = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": raw, "recipe_hash_pristine": raw,
        "project_root": str(project_root), "identity": ident,
        "causal": {"enabled": False},
        "file_sha256": {rel: hashlib.sha256(
            (Path(project_root) / rel).read_bytes()).hexdigest()},
        "target_files": [rel],
        "tests_face": {"tests/test_x.gd": hashlib.sha256(
            (project_root / "tests" / "test_x.gd").read_bytes()).hexdigest()},
        "instrumentable_lines": {rel: list(instrumentable)},
        "arms": {"gut_full": arm},
        "arms_state": {"gut_full": {"state": "valid", "invalid_reason": None,
                                    "bundles": 1, "pairs": 1,
                                    "probes_fired": probes_fired,
                                    "vacuous": False}},
        "notes": [],
        "test_verdicts": {ident: {"verdict": "green",
                                  "classification": "pre_existing"}},
        "feature_sha256": fx._feature_sha_of(project_root),
    }
    out = Path(project_root) / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return str(out)


def _coverage_via_cli(tmp_path, monkeypatch, gd_text, evidence_out,
                      mode="success"):
    """Drive cmd_measure_coverage in-process against the line-mapping source.

    mode: success / no_map / no_probes / legacy_residue — no Godot, no GUT
    (SEE-1594 S1: coverage answers come from the line map)."""
    import io
    from contextlib import redirect_stdout

    import attribution_fixtures as fx
    from godot_qa_toolkit.coverage import runner as runner_mod

    proj = tmp_path / "proj"
    (proj / "utils").mkdir(parents=True, exist_ok=True)
    target = proj / "utils" / "calc.gd"
    target.write_text(gd_text, encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.gd").write_text("extends Node\n", encoding="utf-8")
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)

    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.detect_binary_kind",
        lambda *a, **k: "linux")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.identity.identity_pack",
        lambda *a, **k: {"kind": "linux"})

    if mode == "success":
        fx.write_map(proj, {"utils/calc.gd": ["test_x.gd::test_a"]})
    elif mode == "no_probes":
        _write_raw_map(proj, [2, 3], covered_lines=[2],
                       probes_fired=0)
    elif mode == "legacy_residue":
        target.write_text(
            gd_text + "\nstatic func __qa_cov_probe_0():\n\tpass\n",
            encoding="utf-8")

    args = build_parser().parse_args([
        "measure", "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(ev),
    ])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    payload = json.loads(buf.getvalue())
    return ev, rc, payload

def test_evidence_lifecycle_success_lands_bundle_and_clears(tmp_path, monkeypatch):
    """成功：投影 bundle 落盘（gut_ok=True，hits_lines=映射 covered；
    probes_fired 来自映射 arms_state）——无第二次 GUT/插桩。"""
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, ev := tmp_path / "ev", "success"
    )
    assert rc == 0 and payload["ok"] is True
    assert payload["summary"]["coverage_source"] == "line_mapping_projection"
    bundles = [q for q in ev.glob("*.json") if not q.name.endswith(".record.json")]
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle["recovered"] is False and bundle["sentinels"]["gut_ok"] is True
    assert bundle["sentinels"]["probes_fired"] == 640  # 来自夹具 arms_state
    assert [c["covered"] for c in bundle["C"]] == [1]

def test_evidence_lifecycle_abort_lands_failure_bundle(tmp_path, monkeypatch):
    """失败（旧版残留插桩 → fail-loud）：失败 bundle 落盘（gut_ok=False /
    run_error=True），`measure risk` 逐文件降级、不整批中止（§SPEC-025/027）。"""
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev", "legacy_residue"
    )
    assert rc != 0 and payload["ok"] is False
    assert payload["summary"]["run_error"] is True
    bundles = [q for q in ev.glob("*.json") if not q.name.endswith(".record.json")]
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle["sentinels"]["gut_ok"] is False
    assert bundle["sentinels"]["run_error"] is True
    assert bundle["file_state"] == "bundle_only"

def test_evidence_lifecycle_probes_zero_sentinel_lands_failure_bundle(
        tmp_path, monkeypatch):
    """执行证据哨兵：映射 probes_fired=0 且存在有效行事实 → 不得产出可信
    分数；失败 bundle 落盘、run_error 具名（§SPEC-025/027）。"""
    ev, rc, payload = _coverage_via_cli(
        tmp_path, monkeypatch, GD, tmp_path / "ev", "no_probes"
    )
    assert rc != 0 and payload["ok"] is False
    assert payload["summary"]["run_error"] is True
    assert payload["summary"]["probes_fired"] == 0
    assert "probes_fired=0" in payload["summary"]["error"]
    bundles = [q for q in ev.glob("*.json") if not q.name.endswith(".record.json")]
    assert len(bundles) == 1
    bundle = json.loads(bundles[0].read_text(encoding="utf-8"))
    assert bundle["sentinels"]["gut_ok"] is False
    assert bundle["sentinels"]["run_error"] is True

def test_evidence_lifecycle_vacuous_file_lands_bundle_without_gut(tmp_path, monkeypatch):
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "empty.gd"
    target.write_text("extends RefCounted\n", encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_x.gd").write_text("extends Node\n", encoding="utf-8")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.inject.detect_binary_kind",
        lambda *a, **k: "linux")
    monkeypatch.setattr(
        "godot_qa_toolkit.mutation.identity.identity_pack",
        lambda *a, **k: {"kind": "linux"})
    args = build_parser().parse_args(["measure", "coverage", str(target), "--project-root", str(proj),
        "--min-percent", "0", "--evidence-out", str(ev),
    ])
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    payload = json.loads(buf.getvalue())
    assert rc == 0 and payload["ok"] is True
    assert payload["summary"]["coverage_source"] == "line_mapping_projection"
    bundle = json.loads(next(ev.glob("*.json")).read_text(encoding="utf-8"))
    assert bundle["sentinels"]["vacuous"] is True
    assert bundle["file_state"] == "vacuous"

def test_evidence_lifecycle_read_error_is_structural(tmp_path):
    """§SPEC-028: a non-UTF-8 target fails structurally (ok=false, run_error
    lane) and lands a failure bundle — never crashes."""
    ev = tmp_path / "ev"
    ev.mkdir(exist_ok=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "bad.gd"
    target.write_bytes(b"\xff\xfe\x00 not utf-8")
    args = build_parser().parse_args(["measure", "coverage", str(target), "--project-root", str(proj),
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
    bundles = [q for q in ev.glob("*.json") if not q.name.endswith(".record.json")]
    assert len(bundles) == 1  # 失败 bundle 落盘

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
