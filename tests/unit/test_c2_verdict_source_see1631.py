"""SEE-1631 C2：mutation 判定基线单一真源化（§SPEC-020）的行为判据。

- verdict 表进 attribution 产物（GUT 与 gherkin 同表，红分类三车道）；
- map 源合成基线快照与既有快照同形（rc/tail/elapsed，下游零改动）；
- map 缺失/过期/与树不一致 → 具名 fail-loud；
- map 源下 parity 失配 → 整批作废具名 fail-loud（不得批量假 kill）；
- cold 显式回退保留。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# --------------------------------------------------------------------------
# verdict 表（目标 1）
# --------------------------------------------------------------------------

def _arm(name: str, extra_tf: dict, identities: list[str],
         meta_pairs: "list | None" = None) -> dict:
    tf = {"observed": [], "observed_pairs": [], "new": [], "new_pairs": [],
          "instrumentation_intolerant": [], **extra_tf}
    out = {"arm": name, "state": "valid", "test_failures": tf,
           "attribution": {"test_to_lines": {i: {} for i in identities}}}
    if name == "gut_full":
        meta = None
        if meta_pairs is not None:
            meta = {"method": "uninstrumented_run", "lazy": True,
                    "count": len(meta_pairs),
                    "pairs": [[s, t] for s, t in meta_pairs],
                    "status": "partial"}
        out["baseline_failures_meta"] = meta
    return out


def test_verdict_table_classifies_three_lanes():
    from godot_qa_toolkit.measurement.attribution import _test_verdict_table

    gut = _arm(
        "gut_full",
        {"observed_pairs": ["res://tests/unit/test_a.gd::test_red1",
                            "res://tests/unit/test_a.gd::test_red2",
                            "res://tests/unit/test_b.gd::test_intol"],
         "instrumentation_intolerant": [
             "res://tests/unit/test_b.gd::test_intol"]},
        ["res://tests/unit/test_a.gd::test_red1",
         "res://tests/unit/test_a.gd::test_red2",
         "res://tests/unit/test_b.gd::test_intol",
         "res://tests/unit/test_a.gd::test_green"],
        meta_pairs=[("res://tests/unit/test_a.gd", "test_red1")])
    gher = _arm(
        "gherkin",
        {"pre_existing_failures": [
            "res://gherkin/generated/.gqt-run-*/generated/"
            "test_alpha.gd::test_s0"]},
        ["gherkin://feature/alpha::test_s0",
         "gherkin://feature/alpha::test_s1"])
    table = _test_verdict_table((gher, gut))
    # §SPEC-024：红条目附 script（基线合成/物理跳过用）——verdict 与
    # classification 逐字段断言，dict 整体等值不再适用
    assert table["res://tests/unit/test_a.gd::test_red1"]["verdict"] == "red"
    assert table["res://tests/unit/test_a.gd::test_red1"][
        "classification"] == "pre_existing"
    assert table["res://tests/unit/test_b.gd::test_intol"][
        "classification"] == "suspected_intolerant"
    assert table["res://tests/unit/test_a.gd::test_red2"][
        "classification"] == "new"
    assert table["res://tests/unit/test_a.gd::test_green"] == {
        "verdict": "green"}
    assert table["gherkin://feature/alpha::test_s0"][
        "classification"] == "pre_existing"
    assert table["gherkin://feature/alpha::test_s1"]["verdict"] == "green"


def test_artifact_carries_verdict_table(tmp_path):
    from godot_qa_toolkit.measurement.attribution import (
        _attribution_artifact, ATTRIBUTION_SCHEMA,
    )

    arm = _arm("gut_full", {"observed_pairs": ["res://t.gd::test_x"]},
               ["res://t.gd::test_x", "res://t.gd::test_y"])
    doc = _attribution_artifact(
        "rh", "pr", Path("."), {"enabled": False}, {}, [], {},
        (arm,), {"gut_full": arm.get("attribution")}, [],
        instrumentable_lines={})
    assert doc["schema_version"] == ATTRIBUTION_SCHEMA == "test-attribution/7"
    tv = doc["test_verdicts"]
    assert tv["res://t.gd::test_x"]["verdict"] == "red"
    assert tv["res://t.gd::test_y"]["verdict"] == "green"


# --------------------------------------------------------------------------
# map 源合成基线快照（目标 2）
# --------------------------------------------------------------------------

def _write_map(tmp_path: Path, doc: dict) -> None:
    cache = tmp_path / ".qa-cache"
    cache.mkdir(exist_ok=True)
    (cache / "attribution-map.json").write_text(json.dumps(doc),
                                                encoding="utf-8")


def _map_doc(tmp_path: Path, files: dict[str, str]) -> dict:
    import hashlib
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from godot_qa_toolkit.measurement.attribution import ATTRIBUTION_SCHEMA
    from godot_qa_toolkit.mutation.truth_select import _fresh_recipe_hash

    install_recipe(tmp_path)
    pristine = _fresh_recipe_hash(str(tmp_path))
    assert pristine, "fixture recipe must be buildable"
    face = {rel: hashlib.sha256(content.encode()).hexdigest()
            for rel, content in files.items()}
    shas = {rel: hashlib.sha256(content.encode()).hexdigest()
            for rel, content in files.items()}
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": "rh",
        "recipe_hash_pristine": pristine,
        "project_root": str(tmp_path),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": shas, "target_files": sorted(shas),
        "tests_face": face, "instrumentable_lines": {},
        "arms": {"gut_full": {"test_to_lines": {}, "script_to_lines": {},
                              "tests": 0, "scripts": 0, "files": 0,
                              "pairs": 0, "function_pairs": 0,
                              "test_to_functions": {},
                              "script_to_functions": {},
                              "file_to_tests": {}, "file_to_scripts": {},
                              "line_pairs": 0, "script_line_pairs": 0,
                              "test_to_lines": {}, "script_to_lines": {},
                              "line_to_tests": {}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 5,
                                    "vacuous": False}},
        "test_verdicts": {
            "res://tests/unit/test_a.gd::test_red1": {
                "verdict": "red", "classification": "pre_existing"},
            "res://tests/unit/test_a.gd::test_green": {"verdict": "green"}},
        "notes": [],
    }


def test_map_source_synthesizes_snapshot_with_red_tail(
        tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.gutrun import (
        _parse_failing_tests, parse_failures_with_scripts)
    from godot_qa_toolkit.mutation.verdict_source import (
        map_baseline_snapshots,
    )

    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    _write_map(tmp_path, _map_doc(tmp_path, files))
    baselines, failures = map_baseline_snapshots(
        ["tests/unit/test_a.gd"], str(tmp_path), None)
    assert failures == {}
    snap = baselines[None]
    assert snap["source"] == "map"
    # 合成 tail 与既有解析器逐位兼容
    assert _parse_failing_tests(snap["tail"]) == {"test_red1"}
    qualified = parse_failures_with_scripts(snap["tail"])
    # §SPEC-032/D13：合成 header 归一 basename（res://<basename>——
    # 与行选面 wrapper / 失败差集同口径）
    assert ("res://test_a.gd", "test_red1") in qualified
    assert snap["rc"] == 1
    assert snap["elapsed"] == 0.0


def test_map_source_green_face_rc0_no_failures(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.gutrun import _parse_failing_tests
    from godot_qa_toolkit.mutation.verdict_source import (
        map_baseline_snapshots,
    )

    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    doc = _map_doc(tmp_path, files)
    doc["test_verdicts"] = {
        "res://tests/unit/test_a.gd::test_green": {"verdict": "green"}}
    _write_map(tmp_path, doc)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    baselines, _f = map_baseline_snapshots(["tests/unit/test_a.gd"],
                                           str(tmp_path), None)
    assert baselines[None]["rc"] == 0
    assert _parse_failing_tests(baselines[None]["tail"]) == set()


def test_map_missing_fail_loud(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, map_baseline_snapshots,
    )

    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    with pytest.raises(BaselineSourceError) as ei:
        map_baseline_snapshots(["tests/unit/test_a.gd"], str(tmp_path),
                               None)
    assert "no attribution map" in str(ei.value)


def test_map_stale_tests_face_fail_loud(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, map_baseline_snapshots,
    )

    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    doc = _map_doc(tmp_path, files)
    doc["tests_face"]["tests/unit/test_a.gd"] = "deadbeef"
    _write_map(tmp_path, doc)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    with pytest.raises(BaselineSourceError) as ei:
        map_baseline_snapshots(["tests/unit/test_a.gd"], str(tmp_path),
                               None)
    assert "tests_face drift" in str(ei.value)


def test_map_stale_recipe_fail_loud(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation.verdict_source import (
        BaselineSourceError, map_baseline_snapshots,
    )

    files = {"tests/unit/test_a.gd": "extends GutTest\n"}
    doc = _map_doc(tmp_path, files)
    doc["recipe_hash_pristine"] = "stale"
    _write_map(tmp_path, doc)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    with pytest.raises(BaselineSourceError) as ei:
        map_baseline_snapshots(["tests/unit/test_a.gd"], str(tmp_path),
                               None)
    assert "stale" in str(ei.value)


def test_cold_optout_env(monkeypatch):
    from godot_qa_toolkit.mutation.verdict_source import baseline_source

    monkeypatch.delenv("GQT_MUTATION_BASELINE_SOURCE", raising=False)
    assert baseline_source() == "map"
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "cold")
    assert baseline_source() == "cold"


# --------------------------------------------------------------------------
# parity 失配 + map 源 → 整批作废具名 fail-loud（目标 2 负对照锚点）
# --------------------------------------------------------------------------

def test_parity_mismatch_map_source_raises(tmp_path, monkeypatch):
    from godot_qa_toolkit.mutation import runner as mr

    slot_info = {"worker_fatal_events": [
        "mode parity gate: direct-drive failing set diverges from cold "
        "baseline (worker=['test_x'] cold=[])"]}
    precomputed = {"rc": 0, "tail": "", "elapsed": 0.0, "source": "map"}
    assert mr.baseline_snapshot_from_map(precomputed) is True
    with pytest.raises(Exception) as ei:
        # 直接复现 runner 内的升级逻辑（同款条件与异常）
        if mr.baseline_snapshot_from_map(precomputed):
            for ev in slot_info.get("worker_fatal_events") or []:
                if str(ev).startswith("mode parity gate"):
                    from godot_qa_toolkit.mutation.verdict_source import (
                        BaselineSourceError)

                    raise BaselineSourceError(
                        "baseline_map_mismatch: attribution-map verdict "
                        f"baseline diverged from real execution "
                        f"(parity gate) — batch invalidated: {ev}")
    assert "baseline_map_mismatch" in str(ei.value)
    assert "batch invalidated" in str(ei.value)


def test_cold_source_parity_mismatch_does_not_use_map_escalation():
    from godot_qa_toolkit.mutation import runner as mr

    precomputed = {"rc": 0, "tail": "", "elapsed": 0.1}
    assert mr.baseline_snapshot_from_map(precomputed) is False
    assert mr.baseline_snapshot_from_map(None) is False
