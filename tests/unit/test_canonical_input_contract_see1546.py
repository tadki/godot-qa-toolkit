"""SEE-1562 R21 — §SPEC-067 canonical 外部 JSON 全量输入校验。

回归基线来自 Revy R20 定向复验（157 项扩展矩阵 + 6 项嵌套反例）：
recipe 的 toolchain / gut_face / acceptance_face / features / key、
exclusions scope/entry/reason，以及 stability-baseline 的
file/fid/function_id/covered_line_ids/total/crap_line/provenance
等全部下游直接消费字段，必须在编排/注入前系统校验；布尔不得当数字，
NaN/±Infinity 拒绝，total 仅有限整数值或 null，身份串 strip 后非空。
非法输入 API 统一 CanonicalError、真实 CLI 输出可解析 tool JSON
ok=false，不得 traceback 或静默失配；合法输入行为不变。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from godot_qa_toolkit.canonical.orchestrator import (
    CanonicalError,
    _load_baseline_report,
    run_canonical,
)
from godot_qa_toolkit.canonical.recipe import (
    RECIPE_VERSION,
    build_recipe,
    recipe_hash,
    verify_recipe,
)

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="contract-fixture"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n")
    feats = root / "acceptance" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(
        "extends RefCounted\n\n\nfunc work(n: int) -> int:\n"
        "\tvar acc = 0\n\tfor i in range(n):\n\t\tacc += i\n\treturn acc\n")
    return root


def _verify_recipe_err(doc) -> str:
    ok, reason = verify_recipe(doc)
    assert ok is False
    assert reason
    return reason


def _legal_recipe(**overrides) -> dict:
    # §SPEC-068：空 features/dirs 被拒绝——合法基底用非空面
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": {"dirs": ["res://tests/"]},
        "acceptance_face": {"features": [{"key": "demo"}]},
        "toolchain": {},
        "exclusions": [],
    }
    recipe.update(overrides)
    recipe["recipe_hash"] = recipe_hash(recipe)
    return recipe


# ---------------------------------------------------------------- recipe 面

@pytest.mark.parametrize("root", [
    [1, 2], "recipe", 7, 1.5, True, False, None,
])
def test_recipe_root_must_be_object(root):
    assert "must be a JSON object" in _verify_recipe_err(root)


@pytest.mark.parametrize("exclusions", [
    None, "x", 7, 1.5, True, False, {"a": 1}, {"0": {}},
])
def test_recipe_exclusions_container_must_be_list(exclusions):
    reason = _verify_recipe_err({
        "recipe_version": RECIPE_VERSION,
        "exclusions": exclusions,
        "recipe_hash": "x",
    })
    assert "must be a list" in reason


@pytest.mark.parametrize("row", [
    "x", 7, 1.5, True, False, None, [], [1], (),
])
def test_recipe_exclusion_row_must_be_object(row):
    reason = _verify_recipe_err({
        "recipe_version": RECIPE_VERSION,
        "exclusions": [row],
        "recipe_hash": "x",
    })
    assert "exclusion row must be an object" in reason


@pytest.mark.parametrize("reason_value", [
    None, 7, 1.5, True, False, [], {}, "", "   ",
])
def test_recipe_exclusion_reason_must_be_nonempty_string(reason_value):
    reason = _verify_recipe_err({
        "recipe_version": RECIPE_VERSION,
        "exclusions": [{"scope": "gut_dir", "entry": "x",
                        "reason": reason_value}],
        "recipe_hash": "x",
    })
    assert "reason" in reason


@pytest.mark.parametrize("row", [
    {"reason": "r"},
    {"scope": None, "entry": "x", "reason": "r"},
    {"scope": 7, "entry": "x", "reason": "r"},
    {"scope": True, "entry": "x", "reason": "r"},
    {"scope": "", "entry": "x", "reason": "r"},
    {"scope": "gut_dir", "entry": None, "reason": "r"},
    {"scope": "gut_dir", "entry": 7, "reason": "r"},
    {"scope": "gut_dir", "entry": True, "reason": "r"},
    {"scope": "gut_dir", "entry": "   ", "reason": "r"},
    {"scope": "not-a-scope", "entry": "x", "reason": "r"},
])
def test_recipe_exclusion_identity_fields_must_be_legal(row):
    assert "exclusion" in _verify_recipe_err(
        _legal_recipe(exclusions=[row]))


@pytest.mark.parametrize("value", [7, 1.5, True, "x", [], None])
def test_recipe_gut_face_must_be_object_with_list_dirs(value):
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": value,
        "acceptance_face": {"features": []},
        "exclusions": [],
    }
    recipe["recipe_hash"] = recipe_hash(recipe)
    assert "gut_face" in _verify_recipe_err(recipe)


@pytest.mark.parametrize("dirs", [7, 1.5, True, "x", {}, None, [7], [""]])
def test_recipe_gut_face_dirs_must_be_list(dirs):
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": {"dirs": dirs},
        "acceptance_face": {"features": []},
        "exclusions": [],
    }
    recipe["recipe_hash"] = recipe_hash(recipe)
    assert "dirs" in _verify_recipe_err(recipe)


@pytest.mark.parametrize("value", [7, 1.5, True, "x", [], None])
def test_recipe_acceptance_face_must_be_object_with_feature_list(value):
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": {"dirs": []},
        "acceptance_face": value,
        "exclusions": [],
    }
    recipe["recipe_hash"] = recipe_hash(recipe)
    assert "acceptance_face" in _verify_recipe_err(recipe)


@pytest.mark.parametrize("features", [
    7, 1.5, True, "x", {}, None, ["x"], [7], [{"source": "s"}],
    [{"key": None}], [{"key": 7}], [{"key": "   "}],
])
def test_recipe_features_must_be_objects_with_legal_key(features):
    assert "feature" in _verify_recipe_err(
        _legal_recipe(acceptance_face={"features": features}))


@pytest.mark.parametrize("value", [7, 1.5, True, "x", [], ["x"], None])
def test_recipe_toolchain_must_be_object(value):
    assert "toolchain" in _verify_recipe_err(_legal_recipe(toolchain=value))


def test_recipe_hash_non_serializable_is_domain_error():
    reason = _verify_recipe_err({
        "recipe_version": RECIPE_VERSION,
        "exclusions": [],
        "recipe_hash": "x",
        "junk": {1, 2},
    })
    assert "serializable" in reason


@pytest.mark.parametrize("exclusions", [
    "x", 7, True, False, {"a": 1}, ["x"], [7],
    [{"scope": ["gut_dir"], "entry": "x", "reason": "r"}],
    [{"scope": "gut_dir", "entry": 7, "reason": "r"}],
    [{"scope": "gut_dir", "entry": "x", "reason": 123}],
])
def test_build_recipe_illegal_exclusions_return_error(project, exclusions):
    recipe, error = build_recipe(project, exclusions=exclusions)
    assert recipe is None
    assert error


def test_build_and_verify_legal_recipe_control(project):
    """合法路径不变：build_recipe 产物（含完整 faces/toolchain/exclusions）
    必须通过全量校验。"""
    recipe, error = build_recipe(project, exclusions=[
        {"scope": "gut_dir", "entry": "res://tests/old/",
         "reason": "retired face"}])
    assert error is None
    assert verify_recipe(recipe) == (True, None)


# -------------------------------------------------------------- baseline 面

def _baseline_doc(**overrides) -> dict:
    doc = {
        "unions": [{
            "file": "res://a.gd",
            "union": {"per_fid": [
                {"fid": "f", "covered_line_ids": [1], "total": 1}]},
        }],
        "crap": {"records": []},
    }
    doc.update(overrides)
    return doc


def _write_json(path: Path, doc) -> str:
    path.write_text(json.dumps(doc, allow_nan=True), encoding="utf-8")
    return str(path)


def _baseline_err(tmp_path: Path, doc) -> str:
    path = _write_json(tmp_path / "b.json", doc)
    with pytest.raises(CanonicalError) as ei:
        _load_baseline_report(path)
    return str(ei.value)


@pytest.mark.parametrize("root", [
    [1, 2], "x", 7, 1.5, True, False, None,
])
def test_baseline_root_must_be_object(tmp_path, root):
    assert "must be a JSON object" in _baseline_err(tmp_path, root)


@pytest.mark.parametrize("unions", [
    "x", 7, True, {}, None, [7], ["x"], [True],
])
def test_baseline_unions_must_be_list_of_objects(tmp_path, unions):
    reason = _baseline_err(tmp_path, _baseline_doc(unions=unions))
    assert "unions" in reason or "union row" in reason


@pytest.mark.parametrize("union", [7, "x", True, []])
def test_baseline_union_must_be_object_or_null(tmp_path, union):
    doc = _baseline_doc(unions=[{"file": "res://a.gd", "union": union}])
    assert "'union' must be an object or null" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("file", [None, 7, 1.5, True, "", "   ", [], {}])
def test_baseline_file_must_be_nonempty_string(tmp_path, file):
    doc = _baseline_doc(unions=[{"file": file, "union": {"per_fid": []}}])
    assert "'file' must be a non-empty string" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("per_fid", [
    "x", 7, True, {}, None, ["x"], [7],
])
def test_baseline_per_fid_must_be_list_of_objects(tmp_path, per_fid):
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": per_fid}}])
    assert "per_fid" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("fid", [None, 7, 1.5, True, "", "   ", [], {}])
def test_baseline_fid_must_be_nonempty_string(tmp_path, fid):
    row = {"fid": fid, "covered_line_ids": [1], "total": 1}
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": [row]}}])
    assert "'fid' must be a non-empty string" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("covered", [
    None, "1", 7, True, {}, [None], [True], [False], [[1]], [{}],
    [float("nan")], [float("inf")], [float("-inf")],
])
def test_baseline_covered_line_ids_must_be_finite_scalars(
        tmp_path, covered):
    row = {"fid": "f", "covered_line_ids": covered, "total": 1}
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": [row]}}])
    assert "'covered_line_ids'" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("total", [
    "1", "abc", True, False, {}, [], 1.5, 2.25,
    float("nan"), float("inf"), float("-inf"),
])
def test_baseline_total_must_be_finite_integer_or_null(tmp_path, total):
    row = {"fid": "f", "covered_line_ids": [1], "total": total}
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": [row]}}])
    assert "'total' must be a" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("crap", [7, "x", True, [], None])
def test_baseline_crap_must_be_object(tmp_path, crap):
    assert "'crap' must be an object" in _baseline_err(
        tmp_path, _baseline_doc(crap=crap))


@pytest.mark.parametrize("records", [
    "x", 7, True, {}, None, ["x"], [7],
])
def test_baseline_crap_records_must_be_list_of_objects(tmp_path, records):
    reason = _baseline_err(
        tmp_path, _baseline_doc(crap={"records": records}))
    assert "crap.records" in reason or "crap record" in reason


@pytest.mark.parametrize("fid", [None, 7, 1.5, True, "", "   ", [], {}])
def test_baseline_crap_function_id_must_be_nonempty_string(tmp_path, fid):
    assert "'function_id' must be a non-empty string" in _baseline_err(
        tmp_path, _baseline_doc(crap={"records": [{"function_id": fid}]}))


@pytest.mark.parametrize("field", ["crap_line", "provenance"])
@pytest.mark.parametrize("value", [7, 1.5, True, "x", [], ["x"]])
def test_baseline_replay_fields_must_be_object_or_null(
        tmp_path, field, value):
    """R20 嵌套反例：`_crap_replay_vanished_baseline` 对 `crap_line` /
    `provenance` 直接 `dict(...)`——非对象即 TypeError/ValueError。"""
    record = {"function_id": "res://a.gd::f", "score_status": "scored",
              field: value}
    reason = _baseline_err(
        tmp_path, _baseline_doc(crap={"records": [record]}))
    assert field in reason


@pytest.mark.parametrize("score_status", [7, True, [], {}, "", "   "])
def test_baseline_crap_score_status_must_be_legal_string(
        tmp_path, score_status):
    record = {"function_id": "res://a.gd::f", "score_status": score_status}
    reason = _baseline_err(
        tmp_path, _baseline_doc(crap={"records": [record]}))
    assert "score_status" in reason


def test_baseline_legal_control_and_union_null(tmp_path):
    doc = {
        "unions": [
            {"file": "res://a.gd",
             "union": {"per_fid": [
                 {"fid": "f", "covered_line_ids": ["0", "1", 2], "total": 3},
                 {"fid": "g", "covered_line_ids": [4], "total": None},
                 {"fid": "h", "covered_line_ids": [5], "total": 2.0}]}},
            {"file": "res://gone.gd", "union": None},
        ],
        "crap": {"records": [
            {"function_id": "res://a.gd::f",
             "score_status": "scored",
             "crap_line": {"score": 1.0},
             "provenance": {"arm": "gut_full"}},
            {"function_id": "res://a.gd::g"},
        ]},
    }
    path = _write_json(tmp_path / "b.json", doc)
    lines, records = _load_baseline_report(path)
    assert lines["res://a.gd"]["f"] == (frozenset({"0", "1", 2}), 3)
    assert lines["res://a.gd"]["g"] == (frozenset({4}), 0)
    assert lines["res://a.gd"]["h"] == (frozenset({5}), 2)
    assert "res://gone.gd" not in lines
    assert records["res://a.gd::f"]["crap_line"] == {"score": 1.0}
    assert records["res://a.gd::g"] == {"function_id": "res://a.gd::g"}


# --------------------------------------------------- API 下游注入点反例（R20-H1）

def _full_recipe(project: Path, tmp_path: Path) -> dict:
    recipe, error = build_recipe(project)
    assert error is None
    return recipe


def _targets(project: Path) -> list[str]:
    return sorted(str(p) for p in (project / "src_game").glob("*.gd"))


@pytest.mark.parametrize("toolchain", [7, "x", ["gqt_version"], 1.5, True])
def test_api_run_canonical_bad_toolchain_is_domain_error(
        project, tmp_path, toolchain):
    """R20-H1：toolchain 非 dict 原在 `_inject_toolchain`（orchestrator.py:315）
    崩溃——必须在编排前转 CanonicalError。"""
    recipe = _full_recipe(project, tmp_path)
    recipe["toolchain"] = toolchain
    recipe["recipe_hash"] = recipe_hash(recipe)
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe)


@pytest.mark.parametrize("gut_face", [None, 7, "x", []])
def test_api_run_canonical_bad_gut_face_is_domain_error(
        project, tmp_path, gut_face):
    """R20-H1：gut_face 缺失/非 dict 原在分片计划（orchestrator.py:616）
    崩溃。"""
    recipe = _full_recipe(project, tmp_path)
    if gut_face is None:
        recipe.pop("gut_face")
    else:
        recipe["gut_face"] = gut_face
    recipe["recipe_hash"] = recipe_hash(recipe)
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe)


@pytest.mark.parametrize("acceptance_face", [None, 7, "x",
                                             {"features": 7},
                                             {"features": [{"source": "s"}]}])
def test_api_run_canonical_bad_acceptance_face_is_domain_error(
        project, tmp_path, acceptance_face):
    """R20-H1：acceptance_face/features/key 原在 feature key 展开
    （orchestrator.py:878 / 1448）崩溃或静默错配。"""
    recipe = _full_recipe(project, tmp_path)
    if acceptance_face is None:
        recipe.pop("acceptance_face")
    else:
        recipe["acceptance_face"] = acceptance_face
    recipe["recipe_hash"] = recipe_hash(recipe)
    with pytest.raises(CanonicalError):
        run_canonical(project, _targets(project), recipe)


# ------------------------------------------------------------- 真实 CLI 双路径

def _cli(recipe_doc, tmp_path: Path, extra_args=(), stdin=None):
    recipe = tmp_path / "recipe.json"
    recipe.write_text(json.dumps(recipe_doc, allow_nan=True),
                      encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if k not in ("HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                        "PYTHONUSERBASE", "PYTHONPATH")}
    env.update({
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "XDG_DATA_HOME": str(tmp_path / "xdg"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "PYTHONPATH": str(REPO / "src"),
        "GQT_GODOT_BIN": os.environ.get(
            "GQT_GODOT_BIN", sys.executable),
    })
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", "canonical",
         "/nonexistent-root", "--project-root", "/nonexistent-project",
         "--recipe", str(recipe), "--out", str(tmp_path / "out.json"),
         *extra_args],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1
    # stdout 必须整体可解析为 tool JSON（即无 traceback 泄漏）
    payload = json.loads(proc.stdout)
    assert payload["tool"] == "canonical"
    assert payload["ok"] is False
    return payload


def test_real_cli_rejects_non_dict_recipe_root(tmp_path):
    payload = _cli([1, 2], tmp_path)
    assert "must be a JSON object" in payload["summary"]["error"]


def test_real_cli_rejects_bad_exclusion_row(tmp_path):
    payload = _cli({
        "recipe_version": RECIPE_VERSION,
        "exclusions": ["x"],
        "recipe_hash": "x",
    }, tmp_path)
    assert "exclusion row must be an object" in payload["summary"]["error"]


def test_real_cli_rejects_bad_toolchain(tmp_path):
    payload = _cli(_legal_recipe(toolchain=7), tmp_path)
    assert "toolchain" in payload["summary"]["error"]


def test_real_cli_rejects_bad_gut_face(tmp_path):
    payload = _cli(_legal_recipe(gut_face=7), tmp_path)
    assert "gut_face" in payload["summary"]["error"]


def _in_process_cli_baseline(project: Path, tmp_path: Path,
                             baseline_doc) -> dict:
    """baseline 在双臂之后加载，真实子进程会拉起整条 GUT 流水线；
    进程内 CLI（build_parser → args.func，与二进制同一代码路径）+
    FakeGodot 保持真实 CLI 语义且不依赖真机。"""
    import io
    import subprocess as sp
    from contextlib import redirect_stdout

    from godot_qa_toolkit.cli import build_parser

    from .test_canonical_report_see1546 import FakeGodot

    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(json.dumps(_legal_recipe()), encoding="utf-8")
    baseline = _write_json(tmp_path / "baseline.json", baseline_doc)
    out_path = tmp_path / "out.json"
    args = build_parser().parse_args([
        "canonical", str(project / "src_game"),
        "--project-root", str(project),
        "--recipe", str(recipe_path),
        "--stability-baseline", baseline,
        "--out", str(out_path)])
    # §SPEC-075：CI 纯净环境无 godot——CLI 前置校验需 GQT_GODOT_BIN
    # 指向存在且可执行的二进制（FakeGodot 负责 GUT 执行，不依赖真机）
    old_bin = os.environ.get("GQT_GODOT_BIN")
    os.environ["GQT_GODOT_BIN"] = sys.executable
    orig = sp.run
    sp.run = FakeGodot(project)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = args.func(args)
    finally:
        sp.run = orig
        if old_bin is None:
            os.environ.pop("GQT_GODOT_BIN", None)
        else:
            os.environ["GQT_GODOT_BIN"] = old_bin
    assert code == 1
    payload = json.loads(buf.getvalue())
    assert payload["tool"] == "canonical"
    assert payload["ok"] is False
    assert payload["summary"]["canonical_error"] is True
    return payload


def test_real_cli_rejects_bad_baseline_replay_field(project, tmp_path):
    payload = _in_process_cli_baseline(project, tmp_path, _baseline_doc(
        crap={"records": [{"function_id": "res://a.gd::f",
                           "crap_line": 7}]}))
    assert "crap_line" in payload["summary"]["error"]


def test_real_cli_rejects_bool_total_baseline(project, tmp_path):
    payload = _in_process_cli_baseline(project, tmp_path, _baseline_doc(
        unions=[{
            "file": "res://a.gd",
            "union": {"per_fid": [{"fid": "f", "covered_line_ids": [1],
                                   "total": True}]}}]))
    assert "'total' must be a" in payload["summary"]["error"]


# ---------------------------------------------- §SPEC-068 build↔verify 自洽

def test_build_rejects_blank_entry_like_verify(project):
    """§SPEC-068 跨命令反例①：空白 entry 不得再由 build 产出后被 verify
    拒绝——build 与 verify 对 entry 采用同一 strip 非空规则。"""
    recipe, error = build_recipe(project, exclusions=[
        {"scope": "gut_dir", "entry": "   ", "reason": "r"}])
    assert recipe is None and error and "scope/entry" in error


@pytest.mark.parametrize("dirs", [
    [1], ["res://a/", ""], ["   "], "res://tests/", {"a": 1},
])
def test_build_rejects_illegal_gutconfig_dirs(project, dirs):
    """§SPEC-068 跨命令反例②：gutconfig dirs 含非 str / 空白项或非列表
    时 build 直接拒绝——不再产出会被自家 verify 拒绝的 recipe。"""
    (project / ".gutconfig.json").write_text(json.dumps({"dirs": dirs}))
    recipe, error = build_recipe(project)
    assert recipe is None and error and "dirs" in error, dirs


def test_build_verify_roundtrip_legal_control(project):
    """合法路径不变：build 产物（非空 dirs/features）必过 verify。"""
    recipe, error = build_recipe(project)
    assert error is None
    assert verify_recipe(recipe) == (True, None)


def test_verify_dirs_elements_strip_aligned_with_build():
    """§SPEC-068/R23 残余：verify 侧 gut_face.dirs 元素与 build 同一
    strip 非空规则——纯空白元素拒绝、含空白但非空白目录合法。"""
    reason = _verify_recipe_err(_legal_recipe(gut_face={"dirs": ["   "]}))
    assert "dirs" in reason
    ok, err = verify_recipe(
        _legal_recipe(gut_face={"dirs": [" res://tests/"]}))
    assert (ok, err) == (True, None)


def test_verify_rejects_empty_faces_structural_errors_first():
    """§SPEC-068：空 features/dirs 拒绝（与 build 非空要求一致）；面结构
    错误优先于面空性——dirs 空列表不得抢在 acceptance_face 类型错之前。"""
    recipe = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": {"dirs": []},
        "acceptance_face": {"features": []},
        "toolchain": {},
        "exclusions": [],
    }
    recipe["recipe_hash"] = recipe_hash(recipe)
    reason = _verify_recipe_err(recipe)
    assert "dirs" in reason or "features" in reason
    # 结构错误优先：acceptance_face 类型错不被 dirs 空列表抢先
    recipe2 = {
        "recipe_version": RECIPE_VERSION,
        "gut_face": {"dirs": []},
        "acceptance_face": 7,
        "toolchain": {},
        "exclusions": [],
    }
    recipe2["recipe_hash"] = recipe_hash(recipe2)
    assert "acceptance_face" in _verify_recipe_err(recipe2)


@pytest.mark.parametrize("scope", [[], {}, ["gut_dir"], {"a": 1}])
def test_verify_rejects_unhashable_scope_gracefully(scope):
    """§SPEC-069 hardener: a non-str / unhashable exclusion scope must be a
    graceful domain rejection — never a TypeError escaping from the
    `scope not in LEGAL_SCOPES` frozenset membership test."""
    reason = _verify_recipe_err(_legal_recipe(exclusions=[
        {"scope": scope, "entry": "x", "reason": "r"}]))
    assert "scope" in reason


def test_verify_rejects_empty_dirs_with_valid_features():
    """§SPEC-069 hardener: empty `gut_face.dirs` is rejected on its own even
    when the acceptance face is structurally valid and non-empty (the face
    emptiness gate must not rely on another face being empty to fire)."""
    reason = _verify_recipe_err(_legal_recipe(gut_face={"dirs": []}))
    assert "dirs" in reason


def test_verify_rejects_empty_features_with_valid_dirs():
    """§SPEC-069 hardener: empty `acceptance_face.features` is rejected on its
    own even when the gut face is structurally valid and non-empty."""
    reason = _verify_recipe_err(_legal_recipe(acceptance_face={"features": []}))
    assert "features" in reason


# ------------------------------------------------ §SPEC-068 接受侧边界

@pytest.mark.parametrize("covered", [[2.5], [-0.5], [1.5, 2]])
def test_baseline_non_integer_float_line_ids_rejected(tmp_path, covered):
    """§SPEC-068：数值行号须为有限整数——2.0 合法、2.5 拒绝。"""
    row = {"fid": "f", "covered_line_ids": covered, "total": 1}
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": [row]}}])
    assert "'covered_line_ids'" in _baseline_err(tmp_path, doc)


@pytest.mark.parametrize("total", [-1, -5, -2.0])
def test_baseline_negative_total_rejected(tmp_path, total):
    row = {"fid": "f", "covered_line_ids": [1], "total": total}
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd", "union": {"per_fid": [row]}}])
    assert "'total' must be a" in _baseline_err(tmp_path, doc)


def test_baseline_integer_valued_float_and_compat_legal(tmp_path):
    """兼容行为不变：整值浮点行号/total、str/int 行集、union:null、
    total:null 全部保持合法。"""
    doc = {
        "unions": [
            {"file": "res://a.gd", "union": {"per_fid": [
                {"fid": "f", "covered_line_ids": ["0", 1, 2.0],
                 "total": 2.0},
                {"fid": "g", "covered_line_ids": [3.0], "total": None}]}},
            {"file": "res://gone.gd", "union": None},
        ],
        "crap": {"records": []},
    }
    path = _write_json(tmp_path / "b.json", doc)
    lines, _ = _load_baseline_report(path)
    assert lines["res://a.gd"]["f"] == (frozenset({"0", 1, 2.0}), 2)
    assert lines["res://a.gd"]["g"] == (frozenset({3.0}), 0)
    assert "res://gone.gd" not in lines


def test_baseline_duplicate_file_rejected(tmp_path):
    doc = _baseline_doc(unions=[
        {"file": "res://a.gd", "union": {"per_fid": []}},
        {"file": "res://a.gd", "union": {"per_fid": []}},
    ])
    assert "duplicate" in _baseline_err(tmp_path, doc)


def test_baseline_duplicate_fid_rejected(tmp_path):
    doc = _baseline_doc(unions=[{
        "file": "res://a.gd",
        "union": {"per_fid": [
            {"fid": "f", "covered_line_ids": [1], "total": 1},
            {"fid": "f", "covered_line_ids": [2], "total": 1},
        ]}}])
    assert "duplicate" in _baseline_err(tmp_path, doc)


def test_baseline_duplicate_function_id_rejected(tmp_path):
    doc = _baseline_doc(crap={"records": [
        {"function_id": "res://a.gd::f"},
        {"function_id": "res://a.gd::f"},
    ]})
    assert "duplicate" in _baseline_err(tmp_path, doc)
