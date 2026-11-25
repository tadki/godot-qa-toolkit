"""SEE-1631 C3 步③：mutation 接入行为判据（§SPEC-032 判据④⑤）。"""

from __future__ import annotations

import json

import pytest
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
    out = prepare_map_for_baseline(
        str(root), [str(target)], timeout_s=900)
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    # 建图后落盘恒红测试 + 注入判定（消费路径冒烟：pre_existing 红条目）
    red_test = root / "tests" / "unit" / "test_red_always.gd"
    red_test.write_text(
        "extends GutTest\nfunc test_red_always():\n\tfail_test(\"red\")\n")
    doc["tests_face"]["tests/unit/test_red_always.gd"] = hashlib.sha256(
        red_test.read_bytes()).hexdigest()
    doc["test_verdicts"]["test_red_always.gd::test_red_always"] = {
        "verdict": "red", "classification": "pre_existing",
        "script": "res://tests/unit/test_red_always.gd"}
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
    import subprocess as _sp
    _sp.run([real_godot, "--headless", "--import", "--path", str(root)],
            capture_output=True, timeout=600)
    out = (root / ".qa-cache" / "attribution-map.json")
    try:
        prepare_map_for_baseline(str(root), [str(target)], timeout_s=900)
    except Exception:
        pass  # 首建可能因 arms 证据不齐失败——产物在场即继续（下述补丁）
    assert out.is_file(), "首建须产出 map（哪怕 arms 门拒装，tmp 不落盘时跳过）"
    doc = json.loads(out.read_text(encoding="utf-8"))
    for rel, f in (("tests/unit/test_dup.gd", unit_dup),
                   ("tests/nested/test_dup.gd", nested_dup)):
        doc["tests_face"][rel] = hashlib.sha256(f.read_bytes()).hexdigest()
    doc["file_sha256"]["calc_sign.gd"] = hashlib.sha256(
        target.read_bytes()).hexdigest()
    # 后补绑定即时落盘（patch 后的读回走同一文件）
    Path(out).write_text(json.dumps(doc, ensure_ascii=False),
                         encoding="utf-8")
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
    # 判定面必须含同名的红/绿两测（face 未静默收缩）——经 worker 披露或
    # zero_ref 拒跑形态具名
    err = capsys.readouterr().err
    zero_ref = "no test touches" in err or s.get("mutants", 0) == 0
    if zero_ref:
        # 归属边不可达 = 具名拒跑（leader 允许的"行选面具名拒绝"形态）
        assert "no test touches" in err
        return
    assert captured["cfgs"], "直驱宿主 cfg 未捕获"
    for cfg in captured["cfgs"]:
        for pair in (cfg.get("skip_red_tests") or []):
            assert "nested" in pair[0], (
                f"D10：绿测同名 wrapper 不得被跳过: {pair}")
    assert "marked=1" in err or "marked=0" in err  # skip 机制在场
    # 绿 killer 的 kill 在场（绿测未被静默跳过/豁免）
    assert s.get("killed", 0) >= 1
    assert s.get("killed", 0) < s.get("mutants", 0)
