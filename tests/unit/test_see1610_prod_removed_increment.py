"""SEE-1610 §SPEC-N1 — measure map-update 删除生产文件（prod_removed）语义。

判据（issue §SPEC-N1，Owner 2026-11-20「做第二步」）：
- 删除 prod 文件归类新增类 `prod_removed`（有基线边且不在盘上的 .gd）；
- 增量产物清掉旧图该文件死边（行事实主真值 purge → 派生视图单源重算）；
- 失效面 = 触达该文件的测试（行事实反查）∪ 静态直连消费者（gdself
  depth==1）重跑；
- target 面/文件哈希/可插桩面刷新；
- 残余 unknown 路径（非 .gd、无基线边的删除文件等）具名 fail-loud，
  对齐模块 docstring「拒绝，不静默」承诺。
"""

from __future__ import annotations

import json
import re
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.measurement.increment import (
    IncrementError,
    _increment_decision,
    _merge_arm,
    classify_changes,
    compute_invalidation,
    run_attribution_increment,
)
from godot_qa_toolkit.measurement.orchestrator import run_measurement


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


def _project(tmp_path: Path, with_consumer: bool = False) -> Path:
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
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    if with_consumer:
        (root / "tests" / "unit" / "test_consumer.gd").write_text(
            'extends GutTest\n\nconst _P := preload("res://src_game/node.gd")'
            "\n\nfunc test_never() -> void:\n\tassert_true(false)\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(TARGET_SRC)
    (game / "node.gd").write_text(TARGET2)
    return root


def _recipe_and_targets(project: Path, tmp_path: Path):
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(project)
    from godot_qa_toolkit.measurement.recipe import build_recipe

    recipe, error = build_recipe(project)
    assert error is None, error
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(recipe), encoding="utf-8")
    return recipe, sorted(str(p) for p in (project / "src_game").glob("*.gd"))


# ---------------------------------------------------------------- 分类


def test_classify_deleted_prod_with_edges_is_prod_removed(tmp_path: Path):
    root = _project(tmp_path)
    (root / "src_game" / "node.gd").unlink()   # 删除生产文件（有基线边）
    from godot_qa_toolkit.measurement.increment import load_baseline
    baseline = {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": "h1",
        "arms": {"gut_full": {
            "test_to_files": {"test_game.gd::test_ok": [
                "src_game/cell.gd", "src_game/node.gd"]},
            "test_to_functions": {},
            "script_to_files": {},
            "script_to_functions": {},
            "file_to_tests": {"src_game/node.gd": ["test_game.gd::test_ok"]},
            "file_to_scripts": {},
        }},
    }
    classes = classify_changes(root, [
        "src_game/node.gd",           # 有基线边且盘上已删 → prod_removed
        "src_game/ghost.gd",          # 盘上已删且无基线边 → unknown
        "docs/readme.txt",            # 非 .gd → unknown
    ], baseline)
    assert classes["prod_removed"] == ["src_game/node.gd"]
    assert classes["unknown"] == ["src_game/ghost.gd", "docs/readme.txt"]
    assert classes["prod_changed"] == [] and classes["prod_new"] == []


# ---------------------------------------------------------------- fail-loud


def test_decision_fails_loud_on_unknown_paths(tmp_path: Path):
    root = _project(tmp_path)
    baseline = {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": "h1",
        "arms": {"gut_full": {
            "test_to_files": {}, "test_to_functions": {},
            "script_to_files": {}, "script_to_functions": {},
            "file_to_tests": {}, "file_to_scripts": {},
        }},
    }
    recipe = {"recipe_hash": "h1"}
    with pytest.raises(IncrementError) as ei:
        _increment_decision(root, recipe, baseline,
                            ["docs/readme.txt", "src_game/ghost.gd"],
                            since_full=0, calibration_every=5,
                            rerun_full_face_ratio=0.5)
    msg = str(ei.value)
    assert "unknown" in msg
    # 具名：报错含被拒路径（不静默丢弃）
    assert "docs/readme.txt" in msg and "src_game/ghost.gd" in msg


# ---------------------------------------------------------------- 失效面


def test_invalidation_prod_removed_line_touchers_and_static(tmp_path: Path):
    root = _project(tmp_path, with_consumer=True)
    baseline = {
        "schema_version": "test-attribution/6",
        "report_recipe_hash": "h1",
        "arms": {"gut_full": {
            # 行事实反查：test_game 触达 node.gd（test 车道）
            "test_to_lines": {"test_game.gd::test_ok": {
                "src_game/node.gd": [4]}},
            # 脚本车道：test_game 的准备代码也触达
            "script_to_lines": {"test_game.gd": {"src_game/node.gd": [4]}},
            "test_to_functions": {}, "script_to_functions": {},
        }},
    }
    classes = {"test_changed": [], "test_new": [], "test_removed": [],
               "prod_changed": [], "prod_new": [],
               "prod_removed": ["src_game/node.gd"], "unknown": []}
    inv = compute_invalidation(root, baseline, classes, index=None)
    # 行事实反查（两车道）触达测试入重跑集
    assert "test_game.gd" in inv["rerun"]
    # 静态直连消费者（preload 字面量 depth==1）入重跑集
    assert "test_consumer.gd" in inv["rerun"]
    assert any(s.startswith("prod_removed_lines:") or
               s.startswith("static:") for v in inv["sources"].values()
               for s in v)


# ---------------------------------------------------------------- 合并


def test_merge_arm_purges_removed_rel_line_facts_and_views():
    arm = {
        "tests": 2, "scripts": 2, "files": 2, "pairs": 3,
        "function_pairs": 0, "line_pairs": 3,
        "test_to_lines": {
            "test_a.gd::t1": {"src_game/cell.gd": [5],
                              "src_game/node.gd": [4]},
            "test_b.gd::t2": {"src_game/node.gd": [4]},
        },
        "script_to_lines": {"test_a.gd": {"src_game/node.gd": [4]}},
    }
    merged = _merge_arm(arm, fresh={}, dropped=set(),
                        fid_by_line_by_rel={"src_game/cell.gd": {5: "f"}},
                        removed_rels={"src_game/node.gd"})
    # 死边清除：任何视图不再含 node.gd
    assert "src_game/node.gd" not in merged["file_to_tests"]
    assert "src_game/node.gd" not in merged["file_to_scripts"]
    assert "src_game/node.gd" not in merged["line_to_tests"]
    assert all("src_game/node.gd" not in by_rel
               for by_rel in merged["test_to_lines"].values())
    assert all("src_game/node.gd" not in by_rel
               for by_rel in merged["script_to_lines"].values())
    # 其余边保留
    assert merged["test_to_lines"]["test_a.gd::t1"] == {
        "src_game/cell.gd": [5]}
    assert merged["file_to_tests"]["src_game/cell.gd"] == ["test_a.gd::t1"]


# ------------------------------------------------------- 模板隔离对称性


def test_prepare_attr_templates_isolates_unparseable_file(
        tmp_path, monkeypatch, capsys):
    """SEE-1610 §SPEC-N1：单文件插桩失败逐文件隔离（与全量 unmeasured
    口径对称）——跳过该文件并 stderr 具名披露，不整体硬抛、不静默缩面。"""
    import godot_qa_toolkit.measurement.increment_runner as runner

    root = _project(tmp_path)
    recipe = {"recipe_hash": "h1"}

    real_instrument = runner._instrument_pair

    def poisoned(rel, src, registry, **kw):
        if rel == "src_game/cell.gd":
            return {"parse_error": "instrument_parse_error: poisoned"}
        return real_instrument(rel, src, registry, **kw)

    monkeypatch.setattr(runner, "_instrument_pair", poisoned)
    targets = sorted(str(p) for p in (root / "src_game").glob("*.gd"))
    templates = runner.prepare_attr_templates(root, targets, recipe, False)
    assert "src_game/cell.gd" not in templates          # 毒文件被隔离
    assert "src_game/node.gd" in templates              # 其余照常
    err = capsys.readouterr().err
    assert "src_game/cell.gd" in err and "skipped" in err


# ---------------------------------------------------------------- 端到端


def test_increment_prod_removed_end_to_end(tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    sp.run = BareNameFakeGodot(project)
    base_artifact = tmp_path / "base.attribution.json"
    run_measurement(project, targets, recipe, test_attribution=True,
                  attribution_out=str(base_artifact))
    assert base_artifact.is_file()
    # 删除生产文件 node.gd
    (project / "src_game" / "node.gd").unlink()
    inc_artifact = tmp_path / "inc.attribution.json"
    report = run_attribution_increment(
        project, targets, recipe, base_artifact, ["src_game/node.gd"],
        inc_artifact,
        rerun_full_face_ratio=1.0)   # fixture 单脚本占比恒 1.0，放宽帽走增量
    assert report["mode"] == "incremental"
    assert report["changed"]["prod_removed"] == ["src_game/node.gd"]
    assert "test_game.gd" in report["rerun_scripts"]
    doc = json.loads(inc_artifact.read_text(encoding="utf-8"))
    arm = doc["arms"]["gut_full"]
    # 产物无该文件死边（file 视图 / 行反查 / 行事实全干净）
    assert "src_game/node.gd" not in arm["file_to_tests"]
    assert "src_game/node.gd" not in arm["line_to_tests"]
    assert all("src_game/node.gd" not in by_rel
               for by_rel in arm["test_to_lines"].values())
    assert all("src_game/node.gd" not in by_rel
               for by_rel in arm["script_to_lines"].values())
    # target 面/文件哈希/可插桩面刷新
    assert not [t for t in doc["target_files"]
                if str(t).endswith("node.gd")]
    assert "src_game/node.gd" not in (doc["file_sha256"] or {})
    assert "src_game/node.gd" not in (doc["instrumentable_lines"] or {})
    # 增量块披露 prod_removed 与重跑来源
    assert doc["increment"]["changed_files"]["prod_removed"] == [
        "src_game/node.gd"]


class BareNameFakeGodot:
    """与 test_canonical_increment_see1581 同形：裸脚本名身份 FakeGodot。"""

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
