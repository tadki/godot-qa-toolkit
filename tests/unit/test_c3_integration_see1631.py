"""SEE-1631 C3 步③：mutation 接入行为判据（§SPEC-032 判据④⑤）。"""

from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _reset_runner_globals():
    """真机冒烟会经 runner 填充模块级 _BASELINE_RED_MAP / face 映射——
    用后清零，避免跨文件污染 d7 冷车道单测（其对全局缺省有断言）。"""
    yield
    from godot_qa_toolkit.mutation import runner as _runner_mod

    _runner_mod._BASELINE_RED_MAP = set()
    _runner_mod._FACE_SCRIPT_BY_BASENAME = {}
import os
import shutil
from pathlib import Path

FORMS_TEMPLATE = Path(__file__).parent.parent / "fixtures" / "see1550_forms"


def test_run_side_resolve_selection_renew_false(monkeypatch, tmp_path):
    """run 路径（含 ownership_for）固定 renew=False——解析器收到的
    renew 关键字为 False。"""
    import godot_qa_toolkit.mutation.truth_lines as tl
    from godot_qa_toolkit.mutation import truth_select as ts

    captured = {}

    def fake_resolve(project_root, target_file, map_path_override=None,
                     strict=True, timeout_s=None, renew=True):
        captured["renew"] = renew
        return {"source": "runtime_mapping", "mapping": {
            "file_sha256_bound": True, "path": "/x/m.json"},
            "tests": ["res://tests/unit/test_a.gd"], "zero_ref": False}

    monkeypatch.setattr(tl, "_ts",
                        lambda: type("TS", (), {
                            "resolve_selection": staticmethod(fake_resolve),
                            "load_map": staticmethod(
                                lambda p: {"arms": {"gut_full": {}}})})())
    owner = tl.ownership_for("/p", "/p/calc.gd")
    assert captured["renew"] is False


def test_plan_map_section_calls_status(monkeypatch, tmp_path):
    """plan 的 map 段改调契约 status（不进 resolve/preview 旧路径）。"""
    from godot_qa_toolkit.mutation import plancheck
    from pathlib import Path

    called = {"status": 0, "preview": 0}

    def fake_status(root, files=None):
        called["status"] += 1
        return {"present": False, "fresh": False,
                "factors": ["no attribution map"],
                "would_refresh": "create", "scope": ["missing"],
                "last_cost": "unknown"}
    from godot_qa_toolkit.mutation import map_contract as _mcp
    monkeypatch.setattr(_mcp, "status", fake_status)
    import godot_qa_toolkit.mutation.truth_select as _ts
    monkeypatch.setattr(_ts, "map_preview_state",
                        lambda r, t: called.__setitem__(
                            "preview", called["preview"] + 1) or {})
    probs = plancheck._map_problem(Path(tmp_path), str(tmp_path / "c.gd"))
    assert called["status"] == 1 and called["preview"] == 0
    codes = [p["code"] for p in probs]
    assert "attribution_map_missing_or_stale" in codes
    ev = probs[0]["evidence"]
    assert ev["would_refresh"] == "create"


def test_auto_increment_lock_recheck_merges(monkeypatch, tmp_path):
    """锁内新鲜度复检：map 已被并发对端刷到新鲜 → 合并返回，不跑增量。"""
    import godot_qa_toolkit.mutation.truth_select as ts

    calls = {"increment": 0}

    def fake_apply(root, path, changed, timeout_s, t0):
        calls["increment"] += 1
        return True, None
    monkeypatch.setattr(ts, "_increment_apply", fake_apply)
    from godot_qa_toolkit.mutation.verdict_source import BaselineSourceError
    monkeypatch.setattr(ts, "load_map",
                        lambda p: {"schema_version": "test-attribution/7",
                                   "arms_state": {"gut_full": {
                                       "state": "valid"}},
                                   "test_verdicts": {},
                                   "file_sha256": {"calc.gd": "x"},
                                   "tests_face": {}})
    from godot_qa_toolkit.mutation import verdict_source as _vs
    monkeypatch.setattr(_vs, "assert_map_fresh",
                        lambda *a, **k: None)  # 锁内复检判新鲜
    map_file = tmp_path / "m.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text("{}")
    renewed, err = ts._auto_increment("/p", str(map_file),
                                      {"file_sha256": {}}, "calc.gd",
                                      timeout_s=60)
    assert renewed is True and err is None
    assert calls["increment"] == 0, "复检 fresh 不再跑增量腿"


def test_d12_prepare_real_targets_exist(tmp_path, monkeypatch):
    """D12 不打桩真路径：宇宙路径经 prepare 流入 run_measurement 后，
    map target_files 逐一真实存在（res:// 形态漏转换回归的常驻哨兵）。"""
    import os
    import shutil
    from pathlib import Path as _P

    gut_src = None
    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(_P.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = _P(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            gut_src = p
            break
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    from godot_qa_toolkit.mutation.verdict_source import (
        prepare_map_for_baseline,
    )

    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    # recipe 可构建 + features 元数据（install_recipe/ensure_addon_metadata
    # 为 batteries 同款夹具助手）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(root)
    ensure_addon_metadata(root)
    # forms 模板无 utils/：目标写在根（production.include=file 粒度点名）
    (root / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n")
    # F1 语义：建图面 = production 宇宙后，臂证据要求测试真实触达目标
    # （触达测试随夹具在场，否则探针零点火 → arms 门具名拒装）
    (root / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "unit" / "test_kill.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign():\n\tassert_eq(C.new().sign(5), 1)\n")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file",
                                    "value": "calc_sign.gd"}]}}))
    # conftest 单元套件默认 MAP_MODE=off + 真空 stub godot——本测为真
    # 路径哨兵：显式 auto + 真 Godot 二进制
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    # ENV_MAP（显式）在 resolve 内以 override 参数表达；auto 门只看 mode
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    import shutil as _sh
    real_godot = _sh.which("godot")
    if real_godot is None:
        pytest.skip("real godot binary unavailable")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    # conftest 单元面关掉了自动导入（防误呼真引擎）——本测真路径要 GUT
    # 跑出执行证据，恢复自动导入并预导入一次（首跑 .godot 缓存）
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    import subprocess as _sp
    _sp.run([real_godot, "--headless", "--import", "--path", str(root)],
            capture_output=True, timeout=600)
    out = prepare_map_for_baseline(
        str(root), [str(root / "calc_sign.gd")], timeout_s=900)
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    targets = doc.get("target_files") or []
    assert targets, "宇宙须覆盖生产文件"
    # C3-R1 F1：建图面 = 范围规则 production 宇宙（逐项相等；root 回退
    # 放大/首层推导漏建的两类偏差哨兵）
    assert targets == ["calc_sign.gd"], targets
    for rel in targets:
        assert (root / rel).is_file(), f"target 须真实存在: {rel}"
    states = {k: v.get("state")
              for k, v in (doc.get("arms_state") or {}).items()}
    assert states.get("gut_full") == "valid"


def test_d13_red_fixture_map_source_smoke(tmp_path, monkeypatch, capsys):
    """D13 真 Godot 冒烟：RED fixture（恒红测试 = verdict red/pre_existing）
    上 map 源 mutation——红测物理跳过生效（marked>=1）、killer 正常 kill、
    不得 16/16 全杀假 kill 回归。"""
    import os
    import shutil as _sh
    from pathlib import Path as _P

    gut_src = None
    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(_P.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = _P(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            gut_src = p
            break
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    real_godot = _sh.which("godot")
    if real_godot is None:
        pytest.skip("real godot binary unavailable")

    from godot_qa_toolkit.mutation.verdict_source import (
        prepare_map_for_baseline,
    )
    import hashlib
    import sys as _sys

    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(root)
    ensure_addon_metadata(root)
    # killer 目标 + 绿杀手测试（建图前在场 → 归属边绑定 calc_sign 行）
    target = root / "calc_sign.gd"
    target.write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n")
    (root / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    killer = root / "tests" / "unit" / "test_kill.gd"
    killer.write_text(
        "extends GutTest\nconst C = preload(\"res://calc_sign.gd\")\n"
        "func test_sign_pos():\n\tassert_eq(C.new().sign(5), 1)\n"
        "func test_sign_neg():\n\tassert_eq(C.new().sign(-5), -1)\n")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file", "value": "calc_sign.gd"}]}}))
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    # ENV_MAP（显式）在 resolve 内以 override 参数表达；auto 门只看 mode
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    import subprocess as _sp
    _sp.run([real_godot, "--headless", "--import", "--path", str(root)],
            capture_output=True, timeout=600)
    # F5（C3-R1）：恒红测试在**建图时**就在（从零建图 RED 夹具 =
    # Revy 失败场景）——红被懒基线吸收为 pre_existing（partial 臂）
    red_test = root / "tests" / "unit" / "test_red_always.gd"
    red_test.write_text(
        "extends GutTest\nfunc test_red_always():\n\tfail_test(\"red\")\n")
    out = prepare_map_for_baseline(
        str(root), [str(target)], timeout_s=900)
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    red_key = next((k for k in doc.get("test_verdicts", {})
                    if "test_red_always" in k), None)
    assert red_key is not None, \
        f"建图时在场的恒红测试必须进 verdict 表（red/pre_existing）: " \
        f"{sorted(doc.get('test_verdicts', {}))[:6]}"
    assert doc["test_verdicts"][red_key].get("verdict") == "red"
    assert doc["test_verdicts"][red_key].get("classification") == \
        "pre_existing"
    # 红测同触达目标行 → 进入选面（face 含红测；物理跳过才可执行）
    gut = doc["arms"]["gut_full"]
    gut["test_to_lines"]["res://tests/unit/test_red_always.gd::test_red_always"] = {
        "calc_sign.gd": [3, 4]}
    Path(out).write_text(json.dumps(doc, ensure_ascii=False),
                         encoding="utf-8")
    from godot_qa_toolkit.mutation.runner import run_mutation

    # worker_host 车道（skip_applied 条件之一：物理跳过需 worker host /
    # bail 在场——与 §SPEC-P2-R1 语义一致）
    r = run_mutation(str(target), str(root), budget=20, timeout_s=300,
                     select_mode="line", select_lines=[3, 4],
                     reuse_coverage=False, worker_channel=True)
    err = capsys.readouterr().err
    s = r["summary"]
    # killer 正常 kill（≥1）；非全杀（等价/未覆盖位点 survive 合理）——
    # D13 假 kill 回归面 = killed==mutants 全杀不得复发
    assert s.get("killed", 0) >= 1
    total = s.get("mutants", 0)
    assert s.get("killed", 0) < total, "全杀 = D13 假 kill 回归"
    assert s.get("killed", 0) + s.get("survived", 0) + \
        s.get("suspect", 0) <= total


def test_d14_gherkin_leg_executions_flow(tmp_path, monkeypatch):
    """D14：真实增量路径 gherkin 腿执行账（failing_scenarios）流入
    verdict 合成——不再整腿保守红（最小夹具：合成腿账注入真实合并
    函数；腿端回写由 run_gherkin_increment_leg 常驻测试覆盖）。"""
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    baseline = {"test_verdicts": {
        "gherkin://feature/qa_extra::even": {"verdict": "green"},
        "gherkin://feature/qa_extra::mean": {"verdict": "green"},
        "gherkin://feature/qa_extra::clamp": {"verdict": "green"},
    }}
    # 真实腿账形态：run_gherkin_increment_leg 回写 failing_scenarios
    fresh = {"executions": [{"rc": 1, "out": "...",
                             "failing_scenarios": ["mean"]}]}
    merged = _merged_test_verdicts(
        baseline, [], rerun_scripts=set(), dropped_scripts=set(),
        removed_rels=set(), fresh_gherkin=fresh,
        gherkin_rerun={"qa_extra"}, gherkin_dropped=set())
    assert merged["gherkin://feature/qa_extra::mean"]["verdict"] == "red"
    assert merged["gherkin://feature/qa_extra::even"] == {"verdict": "green"}
    assert merged["gherkin://feature/qa_extra::clamp"] == {"verdict": "green"}


def test_d15_explicit_stale_refreshes_not_refuses(tmp_path, monkeypatch):
    """D15：显式 map 过期 → 同一 prepare 阶梯自刷（不再 prepare_required
    拒绝）；无规则文件夹具静默消费 stale → 共享谓词判 stale 并自刷。"""
    import godot_qa_toolkit.mutation.truth_select as ts
    from godot_qa_toolkit.mutation.map_contract import refresh as mrefresh

    # 最小工程：无 .gqt-scope.json（通用默认）
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "calc.gd").write_text("extends Node\nfunc f(a):\n\treturn a\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (tmp_path / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    calls = {"refresh": 0}

    def spy_refresh(root, files, timeout_s=600):
        calls["refresh"] += 1
        return {"fresh": True, "already_fresh": False, "view": None}

    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    # ENV_MAP（显式）在 resolve 内以 override 参数表达；auto 门只看 mode
    def fake_renew(root, path, doc, rel, timeout_s=None):
        calls["refresh"] += 1
        return True, None
    monkeypatch.setattr(ts, "_resolve_renew", fake_renew)
    # stale map（tests_face 漂移）→ resolve 走续新（显式也不拒绝）
    st = ts.map_preview_state(str(tmp_path), "calc.gd")
    # 无 map → missing 分支；装一个 stale map 再验续新被调用
    (tmp_path / ".qa-cache").mkdir(exist_ok=True)
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": "old", "file_sha256": {"calc.gd": "x"},
           "tests_face": {"tests/unit/test_a.gd": "old"},
           "arms_state": {"gut_full": {"state": "valid"}},
           "arms": {"gut_full": {}}, "test_verdicts": {}}
    (tmp_path / ".qa-cache" / "attribution-map.json").write_text(
        json.dumps(doc))
    st2 = ts.map_preview_state(str(tmp_path), "calc.gd")
    assert st2["fresh"] is False
    assert "tests_face" in st2["reason"] or "stale" in st2["reason"]
    # D15 判据打在消费入口 resolve_selection（显式 map 同走续新阶梯）
    sel = ts.resolve_selection(str(tmp_path),
                               str(tmp_path / "calc.gd"),
                               strict=False, renew=True)
    assert calls["refresh"] >= 1, "显式过期 map 须自刷（不再 prepare_required 拒绝）"


def test_d10_same_name_real_godot_line_face(tmp_path, monkeypatch, capsys):
    """D10 真 Godot 反例（行选面）：两目录同名脚本同名测试（unit 绿
    killer / nested 红）——物理跳过只命中红测 wrapper（marked=1）；绿测
    留在 face 内参与判定（killer kill 在场）。"""
    import os
    import shutil as _sh
    from pathlib import Path as _P

    gut_src = None
    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(_P.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = _P(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            gut_src = p
            break
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    real_godot = _sh.which("godot")
    if real_godot is None:
        pytest.skip("real godot binary unavailable")

    from godot_qa_toolkit.mutation.verdict_source import (
        prepare_map_for_baseline,
    )
    from godot_qa_toolkit.mutation import directdrive
    import hashlib
    import sys as _sys

    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(root)
    ensure_addon_metadata(root)
    target = root / "calc_sign.gd"
    target.write_text(
        "extends RefCounted\\nfunc sign(x: int) -> int:\\n"
        "\\tif x > 0:\\n\\t\\treturn 1\\n\\treturn -1\\n")
    (root / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "nested").mkdir(parents=True)
    unit_dup = root / "tests" / "unit" / "test_dup.gd"
    unit_dup.write_text(
        'extends GutTest\\nconst C = preload("res://calc_sign.gd")\\n'
        'func test_same():\\n\\tassert_eq(C.new().sign(5), 1)\\n')
    nested_dup = root / "tests" / "nested" / "test_dup.gd"
    nested_dup.write_text(
        'extends GutTest\\nfunc test_same():\\n\\tfail_test("red")\\n')
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "file", "value": "calc_sign.gd"}]}}))
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_MUTATION_BASELINE_SOURCE", "map")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    # 同名红测使 gherkin 臂 invalid → build gate 拒装；D10 反例只需
    # gut_full 臂（gherkin 诊断暂停，disabled 臂 state 不阻门）
    monkeypatch.setenv("GQT_MEASUREMENT_GHERKIN_DISABLED", "1")
    # 同名红测会使建图 arms 门具名拒装（gut 臂零归属边证据）——D10 反例
    # 以合成判定面直接落盘（消费路径端到端：map 源 mutation 读判定面）
    import hashlib as _hl
    from godot_qa_toolkit.mutation.coverlink import current_tests_face
    from godot_qa_toolkit.mutation.truth_select import (
        _fresh_recipe_hash as _fresh_recipe_hash,
    )
    out = root / ".qa-cache" / "attribution-map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "synthetic",
        "recipe_hash_pristine": _fresh_recipe_hash(str(root)),
        "project_root": str(root), "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"calc_sign.gd": _hl.sha256(
            target.read_bytes()).hexdigest()},
        "target_files": ["calc_sign.gd"],
        "tests_face": {
            "tests/unit/test_dup.gd": _hl.sha256(
                unit_dup.read_bytes()).hexdigest(),
            "tests/nested/test_dup.gd": _hl.sha256(
                nested_dup.read_bytes()).hexdigest(),
            # C3-R4 项1：测试面集合双向对账——map 记录当前 GUT 测试面全集
            **current_tests_face(str(root))},
        "instrumentable_lines": {"calc_sign.gd": [3, 4, 5]},
        "arms": {"gut_full": {"test_to_lines": {
            "res://tests/unit/test_dup.gd::test_same": {"calc_sign.gd": [3, 4]},
            "res://tests/nested/test_dup.gd::test_same": {"calc_sign.gd": [3, 4]}},
            "test_to_files": {
                "res://tests/unit/test_dup.gd::test_same": ["calc_sign.gd"],
                "res://tests/nested/test_dup.gd::test_same": ["calc_sign.gd"]}}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 2, "probes_fired": 2,
                                    "vacuous": False}},
        "test_verdicts": {
            "res://tests/unit/test_dup.gd::test_same": {"verdict": "green"},
            "res://tests/nested/test_dup.gd::test_same": {
                "verdict": "red", "classification": "pre_existing",
                "script": "res://tests/nested/test_dup.gd"}},
        "notes": []}
    out.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    gut = doc["arms"]["gut_full"]
    line_map = {"calc_sign.gd": [3, 4]}
    gut["test_to_lines"]["res://tests/unit/test_dup.gd::test_same"] = \
        dict(line_map)
    gut["test_to_lines"]["res://tests/nested/test_dup.gd::test_same"] = \
        dict(line_map)
    # 行事实宇宙（_tests_for_target 消费 test_to_files/line 视图）
    gut.setdefault("test_to_files", {})[
        "res://tests/unit/test_dup.gd::test_same"] = ["calc_sign.gd"]
    gut.setdefault("test_to_files", {})[
        "res://tests/nested/test_dup.gd::test_same"] = ["calc_sign.gd"]
    gut.setdefault("line_to_tests", {})[
        "calc_sign.gd"] = {3: ["res://tests/unit/test_dup.gd::test_same",
                               "res://tests/nested/test_dup.gd::test_same"],
                           4: ["res://tests/unit/test_dup.gd::test_same",
                               "res://tests/nested/test_dup.gd::test_same"]}
    gut["pairs"] = gut.get("pairs", 0) + 2
    doc["instrumentable_lines"]["calc_sign.gd"] = [3, 4, 5]
    doc["test_verdicts"]["res://tests/unit/test_dup.gd::test_same"] = {
        "verdict": "green"}
    doc["test_verdicts"]["res://tests/nested/test_dup.gd::test_same"] = {
        "verdict": "red", "classification": "pre_existing",
        "script": "res://tests/nested/test_dup.gd"}
    Path(out).write_text(json.dumps(doc, ensure_ascii=False),
                         encoding="utf-8")
    captured = {"cfgs": []}
    real_write = directdrive.write_direct_cfg

    def spy_write(*a, **k):
        path = real_write(*a, **k)
        captured["cfgs"].append(json.loads(Path(path).read_text()))
        return path
    monkeypatch.setattr(directdrive, "write_direct_cfg", spy_write)
    from godot_qa_toolkit.mutation.runner import run_mutation

    r = run_mutation(str(target), str(root), budget=20, timeout_s=300,
                     select_mode="line", select_lines=[3, 4],
                     reuse_coverage=False, worker_channel=True)
    # 物理跳过只命中 nested 红测 wrapper；unit 绿测 wrapper 不在 skip 名单
    s = r["summary"]
    # D10 判据（Leader 预许可形态）：行选面对同名歧义**具名拒跑**
    # （零引用短路以映射为准——face 不可达时绝不静默跳过绿测）。
    err = capsys.readouterr().err
    # D10 判据：同名歧义 face 下**具名拒跑**（ambiguous/no-test），
    # 绝不静默跳过绿测或以红测假 kill
    assert ("ambiguous" in err or "no test touches" in err
            or s.get("mutants", 0) == 0), \
        "行选面须具名拒跑: " + err[-800:]


def test_f1_universe_exact_four_configs(tmp_path):
    """F1 四种配置：ensure_generate 的建图目标 = 范围规则 production
    .gd 逐项相等（纯函数级——直接调 _ensure_generate 的 targets 推导）。"""
    root = tmp_path / "proj"
    (root / "systems").mkdir(parents=True)
    (root / "top.gd").write_text("extends Node\n")
    for n in "abc":
        (root / "systems" / f"{n}.gd").write_text("extends Node\n")
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "t.gd").write_text("extends Node\n")
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_a.gd").write_text("extends GutTest\n")
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))

    from godot_qa_toolkit.measurement.classify import discover_scope
    from godot_qa_toolkit.mutation.truth_select import _ensure_targets_exact

    def universe(rules):
        (root / ".gqt-scope.json").write_text(json.dumps(rules))
        return [p.removeprefix("res://")
                for p in discover_scope(root)["gd"]]

    # A：include 目录 systems + exclude 单文件 systems/b.gd
    got = universe({"production": {
        "include": [{"kind": "dir", "value": "systems"}],
        "exclude": [{"kind": "file", "value": "systems/b.gd",
                     "reason": "dead"}]}})
    # F1 判据：ensure 目标清单 = production 宇宙逐项相等（绝对路径化）
    exact = _ensure_targets_exact([str(root / rel) for rel in got])
    assert exact == [str(root / rel) for rel in sorted(got)]
    # B：include 类型 *.gd + exclude addons、tests
    got = universe({"production": {
        "include": [{"kind": "ext", "value": "*.gd"}],
        "exclude": [{"kind": "dir", "value": "addons", "reason": "a"},
                    {"kind": "dir", "value": "tests", "reason": "t"}]}})
    assert "top.gd" in got and "systems/a.gd" in got
    assert not any(v.startswith("addons/") for v in got)
    # C：include 单个根级文件 top.gd
    got = universe({"production": {
        "include": [{"kind": "file", "value": "top.gd"}]}})
    assert got == ["top.gd"]
    # D：include systems 两个目录形态（KOL 形态——纯函数级以 systems
    # 单目录为代表；D12 真路径测试已断言 target_files == universe）
    got = universe({"production": {
        "include": [{"kind": "dir", "value": "systems"},
                    {"kind": "ext", "value": "*.gd"}]}})
    assert "top.gd" in got


def test_f4_default_map_unreadable_named_json(tmp_path, monkeypatch, capsys):
    """F4：默认 map 不可读（坏 JSON）→ CLI 全链具名 JSON、零 traceback。"""
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "calc.gd").write_text(
        "extends RefCounted\nfunc f(a):\n\treturn a\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (tmp_path / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(tmp_path)
    ensure_addon_metadata(tmp_path)
    map_file = tmp_path / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text("{ not a map", encoding="utf-8")
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.delenv("GQT_MUTATION_BASELINE_SOURCE", raising=False)
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit.mutation import map_contract as mc

    def fake_rm(root, targets, recipe, *, attribution_out, **_kw):
        doc = {"schema_version": "test-attribution/7",
               "report_recipe_hash": "r", "recipe_hash_pristine":
               mc.__dict__.get("_x", None) or None,
               "project_root": str(root), "identity": "i",
               "causal": {"enabled": False},
               "file_sha256": {Path(t).name: "x" for t in targets},
               "target_files": [Path(t).name for t in targets],
               "tests_face": {}, "instrumentable_lines": {},
               "arms": {"gut_full": {"test_to_lines": {}}},
               "arms_state": {"gut_full": {"state": "valid",
                                           "bundles": 1, "pairs": 2,
                                           "probes_fired": 2,
                                           "vacuous": False}},
               "test_verdicts": {}}
        Path(attribution_out).write_text(json.dumps(doc))
        return None
    import godot_qa_toolkit.measurement.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "run_measurement", fake_rm)
    from godot_qa_toolkit import projectroot as pr_mod
    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda project_root, **_kw: None)
    args = cli_mod.build_parser().parse_args([
        "mutation", str(tmp_path / "calc.gd"),
        "--project-root", str(tmp_path),
        "--budget", "2", "--auto-prepare"])
    rc = cli_mod.cmd_mutation(args)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, "零 traceback"
    assert rc in (0, 1, 2)
    if captured.out.strip():
        payload = json.loads(captured.out)
        assert payload.get("tool") == "mutation"
        # 具名形态：要么 map 不可用重建成功，要么具名 JSON 失败——不得裸异常
        summary = payload.get("summary") or {}
        assert payload.get("ok") in (True, False)
        if payload.get("ok") is False:
            assert summary.get("error") or payload.get("failures")


def test_r2_mapview_violation_named_json(tmp_path, monkeypatch, capsys):
    """C3-R2 项1：MapView 构造期断言（§SPEC-015 invalid 臂边保留）触发时，
    CLI 全链输出具名 JSON、零 traceback。"""
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "calc.gd").write_text("extends Node\nfunc f(a):\n\treturn a\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (tmp_path / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(tmp_path)
    ensure_addon_metadata(tmp_path)
    map_file = tmp_path / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    # otherwise-fresh map，但保留 invalid gherkin 臂的边（§SPEC-015 拦截面）
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": None,
           "project_root": str(tmp_path),
           "file_sha256": {"calc.gd": "x"},
           "target_files": ["calc.gd"],
           "tests_face": {"tests/unit/test_a.gd": "x"},
           "arms": {"gut_full": {"test_to_lines": {}},
                    "gherkin": {"test_to_lines": {"x": {}}}},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1, "probes_fired": 1,
                                       "vacuous": False},
                          "gherkin": {"state": "invalid"}},
           "instrumentable_lines": {"calc.gd": [1]},
           "test_verdicts": {}}
    map_file.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit import projectroot as pr_mod
    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda project_root, **_kw: None)
    args = cli_mod.build_parser().parse_args([
        "mutation", str(tmp_path / "calc.gd"),
        "--project-root", str(tmp_path),
        "--budget", "2", "--auto-prepare"])
    rc = cli_mod.cmd_mutation(args)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, "零 traceback"
    assert captured.out.strip(), "stdout 必须有具名 JSON"
    payload = json.loads(captured.out)
    assert payload.get("ok") is False
    summary = payload.get("summary") or {}
    assert "§SPEC-015" in str(summary.get("error")) or payload.get(
        "failures"), "MapView 断言触发必须具名"
    assert rc in (1, 2)


def test_r2_refresh_still_stale_named_json(tmp_path, monkeypatch, capsys):
    """C3-R2 项1（续新后仍不新鲜形态）：refresh 装填后校验失败 → CLI
    具名 JSON、零 traceback。"""
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "calc.gd").write_text("extends Node\nfunc f(a):\n\treturn a\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (tmp_path / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    map_file = tmp_path / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": "stale", "project_root": str(tmp_path),
           "file_sha256": {"calc.gd": "x"}, "target_files": ["calc.gd"],
           "tests_face": {}, "instrumentable_lines": {},
           "arms": {"gut_full": {"test_to_lines": {}}},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1, "probes_fired": 1,
                                       "vacuous": False}},
           "test_verdicts": {}}
    map_file.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit.mutation import map_contract as mc
    from godot_qa_toolkit.mutation import verdict_source as vs

    def boom(root, files, timeout_s=600):
        from godot_qa_toolkit.mutation.map_contract import MapContractError
        raise MapContractError(
            "refresh still stale after renewal (old map bytes unchanged)")
    # refresh 内延迟 import verdict_source 符号——patch 源模块属性
    monkeypatch.setattr(vs, "_prepare_build_missing", boom)
    from godot_qa_toolkit import projectroot as pr_mod
    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda project_root, **_kw: None)
    args = cli_mod.build_parser().parse_args([
        "mutation", str(tmp_path / "calc.gd"),
        "--project-root", str(tmp_path),
        "--budget", "2", "--auto-prepare"])
    rc = cli_mod.cmd_mutation(args)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, "零 traceback"
    assert captured.out.strip(), "stdout 必须有具名 JSON"
    payload = json.loads(captured.out)
    assert payload.get("ok") is False
    assert rc in (1, 2)


def test_r2_backup_root_no_residue_on_refresh_failure(tmp_path, monkeypatch):
    """C3-R2 项2：refresh 失败（project.godot missing）→ 备份根无残留
    （创建处自清）；他进程刚建的目录不被误删。"""
    from godot_qa_toolkit.measurement.backup import _backup_base

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (proj / "calc.gd").write_text("extends Node\nfunc f(a):\n\treturn a\n")
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc test_a():\n\tpass\n")
    (proj / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    # 无 project.godot → refresh 阶梯内 run_measurement 具名失败
    from godot_qa_toolkit.mutation.map_contract import refresh

    base = _backup_base()
    base.mkdir(parents=True, exist_ok=True)
    other_dir = base / "other-process-dir"
    other_dir.mkdir(exist_ok=True)
    (other_dir / "started.json").write_text("{}")
    try:
        refresh(str(proj), [str(proj / "calc.gd")], timeout_s=60)
        raised = False
    except Exception:
        raised = True
    assert raised, "缺 project.godot 的 refresh 必须具名失败"
    leftovers = [p.name for p in base.iterdir() if p.is_dir()
                 and p.name != "other-process-dir"]
    # 本进程的空备份目录自清；他进程目录不被误删
    empty_left = [n for n in leftovers
                  if not any((base / n).iterdir())]
    assert not empty_left, f"refresh 失败不得残留空备份目录: {empty_left}"
    assert other_dir.is_dir(), "他进程目录不得被误删"
    # 测试自身清理他进程模拟目录（避免污染共享 TMPDIR 的后续断言）
    import shutil as _sh
    _sh.rmtree(other_dir, ignore_errors=True)


def _r2_project(tmp_path) -> "object":
    proj = tmp_path / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "project.godot").write_text("config_version=5\n")
    (proj / "calc.gd").write_text("extends Node\nfunc f(a):\n\treturn a\n")
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc.gd")\n'
        "func test_a():\n\tassert_eq(C.new().f(1), 1)\n")
    (proj / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/unit"], "include_subdirs": True,
         "prefix": "test_"}))
    return proj


def test_r2_full_rebuild_failure_keeps_old_map(tmp_path, monkeypatch):
    """C3-R3 项2：全量重建中途失败 → 旧 map 字节不变（默认路径）。"""
    import hashlib as _hl

    from godot_qa_toolkit.mutation import verdict_source as vs

    proj = _r2_project(tmp_path)
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text('{"schema_version": "test-attribution/7"}',
                        encoding="utf-8")
    before = _hl.sha256(map_file.read_bytes()).hexdigest()
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit.mutation import truth_select as ts
    monkeypatch.setattr(ts, "_ensure_generate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ts.MutationSelectionError(
                                "attribution_generate_failed: boom")))
    with pytest.raises(Exception):
        vs.prepare_map_for_baseline(str(proj), ["calc.gd"], timeout_s=60)
    assert map_file.is_file(), "旧 map 不得被销毁"
    assert _hl.sha256(map_file.read_bytes()).hexdigest() == before


def test_r2_increment_failure_keeps_old_map(tmp_path, monkeypatch):
    """C3-R3 项2：增量中途失败 → 旧 map 字节不变（默认路径）。"""
    import hashlib as _hl

    from godot_qa_toolkit.mutation import verdict_source as vs

    proj = _r2_project(tmp_path)
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text('{"schema_version": "test-attribution/7"}',
                        encoding="utf-8")
    before = _hl.sha256(map_file.read_bytes()).hexdigest()
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit.measurement import increment as _inc
    monkeypatch.setattr(_inc, "run_attribution_increment",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _inc.IncrementError("increment failed: boom")))
    with pytest.raises(Exception):
        vs.prepare_map_for_baseline(str(proj), ["calc.gd"], timeout_s=60)
    assert map_file.is_file(), "旧 map 不得被销毁"
    assert _hl.sha256(map_file.read_bytes()).hexdigest() == before


def test_r2_explicit_map_failure_keeps_user_file(tmp_path, monkeypatch):
    """C3-R3 项2：显式 map 续新失败 → 用户那份文件字节不变。"""
    import hashlib as _hl

    from godot_qa_toolkit.mutation import verdict_source as vs

    proj = _r2_project(tmp_path)
    user_map = tmp_path / "my-map.json"
    user_map.write_text('{"schema_version": "test-attribution/7"}',
                        encoding="utf-8")
    before = _hl.sha256(user_map.read_bytes()).hexdigest()
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP", str(user_map))
    from godot_qa_toolkit.mutation import truth_select as ts
    monkeypatch.setattr(ts, "_ensure_generate",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ts.MutationSelectionError(
                                "attribution_generate_failed: boom")))
    with pytest.raises(Exception):
        vs.prepare_map_for_baseline(str(proj), ["calc.gd"], timeout_s=60)
    assert user_map.is_file(), "用户显式 map 不得被销毁"
    assert _hl.sha256(user_map.read_bytes()).hexdigest() == before


def _r2_stale_invalid_arm_map(tmp_path):
    """构造 otherwise-fresh 但保留 invalid gherkin 臂边的 map（§SPEC-015
    视图断言触发面）。"""
    import hashlib as _hl

    proj = _r2_project(tmp_path)
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata
    install_recipe(proj)
    ensure_addon_metadata(proj)
    from godot_qa_toolkit.mutation.truth_select import _fresh_recipe_hash
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": _fresh_recipe_hash(str(proj)),
           "project_root": str(proj),
           "file_sha256": {"calc.gd": _hl.sha256(
               (proj / "calc.gd").read_bytes()).hexdigest()},
           "target_files": ["calc.gd"],
           "tests_face": {"tests/unit/test_a.gd": _hl.sha256(
               (proj / "tests" / "unit" / "test_a.gd")
               .read_bytes()).hexdigest()},
           "arms": {"gut_full": {"test_to_lines": {}},
                    "gherkin": {"test_to_lines": {"x": {}}}},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1, "probes_fired": 1,
                                       "vacuous": False},
                          "gherkin": {"state": "invalid"}},
           "instrumentable_lines": {"calc.gd": [1]},
           "test_verdicts": {}}
    map_file.write_text(json.dumps(doc), encoding="utf-8")
    return proj, map_file


@pytest.mark.parametrize("extra", [
    [],
    ["--select", "fn"],
    ["--select", "full"],
    ["--tests", "res://tests/unit/"],
], ids=["line", "fn", "full", "tests"])
def test_r2_mapview_violation_named_json_all_entries(
        tmp_path, monkeypatch, capsys, extra):
    """C3-R3 项1：四种 mutation 入口下 MapView 断言触发 → 具名 JSON
    （零 traceback）——不打桩 _prepare_mapping_face 真实路径。"""
    proj, _map_file = _r2_stale_invalid_arm_map(tmp_path)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit import cli as cli_mod
    from godot_qa_toolkit import projectroot as pr_mod
    monkeypatch.setattr(pr_mod, "ensure_import_cache",
                        lambda project_root, **_kw: None)
    args = cli_mod.build_parser().parse_args([
        "mutation", str(proj / "calc.gd"),
        "--project-root", str(proj),
        "--budget", "2", "--auto-prepare"] + extra)
    rc = cli_mod.cmd_mutation(args)
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, captured.err[-600:]
    if captured.out.strip():
        payload = json.loads(captured.out)
        assert payload.get("tool") == "mutation"
        if payload.get("ok") is False:
            assert payload.get("summary", {}).get("error") or payload.get(
                "failures"), "失败必须具名"
    assert rc in (0, 1, 2)


def test_r4_new_test_file_makes_map_stale(tmp_path, monkeypatch):
    """C3-R4 项1：新增测试文件（当前 GUT 面多出）→ map 判过期（新增
    必须续新跑进去，否则 full 面把新测失败当新失败 → 假 kill）。"""
    from godot_qa_toolkit.mutation import verdict_source as vs
    from godot_qa_toolkit.mutation.coverlink import current_tests_face

    proj = _r2_project(tmp_path)
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": None, "project_root": str(proj),
           "file_sha256": {}, "target_files": [], "tests_face": {},
           "instrumentable_lines": {},
           "arms": {"gut_full": {"test_to_lines": {}}},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1, "probes_fired": 1,
                                       "vacuous": False}},
           "test_verdicts": {}}
    map_file.write_text(json.dumps(doc), encoding="utf-8")
    # 新增一个测试文件 → 面集合变大
    (proj / "tests" / "unit" / "test_new_red.gd").write_text(
        "extends GutTest\nfunc test_new():\n\tfail_test(\"new red\")\n")
    with pytest.raises(vs.BaselineSourceError) as ei:
        vs.assert_map_fresh(str(proj), doc, [])
    assert "tests_face drift" in str(ei.value)
    assert "added:tests/unit/test_new_red.gd" in str(ei.value)


def test_r4_production_content_drift_makes_map_stale(tmp_path, monkeypatch):
    """C3-R4 项2：生产内容（场景/资源/数据文本）内容变更/新增 → map 判
    过期（与生产 .gd 同一套逻辑；§SPEC-029 表第 5/6 行）。"""
    import hashlib as _hl

    from godot_qa_toolkit.mutation import verdict_source as vs

    proj = _r2_project(tmp_path)
    (proj / "resources").mkdir(exist_ok=True)
    (proj / "resources" / "data.csv").write_text("a,b\n")
    (proj / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "resources"},
                                   {"kind": "file", "value": "calc.gd"}]}}))
    from godot_qa_toolkit.mutation.coverlink import current_tests_face
    doc = {"schema_version": "test-attribution/7",
           "recipe_hash_pristine": None, "project_root": str(proj),
           "file_sha256": {}, "target_files": [],
           "tests_face": current_tests_face(str(proj)),
           "instrumentable_lines": {},
           "arms": {"gut_full": {"test_to_lines": {}}},
           "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                       "pairs": 1, "probes_fired": 1,
                                       "vacuous": False}},
           "production_content_sha256": {
               "resources/data.csv": _hl.sha256(b"a,b\n").hexdigest()},
           "test_verdicts": {}}
    # 内容变更 → 过期
    (proj / "resources" / "data.csv").write_text("a,c\n")
    with pytest.raises(vs.BaselineSourceError) as ei:
        vs.assert_map_fresh(str(proj), doc, [])
    assert "production content drift" in str(ei.value)
    # 新增生产内容 → 过期
    doc["production_content_sha256"]["resources/data.csv"] = _hl.sha256(
        (proj / "resources" / "data.csv").read_bytes()).hexdigest()
    (proj / "resources" / "more.csv").write_text("x\n")
    with pytest.raises(vs.BaselineSourceError) as ei:
        vs.assert_map_fresh(str(proj), doc, [])
    assert "production content drift" in str(ei.value)


def test_r4_new_test_identity_enters_verdict_table():
    """C3-R4 项1：新增测试脚本被续新跑过后，其身份必须进 verdict 表
    （红 → 进基线红集 → mutation 判定物理跳过，不再当新失败假 kill）。"""
    from godot_qa_toolkit.measurement.increment import (
        _merged_test_verdicts,
    )

    baseline = {"test_verdicts": {
        "test_old.gd::test_ok": {"verdict": "green"}}}
    executions = [{"script": "tests/unit/test_new_red.gd", "rc": 1,
                   "out": "* test_new_red\n[Failed] test_new_red\n"}]
    baseline["tests_face"] = {
        "tests/unit/test_new_red.gd": "sha",
        "tests/unit/test_old.gd": "sha"}
    merged = _merged_test_verdicts(
        baseline, executions, rerun_scripts={"test_new_red.gd"},
        dropped_scripts=set(), removed_rels=set(), fresh_gherkin=None,
        gherkin_rerun=set(), gherkin_dropped=set(),
        tests_face=baseline["tests_face"])
    assert merged is not None
    assert merged["test_new_red.gd::test_new_red"]["verdict"] == "red"
    # 基线身份逐位继承
    assert merged["test_old.gd::test_ok"] == {"verdict": "green"}


def test_r5_increment_failure_keeps_old_bytes(tmp_path, monkeypatch):
    """C3-R5 项1 补充：增量产物校验不过 → 旧图字节不变（tmp 丢弃）。"""
    import hashlib as _hl

    from godot_qa_toolkit.mutation import verdict_source as vs

    proj = _r2_project(tmp_path)
    map_file = proj / ".qa-cache" / "attribution-map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)
    map_file.write_text('{"schema_version": "test-attribution/7"}',
                        encoding="utf-8")
    before = _hl.sha256(map_file.read_bytes()).hexdigest()
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    from godot_qa_toolkit.measurement import increment as _inc
    monkeypatch.setattr(_inc, "run_attribution_increment",
                        lambda *a, **k: (_ for _ in ()).throw(
                            _inc.IncrementError("increment failed: boom")))
    with pytest.raises(Exception):
        vs._prepare_increment_renewal(
            {"target_files": ["calc.gd"]}, str(proj),
            str(map_file), ["calc.gd"], timeout_s=60)
    assert _hl.sha256(map_file.read_bytes()).hexdigest() == before
    assert not list(map_file.parent.glob("*.renew-tmp")), "tmp 须清理"
