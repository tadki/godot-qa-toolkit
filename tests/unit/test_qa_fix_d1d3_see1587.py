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
    return {"schema_version": "test-attribution/6",
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
    from godot_qa_toolkit.measurement.orchestrator import _arms_state

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
