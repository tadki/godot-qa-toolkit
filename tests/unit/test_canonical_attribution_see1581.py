"""SEE-1581 §SPEC-083 — canonical 可选 per-test 归属模式（试验，默认关闭）。

判据覆盖：
- 归属形态探针/注册块的生成（默认关闭字节不变、开启后 per-id 变量唯一）；
- GUT per-test 生命周期钩子与 finalizer 的归属排空接线；
- test→file / file→tests 映射聚合与无主命中披露；
- 默认关闭路径报告零新增键（既有输出形状不变）。
"""

from __future__ import annotations

import json
import re
import subprocess as sp
from pathlib import Path

import pytest

from godot_qa_toolkit.canonical.orchestrator import (
    ATTRIBUTION_SCHEMA,
    _ATTR_PRE_HOOK_GD,
    _ATTR_FLUSHER_GD,
    _PRE_HOOK_GD,
    _FLUSHER_GD,
    build_attribution_map,
    run_canonical,
)
from godot_qa_toolkit.coverage.runner import (
    _flag_probe_block,
    _instrument_blocks,
    _probe_calls,
)

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""

SINK = "qa-coverage-hits-GQTUUIDSENTINEL-0badf00d.txt"


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


# ---------------------------------------------------------------- probe 形态


def test_probe_calls_default_unchanged():
    line_to_id = {4: 0, 5: 1}
    out = "".join(_probe_calls(TARGET_SRC, line_to_id, True, SINK))
    assert "__qa_cov_probe_m_0badf00d[0] = 1" in out
    assert "gqt_cov_cur_test" not in out
    assert "gqt_cov_attr" not in out


def test_probe_calls_attribution_emits_identity_writes():
    line_to_id = {4: 0, 5: 1}
    out = "".join(_probe_calls(TARGET_SRC, line_to_id, True, SINK,
                               attribution=True))
    # 两处探针 → 两个互不重名的临时变量（同名 var 同一作用域会 parse error）
    assert out.count("Engine.get_meta(\"gqt_cov_cur_test\", -1)") == 2
    assert "__qa_cov_probe_t0" in out and "__qa_cov_probe_t1" in out
    assert "__qa_cov_probe_s0" in out and "__qa_cov_probe_s1" in out
    # 已归属/无主两条计数路径都在
    assert "__qa_cov_probe_a_0badf00d[1][0] += 1" in out
    assert "__qa_cov_probe_a_0badf00d[1][1] += 1" in out
    # 行覆盖 flag 仍保留（归属是叠加而非替代）
    assert "__qa_cov_probe_m_0badf00d[0] = 1" in out


def test_flag_probe_block_attribution_registers_attr_sink():
    default = _flag_probe_block(SINK, 2, runtime_sink=True)
    assert "gqt_cov_attr" not in default
    assert "qa-coverage-attr-" not in default

    attr = _flag_probe_block(SINK, 2, runtime_sink=True, attribution=True)
    assert "gqt_cov_attr" in attr
    assert "qa-coverage-attr-GQTUUIDSENTINEL-" in attr
    assert "[[], [0, 0]]" in attr


def test_instrument_blocks_attribution_parses():
    gdtoolkit_parser = pytest.importorskip("gdtoolkit.parser")
    inst, starts, bmap = _instrument_blocks(
        TARGET_SRC, SINK, runtime_sink=True, attribution=True)
    assert starts
    gdtoolkit_parser.parser.parse(inst)  # 插桩产物必须可解析


def test_hooks_carry_per_test_lifecycle_and_drain():
    for sig in ("start_script", "start_test", "end_test", "end_script"):
        assert f"gut.{sig}.connect" in _ATTR_PRE_HOOK_GD
    assert "gqt_cov_cur_test" in _ATTR_PRE_HOOK_GD
    assert "__GQT_RUN_UUID__" in _ATTR_PRE_HOOK_GD
    assert "qa-cov-attr-tests-" in _ATTR_PRE_HOOK_GD
    # 默认钩子不含归属接线（默认路径零变化）
    assert "start_test" not in _PRE_HOOK_GD
    assert "gqt_cov_attr" in _ATTR_FLUSHER_GD
    assert "gqt_cov_attr" not in _FLUSHER_GD


# ---------------------------------------------------------------- 映射聚合


def test_build_attribution_map_joins_registry():
    attr_by_rel = {
        "src_game/cell.gd": {
            101: {"seen": {0, 1}, "attributed": 20, "unattributed": 4},
        },
        "src_game/node.gd": {
            101: {"seen": {1}, "attributed": 5, "unattributed": 0},
        },
    }
    registries = {101: {0: "res://tests/unit/test_a.gd::test_one",
                        1: "res://tests/unit/test_b.gd::test_two"}}
    m = build_attribution_map("gut_full", attr_by_rel, registries)
    assert m["arm"] == "gut_full"
    assert m["tests"] == 2
    assert m["files"] == 2
    assert m["pairs"] == 3
    assert m["attributed_hit_events"] == 25
    assert m["unattributed_hit_events"] == 4
    assert m["unattributed_rate"] == round(4 / 29, 6)
    assert m["files_with_unattributed"] == 1
    assert m["test_to_files"] == {
        "res://tests/unit/test_a.gd::test_one": ["src_game/cell.gd"],
        "res://tests/unit/test_b.gd::test_two": [
            "src_game/cell.gd", "src_game/node.gd"],
    }
    assert m["file_to_tests"]["src_game/node.gd"] == [
        "res://tests/unit/test_b.gd::test_two"]
    assert m["unmapped_count"] == 0


def test_build_attribution_map_unmapped_is_disclosed_not_guessed():
    attr_by_rel = {"src_game/cell.gd": {
        7: {"seen": {3}, "attributed": 2, "unattributed": 0}}}
    m = build_attribution_map("gut_full", attr_by_rel, {7: {}})
    assert m["tests"] == 0
    assert m["unmapped_count"] == 1
    assert m["unmapped_test_indices"] == [
        {"file": "src_game/cell.gd", "pid": 7, "test_index": 3}]


# ---------------------------------------------------------------- 端到端


TARGET2 = """extends Node


func ready_hook() -> void:
\tset_process(false)
"""


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="attr-fixture"\n')
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


class FakeGodot:
    """命中 sink 与归属 sink 双写：hits 写全量 probe id；attr 写测试 0 命中
    + 已归属 10 / 无主 2；registry 写 `R 0<TAB>...::test_ok`。"""

    def __init__(self, project_root: Path):
        self.real_run = sp.run
        self.root = project_root
        self.returncode = 0
        self.stderr_text = ""
        self.pre_hook_texts: list[str] = []

    def __call__(self, *a, **k):
        argv = a[0]
        cmd = " ".join(argv)
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        hook = self.root / "addons" / "gqt_cov_pre_hook.gd"
        self.pre_hook_texts.append(
            hook.read_text(encoding="utf-8") if hook.is_file() else "")
        env = k.get("env") or {}
        xdg = env.get("XDG_DATA_HOME")
        if xdg:
            pg = (self.root / "project.godot").read_text(encoding="utf-8")
            proj = re.search(r'config/name\s*=\s*"([^"]+)"', pg).group(1)
            dest = Path(xdg) / "godot" / "app_userdata" / proj
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest = self.root
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            probe_ids = set(re.findall(
                r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src))
            for m in re.finditer(
                    r'qa-coverage-hits-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m.group(1), m.group(2)
                (dest / f"qa-coverage-hits-{uuid}-99999-{key8}.txt").write_text(
                    "".join(f"{i}\n" for i in sorted(probe_ids)),
                    encoding="utf-8")
            for m in re.finditer(
                    r'qa-coverage-attr-([0-9a-f-]{8,36})-" \+ '
                    r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                    src):
                uuid, key8 = m.group(1), m.group(2)
                (dest / f"qa-coverage-attr-{uuid}-99999-{key8}.txt").write_text(
                    "T 0\nA 10\nU 2\n", encoding="utf-8")
        hook_text = self.pre_hook_texts[-1]
        m = re.search(r"qa-cov-attr-tests-([0-9a-f-]{8,36})-", hook_text)
        if m:
            (dest / f"qa-cov-attr-tests-{m.group(1)}-99999.txt").write_text(
                "R 0\tres://tests/unit/test_game.gd::test_ok\n",
                encoding="utf-8")

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


def test_default_run_has_no_attribution_outputs(tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    sp.run = FakeGodot(project)
    report = run_canonical(project, targets, recipe)
    assert "test_attribution" not in report
    assert all("attribution" not in arm for arm in report["arms"])
    assert not any("start_test" in t for t in sp.run.pre_hook_texts)


def test_attribution_run_emits_mapping_artifact(tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    fake = FakeGodot(project)
    sp.run = fake
    out = tmp_path / "canonical.json"
    artifact_path = tmp_path / "canonical.attribution.json"
    report = run_canonical(project, targets, recipe,
                           test_attribution=True,
                           attribution_out=str(artifact_path))
    summary = report["test_attribution"]
    assert summary["schema_version"] == ATTRIBUTION_SCHEMA
    assert summary["enabled"] is True
    assert summary["artifact"] == str(artifact_path)
    gut = summary["arms"]["gut_full"]
    assert gut["tests"] == 1
    assert gut["attributed_hit_events"] > 0
    assert gut["unattributed_hit_events"] > 0
    assert gut["unattributed_rate"] == round(
        gut["unattributed_hit_events"]
        / (gut["unattributed_hit_events"] + gut["attributed_hit_events"]), 6)
    # 默认关闭的键不得出现
    assert all("attribution" not in arm for arm in report["arms"])

    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == ATTRIBUTION_SCHEMA
    assert artifact["report_recipe_hash"] == report["recipe_hash"]
    mapping = artifact["arms"]["gut_full"]["test_to_files"]
    assert mapping["res://tests/unit/test_game.gd::test_ok"] == [
        "src_game/cell.gd", "src_game/node.gd"]
    # 归属钩子确实注入（含 run_uuid 替换，无占位符残留）
    assert any("start_test.connect" in t for t in fake.pre_hook_texts)
    assert all("__GQT_RUN_UUID__" not in t for t in fake.pre_hook_texts)


def test_attribution_leaves_no_residue(tmp_path: Path):
    project = _project(tmp_path)
    recipe, targets = _recipe_and_targets(project, tmp_path)
    before = {p: p.read_bytes() for p in (project / "src_game").glob("*.gd")}
    sp.run = FakeGodot(project)
    run_canonical(project, targets, recipe, test_attribution=True)
    for p, data in before.items():
        assert p.read_bytes() == data
    assert not list(project.glob("qa-coverage-*.txt"))
    assert not list(project.glob("qa-cov-attr-tests-*.txt"))
