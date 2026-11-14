"""SEE-1581 方向① — attribution 映射增量重建（探索轮，默认关闭路径零变化）。

覆盖：变更分类、失效面（fn 边反查 + 静态候选）、按脚本合并、provenance、
fallback 触发（new_prod / hash 失配）、逐边差分；端到端用 FakeGodot。
"""

from __future__ import annotations

import json
import shutil
import re
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.canonical.increment import (
    classify_changes,
    compute_invalidation,
    diff_artifacts,
    load_baseline,
    run_attribution_increment,
)
from godot_qa_toolkit.canonical.orchestrator import run_canonical

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""

TARGET2 = """extends Node


func ready_hook() -> void:
\tset_process(false)
"""


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


def _baseline_arm() -> dict:
    return {
        "tests": 2, "scripts": 1, "files": 2, "pairs": 3,
        "function_pairs": 2,
        "test_to_files": {
            "test_a.gd::test_one": ["src_game/cell.gd"],
            "test_b.gd::test_two": ["src_game/cell.gd", "src_game/node.gd"],
        },
        "test_to_functions": {
            "test_a.gd::test_one": ["src_game/cell.gd::work"],
            "test_b.gd::test_two": ["src_game/cell.gd::work",
                                    "src_game/node.gd::ready_hook"],
        },
        "script_to_files": {"test_a.gd": ["src_game/cell.gd"]},
        "script_to_functions": {"test_a.gd": ["src_game/cell.gd::work"]},
        "file_to_tests": {
            "src_game/cell.gd": ["test_a.gd::test_one", "test_b.gd::test_two"],
            "src_game/node.gd": ["test_b.gd::test_two"],
        },
        "file_to_scripts": {"src_game/cell.gd": ["test_a.gd"]},
    }


def _baseline_doc() -> dict:
    return {
        "schema_version": "canonical-test-attribution/4",
        "report_recipe_hash": "h1",
        "arms": {"gut_full": _baseline_arm()},
        "identity": "res://script::test_name",
    }


def test_classify_changes_maps_all_buckets(tmp_path: Path):
    root = tmp_path / "proj"
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "src_game").mkdir()
    (root / "tests" / "unit" / "test_a.gd").write_text("# t\n")
    (root / "tests" / "unit" / "test_new.gd").write_text("# t\n")
    (root / "src_game" / "cell.gd").write_text("# p\n")
    (root / "src_game" / "brand_new.gd").write_text("# p\n")
    baseline = _baseline_doc()
    classes = classify_changes(root, [
        "tests/unit/test_a.gd",          # 在基线 → changed
        "tests/unit/test_new.gd",        # 不在基线 → new
        "tests/unit/test_b.gd",          # 基线有、盘上无 → removed
        "src_game/cell.gd",              # 有基线边 → changed
        "src_game/brand_new.gd",         # 无基线边 → new（回退全脸）
    ], baseline)
    assert classes["test_changed"] == ["tests/unit/test_a.gd"]
    assert classes["test_new"] == ["tests/unit/test_new.gd"]
    assert classes["test_removed"] == ["tests/unit/test_b.gd"]
    assert classes["prod_changed"] == ["src_game/cell.gd"]
    assert classes["prod_new"] == ["src_game/brand_new.gd"]


def test_invalidation_combines_fn_edges_and_static_candidates(tmp_path: Path):
    baseline = _baseline_doc()
    classes = {"test_changed": ["tests/unit/test_a.gd"], "test_new": [],
               "test_removed": ["tests/unit/test_b.gd"],
               "prod_changed": ["src_game/node.gd"], "prod_new": [],
               "unknown": []}
    inv = compute_invalidation(tmp_path, baseline, classes, index=None)
    # fn 边反查：node.gd 的函数边 → test_b；再加 test_changed 的 test_a
    assert "test_b.gd" in inv["rerun"]
    assert "test_a.gd" in inv["rerun"]
    assert inv["dropped"] == {"test_b.gd"}


def test_merge_arm_replaces_by_script_and_drops():
    from godot_qa_toolkit.canonical.increment import _merge_arm
    fresh = {
        "test_to_files": {"test_a.gd::test_one": ["src_game/cell.gd",
                                                  "src_game/node.gd"]},
        "test_to_functions": {"test_a.gd::test_one":
                              ["src_game/cell.gd::work"]},
        "script_to_files": {"test_a.gd": ["src_game/cell.gd",
                                          "src_game/node.gd"]},
        "script_to_functions": {"test_a.gd": ["src_game/cell.gd::work"]},
    }
    merged = _merge_arm(_baseline_arm(), fresh, dropped={"test_b.gd"})
    # 重跑脚本整体替换（test_a 新增 node.gd 边），被删脚本边消失
    assert merged["test_to_files"]["test_a.gd::test_one"] == [
        "src_game/cell.gd", "src_game/node.gd"]
    assert "test_b.gd::test_two" not in merged["test_to_files"]
    assert merged["file_to_tests"]["src_game/node.gd"] == [
        "test_a.gd::test_one"]
    assert merged["pairs"] == 2 and merged["files"] == 2
    assert merged["file_to_tests"]["src_game/cell.gd"] == [
        "test_a.gd::test_one"]
    assert merged["tests"] == 1 and merged["scripts"] == 1


def test_diff_artifacts_counts_edges():
    a = _baseline_doc()
    b = _baseline_doc()
    b["arms"]["gut_full"]["test_to_files"]["test_a.gd::test_one"].append(
        "src_game/node.gd")
    d = diff_artifacts(a, b)
    assert d["test_to_files"]["added_count"] == 1
    assert d["test_to_files"]["removed_count"] == 0
    assert d["test_to_files"]["added_edges"] == [
        "test_a.gd::test_one -> src_game/node.gd"]
    assert d["test_to_functions"]["added_count"] == 0


def test_load_baseline_rejects_wrong_schema(tmp_path: Path):
    p = tmp_path / "b.json"
    p.write_text(json.dumps({"schema_version": "x"}), encoding="utf-8")
    with pytest.raises(Exception):
        load_baseline(p)


def test_execute_rerun_scripts_writes_attr_flusher(tmp_path: Path):
    """回归守卫（实测踩过）：增量重跑必须注入 ATTR finalizer/pre-hook——
    错配默认 flusher 时 hits 正常、归属静默全空。"""
    import godot_qa_toolkit.canonical.increment_runner as runner
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    templates = runner.prepare_attr_templates(
        project, targets, recipe, False)
    captured: list[str] = []

    def fake_run(project_root, gut_args, timeout_s, env=None, gate_root=None):
        hook = (Path(project_root) / "addons" / "gqt_cov_pre_hook.gd")
        flusher = (Path(project_root) / "addons" / "gqt_cov_flusher.gd")
        captured.append(hook.read_text(encoding="utf-8")
                        + flusher.read_text(encoding="utf-8"))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R(), 0.0

    orig = runner._run_one_gut
    runner._run_one_gut = fake_run
    try:
        results, xdg = runner.execute_rerun_scripts(
            project, ["res://tests/"], ["test_game.gd"], 60, templates,
            False, "uuid-test")
    finally:
        runner._run_one_gut = orig
    assert captured and "gqt_cov_attr" in captured[0]
    assert "gqt_cov_slot" in captured[0]
    assert not (project / "addons" / "gqt_cov_pre_hook.gd").exists()
    for x in xdg:
        shutil.rmtree(x, ignore_errors=True)


# ---------------------------------------------------------------- 端到端


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="incr-fixture"\n')
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
    (game / "cell.gd").write_text(TARGET_SRC)
    (game / "node.gd").write_text(TARGET2)
    return root


class BareNameFakeGodot:
    """与真实 GUT `get_filename_and_inner()` 同形：身份用裸脚本名。
    每次运行写一份可区分的归属证据（cnt 按 seed 递增）。"""

    def __init__(self, project_root: Path):
        self.real_run = sp.run
        self.root = project_root
        self.returncode = 0
        self.seed = 0

    def __call__(self, *a, **k):
        argv = a[0]
        cmd = " ".join(argv)
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        self.seed += 1
        env = k.get("env") or {}
        xdg = env.get("XDG_DATA_HOME")
        if xdg:
            pg = (self.root / "project.godot").read_text(encoding="utf-8")
            proj = re.search(r'config/name\s*=\s*"([^"]+)"', pg).group(1)
            dest = Path(xdg) / "godot" / "app_userdata" / proj
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest = self.root
        m = re.search(r"-gselect=(\S+)", cmd)
        stem = (m.group(1).replace(".gd", "") if m else "test_game")
        identity = f"{stem}.gd::test_ok"
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            for m2 in re.finditer(
                    r'qa-coverage-attr-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m2.group(1), m2.group(2)
                (dest / f"qa-coverage-attr-{uuid}-99999-{key8}.txt").write_text(
                    f"test 0\ncnt 0 {10 + self.seed}\nfn 0 0\n",
                    encoding="utf-8")
            for m2 in re.finditer(
                    r'qa-coverage-hits-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m2.group(1), m2.group(2)
                probe_ids = set(re.findall(
                    r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src))
                (dest / f"qa-coverage-hits-{uuid}-99999-{key8}.txt").write_text(
                    "".join(f"{i}\n" for i in sorted(probe_ids)),
                    encoding="utf-8")
        hook = self.root / "addons" / "gqt_cov_pre_hook.gd"
        if hook.is_file():
            for m2 in re.finditer(
                    r'qa-cov-attr-tests-([0-9a-f-]{8,36})-',
                    hook.read_text(encoding="utf-8")):
                uuid = m2.group(1)
                (dest / f"qa-cov-attr-tests-{uuid}-99999.txt").write_text(
                    f"slot 0\ttest\t{identity}\n", encoding="utf-8")

        class R:
            def __init__(self, rc):
                self.returncode = rc
                self.stdout = ""
                self.stderr = ""

        return R(self.returncode)


def _recipe_and_targets(project: Path, tmp_path: Path):
    from godot_qa_toolkit.canonical.recipe import build_recipe
    recipe, error = build_recipe(project)
    assert error is None, error
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return recipe, sorted(str(p) for p in (project / "src_game").glob("*.gd"))


def test_increment_end_to_end_refreshes_changed_prod_edges(
        tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    sp.run = BareNameFakeGodot(project)
    base_artifact = tmp_path / "base.attribution.json"
    run_canonical(project, targets, recipe, test_attribution=True,
                  attribution_out=str(base_artifact))
    assert base_artifact.is_file()
    # 生产代码变更：work() 增加一条语句
    cell = project / "src_game" / "cell.gd"
    cell.write_text(cell.read_text(encoding="utf-8").replace(
        "\treturn acc", "\tacc += 1\n\treturn acc"))
    inc_artifact = tmp_path / "inc.attribution.json"
    report = run_attribution_increment(
        project, targets, recipe, base_artifact, ["src_game/cell.gd"],
        inc_artifact,
        # fixture 只有一个测试脚本：rerun 占比恒 1.0，放宽比例帽以走增量路径
        rerun_full_face_ratio=1.0)
    assert report["mode"] == "incremental"
    # 失效面重跑的是「触达 cell.gd 的测试脚本」，不是生产文件本身
    assert report["rerun_scripts"] == ["test_game.gd"]
    assert report["event_counts_stale"] is True
    doc = json.loads(inc_artifact.read_text(encoding="utf-8"))
    inc = doc["increment"]
    assert inc["mode"] == "incremental"
    assert inc["base_recipe_hash"]  # provenance 携带基线身份
    assert inc["changed_files"]["prod_changed"] == ["src_game/cell.gd"]
    assert inc["event_counts_stale"] is True
    arm = doc["arms"]["gut_full"]
    # 重跑脚本的新鲜边：fake 身份 = 被选中脚本名（-gselect=test_game.gd）
    assert "test_game.gd::test_ok" in arm["test_to_files"]
    assert sorted(arm["test_to_files"]["test_game.gd::test_ok"]) == [
        "src_game/cell.gd", "src_game/node.gd"]
    d = diff_artifacts(json.loads(base_artifact.read_text(encoding="utf-8")),
                       doc)
    # 单脚本重跑 vs 全脸：边差分应很小（同树同 fake），允许少量漂移
    assert d["test_to_files"]["added_count"] <= 4
    assert d["test_to_files"]["removed_count"] <= 4


def test_increment_falls_back_on_new_prod_file(tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    sp.run = BareNameFakeGodot(project)
    base_artifact = tmp_path / "base.attribution.json"
    run_canonical(project, targets, recipe, test_attribution=True,
                  attribution_out=str(base_artifact))
    new_prod = project / "src_game" / "brand_new.gd"
    new_prod.write_text("extends RefCounted\nfunc x() -> void:\n\tpass\n")
    inc_artifact = tmp_path / "inc.attribution.json"
    report = run_attribution_increment(
        project, targets, recipe, base_artifact, ["src_game/brand_new.gd"],
        inc_artifact)
    assert report["mode"] == "new_prod_file_fallback"
    doc = json.loads(inc_artifact.read_text(encoding="utf-8"))
    assert doc["increment"]["mode"] == "new_prod_file_fallback"
    assert doc["increment"]["increments_since_full"] == 0
