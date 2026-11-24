"""SEE-1587 QA 修复轮（D1–D3）回归判据。

Revy QA FAIL 的三项缺陷与逐条对抗判据（正反两向，旧路径注回即红）：

- D1（HIGH）：benchmark 精确判据命令在「映射未覆盖某 --targets 面」的
  干净副本上必须自动补面或给出可判读错误，不得 NameError；
- D2（MEDIUM）：E2E 判据不得假绿——测量前失效既有映射、测量后必须证明
  新映射（内容哈希变化），坏 recipe / 未写新映射 / 空测量（pairs==0）
  任一发生即红；
- D3（MEDIUM）：零测试测量（探针未点火且非 vacuous）不得被 arms_valid
  消费为有效映射；合法空面（测过但无边）/ vacuous 保持可用；不得输出
  假绿 `ok:true, mutants:0`。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import truth_select

REPO = Path(__file__).resolve().parents[2]


def _load_bench():
    path = REPO / "tests" / "integration" / "run_see1587_line_benchmark.py"
    spec = importlib.util.spec_from_file_location("bench_see1587_qa", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_e2e():
    path = REPO / "tests" / "integration" / "run_see1587_e2e.py"
    spec = importlib.util.spec_from_file_location("e2e_see1587_qa", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid_arms(pairs: int = 8, probes: int = 640) -> dict:
    return {"gut_full": {"state": "invalid", "invalid_reason": "run_error",
                         "bundles": 4, "pairs": pairs,
                         "probes_fired": probes, "vacuous": False}}


def _zero_test_arms() -> dict:
    return {"gut_full": {"state": "valid", "invalid_reason": None,
                         "bundles": 1, "pairs": 0,
                         "probes_fired": 0, "vacuous": False}}


def _map_doc(file_sha256: dict, arms_state: dict) -> dict:
    return {"schema_version": "test-attribution/7",
            "file_sha256": dict(file_sha256),
            "arms": {"gut_full": {}}, "arms_state": arms_state}


# ---------------------------------------------------------------- D1：补面


def _bench_fixture(tmp_path: Path):
    bench = _load_bench()
    kol = tmp_path / "kol"
    (kol / ".qa-cache").mkdir(parents=True)
    work = tmp_path / "work"
    work.mkdir()
    (work / "bench-recipe.json").write_text("{}", encoding="utf-8")
    return bench, kol, work, kol / ".qa-cache" / "attribution-map.json"


def test_d1_ensure_map_faces_prepares_uncovered_face_without_nameerror(
        tmp_path, monkeypatch):
    """映射只覆盖 utils、目标面含 systems：必须补面成功，不得 NameError。"""
    bench, kol, work, map_path = _bench_fixture(tmp_path)
    map_path.write_text(json.dumps(_map_doc(
        {"utils/a.gd": "x"}, _valid_arms())), encoding="utf-8")
    calls = []

    def fake_run(argv, env, cwd=None):
        calls.append(argv)
        assert "measure" in argv and "run" in argv
        map_path.write_text(json.dumps(_map_doc(
            {"utils/a.gd": "x", "systems/b.gd": "y"},
            _valid_arms())), encoding="utf-8")
        return {"argv": argv, "rc": 1, "wall_s": 0.01}

    monkeypatch.setattr(bench, "_run", fake_run)
    bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path, work,
                            False)  # 不得抛异常（尤其不是 NameError）
    assert calls, "measure run must be invoked to fill the uncovered face"


def test_d1_ensure_map_faces_fails_readable_when_face_still_missing(
        tmp_path, monkeypatch):
    """补面后仍缺 systems：显式可判读错误（SystemExit 消息含面名），非 NameError。"""
    bench, kol, work, map_path = _bench_fixture(tmp_path)

    def fake_run(argv, env, cwd=None):
        map_path.write_text(json.dumps(_map_doc(
            {"utils/a.gd": "x"}, _valid_arms())), encoding="utf-8")
        return {"argv": argv, "rc": 1, "wall_s": 0.01}

    monkeypatch.setattr(bench, "_run", fake_run)
    with pytest.raises(SystemExit) as ei:
        bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path,
                                work, False)
    msg = str(ei.value)
    assert "still lacks faces ['systems']" in msg
    assert "refusing to benchmark" in msg


def test_d1_ensure_map_faces_rejects_zero_test_measurement(
        tmp_path, monkeypatch):
    """补面写出的映射若为零测试空测量（D3 口径），benchmark 也须 fail-loud。"""
    bench, kol, work, map_path = _bench_fixture(tmp_path)

    def fake_run(argv, env, cwd=None):
        map_path.write_text(json.dumps(_map_doc(
            {"utils/a.gd": "x", "systems/b.gd": "y"},
            _zero_test_arms())), encoding="utf-8")
        return {"argv": argv, "rc": 0, "wall_s": 0.01}

    monkeypatch.setattr(bench, "_run", fake_run)
    with pytest.raises(SystemExit) as ei:
        bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path,
                                work, False)
    assert "execution-integrity" in str(ei.value)


def test_d1_ensure_map_faces_no_measure_when_all_faces_covered(
        tmp_path, monkeypatch):
    bench, kol, work, map_path = _bench_fixture(tmp_path)
    map_path.write_text(json.dumps(_map_doc(
        {"utils/a.gd": "x", "systems/b.gd": "y"}, _valid_arms())),
        encoding="utf-8")

    def boom(*a, **k):  # pragma: no cover - 调用即失败
        raise AssertionError("measure must not run when faces are covered")

    monkeypatch.setattr(bench, "_run", boom)
    bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path, work,
                            False)


# ---------------------------------------------------------------- D2：不假绿


def test_d2_invalidate_existing_map_moves_stale_and_returns_hash(tmp_path):
    e2e = _load_e2e()
    map_path = tmp_path / "attribution-map.json"
    payload = b'{"stale": true}'
    map_path.write_bytes(payload)
    pre = e2e._invalidate_existing_map(map_path)
    assert pre == hashlib.sha256(payload).hexdigest()
    assert not map_path.exists(), "stale map must not survive into measure"
    moved = list(tmp_path.glob("attribution-map.json.stale-*"))
    assert moved and moved[0].read_bytes() == payload  # 取证留存


def test_d2_invalidate_absent_map_returns_none(tmp_path):
    e2e = _load_e2e()
    assert e2e._invalidate_existing_map(tmp_path / "none.json") is None


def test_d2_renewal_green_on_new_content(tmp_path):
    e2e = _load_e2e()
    map_path = tmp_path / "m.json"
    map_path.write_text('{"new": 1}', encoding="utf-8")
    [c] = e2e._map_renewal_checks(map_path, None, 1)
    assert c["ok"] is True
    [c] = e2e._map_renewal_checks(map_path,
                                  "0" * 64, 0)
    assert c["ok"] is True


def test_d2_renewal_red_when_stale_content_survives(tmp_path):
    """坏 recipe 反例：measure 未写新映射、旧内容仍在 → 判据必须红。"""
    e2e = _load_e2e()
    map_path = tmp_path / "m.json"
    map_path.write_text('{"same": 1}', encoding="utf-8")
    pre = hashlib.sha256(map_path.read_bytes()).hexdigest()
    [c] = e2e._map_renewal_checks(map_path, pre, 1)
    assert c["ok"] is False and c["detail"]["measure_rc"] == 1


def test_d2_renewal_red_when_no_map_written(tmp_path):
    e2e = _load_e2e()
    [c] = e2e._map_renewal_checks(tmp_path / "absent.json", "a" * 64, 1)
    assert c["ok"] is False


def test_d2_e2e_main_is_red_on_bad_recipe(tmp_path, monkeypatch, capsys):
    """整链反例：坏 recipe（measure rc=1 且不写新映射）→ E2E main 必须红。"""
    e2e = _load_e2e()
    kol = tmp_path / "kol"
    (kol / "addons" / "gut").mkdir(parents=True)
    (kol / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (kol / ".gutconfig.json").write_text("{}", encoding="utf-8")
    (kol / "utils").mkdir()
    (kol / "utils" / "t.gd").write_text("func f():\n\treturn 1\n",
                                        encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "recipe.json").write_text("{}", encoding="utf-8")
    # 既有陈旧映射：修复前它会让坏 recipe 仍绿
    (kol / ".qa-cache").mkdir(parents=True)
    (kol / ".qa-cache" / "attribution-map.json").write_text(
        json.dumps(_map_doc({"utils/t.gd": "x"}, _valid_arms())),
        encoding="utf-8")
    godot = tmp_path / "fake-godot"
    godot.write_text("#!/bin/sh\necho 4.6.2.fake\n")
    godot.chmod(godot.stat().st_mode | stat.S_IEXEC)

    def bad_measure(argv, env, cwd=None):
        return {"argv": argv, "rc": 1, "wall_s": 0.01,
                "stdout_json": None, "stderr_tail": "bad recipe",
                "stdout_raw_tail": ""}

    monkeypatch.setattr(e2e, "_run", bad_measure)
    out = tmp_path / "e2e.json"
    rc = e2e.main.__wrapped__ if hasattr(e2e.main, "__wrapped__") else None
    # main() 用 argparse：直接调用并以 sys.argv 注入
    import sys
    monkeypatch.setattr(sys, "argv", [
        "run_see1587_e2e.py", "--kol-root", str(kol),
        "--target", "utils/t.gd", "--budget", "1",
        "--recipe", str(work / "recipe.json"),
        "--work-dir", str(work), "--out", str(out),
        "--godot-bin", str(godot)])
    assert e2e.main() == 1
    evidence = json.loads(out.read_text(encoding="utf-8"))
    failed = [c["check"] for c in evidence["checks"] if not c["ok"]]
    assert "measure produced a new attribution map (content hash)" in failed


# ---------------------------------------------------------------- D3：零测试


def test_d3_state_valid_cannot_bypass_empty_measurement():
    """旧逻辑首分支 state=="valid" 直接放行——D3 注回即红。"""
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                      "probes_fired": 0, "vacuous": False}}) is False
    # 旧产物（无 probes 字段）同样不得凭 state=valid 放行
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bundles": 1, "pairs": 0}}) is False


def test_d3_zero_bundles_is_invalid():
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bridges": 0}}) is False
    assert truth_select.arms_valid({}) is False


def test_d3_legit_empty_and_vacuous_faces_stay_usable():
    # 合法空面：测试确已执行（探针点火）但无归属边
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bundles": 2, "pairs": 0,
                      "probes_fired": 96, "vacuous": False}}) is True
    # vacuous（零可执行行）
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                      "probes_fired": 0, "vacuous": True}}) is True


def test_d3_kol_run_error_boundary_stays_usable():
    assert truth_select.arms_valid(_valid_arms(pairs=1856,
                                               probes=3_200_000)) is True
    # 旧产物有边（无 probes 字段）保持可用（升级兼容方向：仅零边旧产物
    # fail-loud 促重建）
    assert truth_select.arms_valid(
        {"gut_full": {"state": "valid", "bundles": 1, "pairs": 5}}) is True


def _write_map_file(proj: Path, doc: dict) -> Path:
    out = proj / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return out


def _resolve_project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    target = proj / "a.gd"
    target.write_text("func f():\n\treturn 1\n", encoding="utf-8")
    return proj, target


def test_d3_zero_test_map_fails_resolve_selection_loud(tmp_path, monkeypatch):
    """零测试映射不得产出假绿 ok:true/mutants:0——选面阶段 fail-loud。"""
    proj, target = _resolve_project(tmp_path)
    path = _write_map_file(proj, _map_doc(
        {"a.gd": hashlib.sha256(target.read_bytes()).hexdigest()},
        _zero_test_arms()))
    monkeypatch.setenv(truth_select.ENV_MAP, str(path))
    monkeypatch.setenv(truth_select.ENV_MODE, "off")
    with pytest.raises(truth_select.MutationSelectionError) as ei:
        truth_select.resolve_selection(str(proj), str(target))
    assert "measurement_invalid" in str(ei.value)


def test_d3_legit_empty_map_keeps_zero_ref_short_circuit(tmp_path,
                                                        monkeypatch):
    """反方向：合法空面映射仍可用（零引用短路是合法终态，不被误杀）。"""
    from attribution_fixtures import install_recipe
    proj, target = _resolve_project(tmp_path)
    install_recipe(proj)
    from godot_qa_toolkit.measurement.recipe import build_recipe, recipe_hash
    recipe, err = build_recipe(proj)
    assert err is None, err
    raw = recipe_hash({k: v for k, v in recipe.items()
                       if k != "recipe_hash"})
    legit = {"gut_full": {"state": "valid", "bundles": 1, "pairs": 0,
                          "probes_fired": 12, "vacuous": False}}
    doc = _map_doc({"a.gd": hashlib.sha256(
        target.read_bytes()).hexdigest()}, legit)
    doc["recipe_hash_pristine"] = raw
    path = _write_map_file(proj, doc)
    monkeypatch.setenv(truth_select.ENV_MAP, str(path))
    monkeypatch.setenv(truth_select.ENV_MODE, "off")
    sel = truth_select.resolve_selection(str(proj), str(target))
    assert sel["source"] == "runtime_mapping" and sel["zero_ref"] is True


def test_d3_arms_state_records_execution_integrity_evidence():
    """落盘证据字段：probes_fired 求和 + vacuous 全体判定（消费侧判据来源）。"""
    from godot_qa_toolkit.measurement.attribution import _arms_state

    arms = ({"arm": "gut_full", "state": "invalid",
             "invalid_reason": "run_error",
             "bundles": 2,
             "attribution": {"test_to_files": {"t.gd::t1": ["a.gd"]}},
             "bundles_data": {
                 "a.gd": {"sentinels": {"probes_fired": 120,
                                        "vacuous": False}},
                 "b.gd": {"sentinels": {"probes_fired": 30,
                                        "vacuous": False}}}},)
    st = _arms_state(arms)["gut_full"]
    assert st["probes_fired"] == 150
    assert st["vacuous"] is False
    assert st["pairs"] == 1
    vac = _arms_state(({"arm": "gut_full", "state": "valid", "bundles": 1,
                        "attribution": {},
                        "bundles_data": {"a.gd": {"sentinels": {
                            "probes_fired": 0, "vacuous": True}}}},))[
        "gut_full"]
    assert vac["vacuous"] is True


# ---------------------------------------------------------------- D4：判据门


def _plan_one(face: str = "utils") -> dict:
    return {face: {"target": f"{face}/x.gd", "sites": 10,
                   "census_files": 1, "census_total_sites": 10,
                   "lines_spec": "1", "fixture": {}, "aa": 1, "rounds": 1}}


def _write_run(bench, work: Path, face: str, arm: str, rnd,
               *, verdicts: "dict[str, str] | None" = None,
               wall: float = 100.0, selection_error: bool = False) -> None:
    verdicts = verdicts or {}
    rec = {"face": face, "arm": arm, "round": rnd, "status": "done",
           "rc": 1, "wall_s": wall, "argv": [],
           "metrics": {
               "ok": False, "mutants": len(verdicts), "killed": 0,
               "survived": 0, "suspect": 0, "timeout": 0,
               "run_errors": 0, "invalid": 0, "jobs": 1, "scoped": True,
               "selection_mode": "line",
               "test_selection_source": "runtime_mapping",
               "tests_count": 3, "groups": 1,
               "coverage_prefilter": {"source": "projection_store",
                                      "uncovered": 0, "covered_executed": 1},
               "lines_selection": None, "baseline_reused": False,
               "stage_timings": {"baseline": 10.0},
               "rss_peak_mb": 150.0, "total_sites": 10, "executed": 1,
               "verdicts": dict(verdicts),
               "mutant_seconds": [1.0] * len(verdicts),
               "exec_scheme": None, "inject_scheme": "disk-farm/1",
               "slot_mode": None,
               "selection_error": selection_error,
               "error": "selection_error" if selection_error else None},
           "stderr_tail": ""}
    bench._atomic_write(bench._run_path(work, face, arm, rnd), rec)


def _fill_face(bench, work: Path, face: str, *, verdicts, walls,
               selection_error_arm: str | None = None) -> None:
    _write_run(bench, work, face, "S2", "aa1", verdicts=verdicts,
               wall=walls["S2"])
    for arm in bench.ARMS:
        _write_run(bench, work, face, arm, 1, verdicts=verdicts,
                   wall=walls[arm],
                   selection_error=(arm == selection_error_arm))


def test_d4_gate_red_when_all_rounds_failed_without_verdicts(tmp_path):
    """D4 剧本：完整面零 pair 映射 + 全轮失败（无 verdict 对账）→ 必须红。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    _fill_face(bench, work, "utils", verdicts={},
               walls={"S0": 100.0, "S1": 95.0, "S2": 99.0, "S3": 110.0})
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    assert rep["gate"]["passed"] is False
    assert rep["gate"]["compared_gate_failures"], rep["gate"]
    faces = {g["arm"] for g in rep["gate"]["compared_gate_failures"]}
    # S3 是参照 oracle，不参与 compared 门；被测臂 S0/S1/S2 全须报红
    assert faces == {"S0", "S1", "S2"}


def test_d4_gate_green_when_verdicts_reconciled(tmp_path):
    """反方向：健康轮次（有 verdict 对账、S2≤S0、无 selection_error）仍绿。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    v = {"utils/x.gd#f#1": "killed"}
    _fill_face(bench, work, "utils", verdicts=v,
               walls={"S0": 100.0, "S1": 90.0, "S2": 95.0, "S3": 120.0})
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    assert rep["gate"]["passed"] is True, rep["gate"]
    assert rep["gate"]["compared_gate_failures"] == []


def test_d4_gate_red_on_selection_error_runs(tmp_path):
    """选择阶段拒跑（selection_error）即便有 verdict 也必须红。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    v = {"utils/x.gd#f#1": "killed"}
    _fill_face(bench, work, "utils", verdicts=v,
               walls={"S0": 100.0, "S1": 90.0, "S2": 95.0, "S3": 120.0},
               selection_error_arm="S1")
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    assert rep["gate"]["passed"] is False
    assert rep["gate"]["selection_error_runs"] == [
        {"face": "utils", "arm": "S1", "rounds": [1]}]


def test_d4_ensure_map_faces_validates_arms_when_faces_already_covered(
        tmp_path, monkeypatch):
    """D4 根因点 1：面已覆盖不得早退跳过执行完整性门。"""
    bench, kol, work, map_path = _bench_fixture(tmp_path)
    map_path.write_text(json.dumps(_map_doc(
        {"utils/a.gd": "x", "systems/b.gd": "y"},
        _zero_test_arms())), encoding="utf-8")

    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("measure must not run when faces are covered")

    monkeypatch.setattr(bench, "_run", boom)
    with pytest.raises(SystemExit) as ei:
        bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path,
                                work, False)
    assert "execution-integrity" in str(ei.value)


def test_d4_ensure_map_faces_validates_arms_in_analyze_only(tmp_path):
    """--analyze-only 同样执行执行完整性门（不是覆盖错误信息）。"""
    bench, kol, work, map_path = _bench_fixture(tmp_path)
    map_path.write_text(json.dumps(_map_doc(
        {"utils/a.gd": "x", "systems/b.gd": "y"},
        _zero_test_arms())), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        bench._ensure_map_faces({}, kol, ["utils", "systems"], map_path,
                                work, True)
    assert "execution-integrity" in str(ei.value)


def test_d4_pending_runs_exit_nonzero(tmp_path, monkeypatch, capsys):
    """未跑完（pending）不得以 0 退出——"未判定"不是"通过"。"""
    bench = _load_bench()
    plan_file = tmp_path / "archived.json"
    plan_file.write_text(json.dumps(
        {"plan": _plan_one(), "protocol": {"aa": 1, "rounds": 1}}),
        encoding="utf-8")
    out = tmp_path / "o.json"
    work = tmp_path / "runs"
    work.mkdir()
    import sys
    monkeypatch.setattr(sys, "argv", [
        "bench", "--kol-root", "/nonexistent", "--targets", "utils",
        "--rounds", "1", "--aa", "1", "--budget", "1",
        "--out", str(out), "--work-dir", str(work),
        "--plan-file", str(plan_file), "--analyze-only"])
    assert bench.main() == 1
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["status"] == "pending" and doc["ok"] is False


def test_d2_stale_forensics_pruned_to_single_copy(tmp_path):
    """E2E 取证副本不累积：失效旧映射后仅保留最新一份 *.stale-*。"""
    e2e = _load_e2e()
    map_path = tmp_path / "attribution-map.json"
    map_path.write_text('{"cur": 1}', encoding="utf-8")
    (tmp_path / "attribution-map.json.stale-111").write_text("old1")
    (tmp_path / "attribution-map.json.stale-222").write_text("old2")
    e2e._invalidate_existing_map(map_path)
    stale = list(tmp_path.glob("attribution-map.json.stale-*"))
    assert len(stale) == 1 and stale[0].read_text() == '{"cur": 1}'


# ---------------------------------------------------------------- D5：对账口径


def test_d5_all_run_error_rounds_red(tmp_path):
    """D5 剧本：全轮 run_error（不可判定）→ compared=0 → 必须红。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    verr = {"utils/x.gd#f#1": "run_error", "utils/x.gd#f#2": "run_error",
            "utils/x.gd#f#3": "run_error"}
    _fill_face(bench, work, "utils", verdicts=verr,
               walls={"S0": 100.0, "S1": 95.0, "S2": 99.0, "S3": 110.0})
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    assert rep["gate"]["passed"] is False
    fails = {g["arm"] for g in rep["gate"]["compared_gate_failures"]}
    assert fails == {"S0", "S1", "S2"}
    for arm in ("S0", "S1", "S2"):
        assert rep["faces"]["utils"]["verdict_reconciliation"][arm][
            "compared"] == 0


def test_d5_mixed_rounds_reconcile_decidable_only(tmp_path):
    """混合轮：可判定部分正常对账（含 mismatch），run_error 不凑数、
    不参与 mismatch，计入 undecidable_excluded 披露。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    # S3: id1 killed / id2 killed / id3 survived
    # S2: id1 killed（一致）、id2 run_error（不可判定→排除）、
    #     id3 survived（一致）→ compared=2、mismatch=0、undec=1
    s3v = {"utils/x.gd#f#1": "killed", "utils/x.gd#f#2": "killed",
           "utils/x.gd#f#3": "survived"}
    s2v = {"utils/x.gd#f#1": "killed", "utils/x.gd#f#2": "run_error",
           "utils/x.gd#f#3": "survived"}
    _write_run(bench, work, "utils", "S2", "aa1", verdicts=s2v, wall=95.0)
    for arm in bench.ARMS:
        _write_run(bench, work, "utils", arm, 1,
                   verdicts=(s3v if arm == "S3" else s2v), wall=95.0)
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    r = rep["faces"]["utils"]["verdict_reconciliation"]["S2"]
    assert r["compared"] == 2 and r["mismatch_count"] == 0
    assert r["undecidable_excluded"] == 1
    assert rep["gate"]["passed"] is True


def test_d5_undecidable_mismatch_not_counted_but_disclosed(tmp_path):
    """S3=killed vs 臂=run_error：既不计入 compared 也不计 mismatch，
    只进 undecidable_excluded（排除不掩盖——可判定样本量随之下降）。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    s3v = {"utils/x.gd#f#1": "killed"}
    s2v = {"utils/x.gd#f#1": "run_error"}
    _write_run(bench, work, "utils", "S2", "aa1", verdicts=s2v, wall=95.0)
    for arm in bench.ARMS:
        _write_run(bench, work, "utils", arm, 1,
                   verdicts=(s3v if arm == "S3" else s2v), wall=95.0)
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    r = rep["faces"]["utils"]["verdict_reconciliation"]["S2"]
    assert r["compared"] == 0 and r["mismatch_count"] == 0
    assert r["undecidable_excluded"] == 1
    assert rep["gate"]["passed"] is False  # compared==0 门


def test_d5_healthy_decidable_mix_unaffected(tmp_path):
    """健康轮：killed/survived 混合对账不受影响；mismatch 仍被抓。"""
    bench = _load_bench()
    work = tmp_path / "runs"
    plan = _plan_one()
    s3v = {"utils/x.gd#f#1": "killed", "utils/x.gd#f#2": "survived"}
    same = {"utils/x.gd#f#1": "killed", "utils/x.gd#f#2": "survived"}
    diff = {"utils/x.gd#f#1": "killed", "utils/x.gd#f#2": "killed"}
    _write_run(bench, work, "utils", "S2", "aa1", verdicts=same, wall=95.0)
    for arm in bench.ARMS:
        _write_run(bench, work, "utils", arm, 1,
                   verdicts=(s3v if arm == "S3" else same), wall=95.0)
    rep = bench._analyze(plan, work, aa=1, rounds=1)
    r = rep["faces"]["utils"]["verdict_reconciliation"]["S2"]
    assert r["compared"] == 2 and r["mismatch_count"] == 0
    assert r["undecidable_excluded"] == 0
    assert rep["gate"]["passed"] is True
    # 注回真 mismatch（S2 幸存 vs S3 击杀）→ 红
    work2 = tmp_path / "runs2"
    for arm in bench.ARMS:
        _write_run(bench, work2, "utils", arm, 1,
                   verdicts=(s3v if arm == "S3" else diff), wall=95.0)
    rep2 = bench._analyze(plan, work2, aa=1, rounds=1)
    assert rep2["gate"]["passed"] is False
    assert rep2["gate"]["verdict_mismatches"]
