"""§SPEC-003：四级 beacon 注入可达性 oracle 单元测试。

- 磁盘读回哈希不符 → run_error；load-null/parse → invalid_mutant；
- autoload 表内目标实例脚本与按路径加载资源不同源 → run_error；
- 无 GUT 运行流不得 killed/survived；
- beacon static init 只执行一次（模板级钉：唯一一次 load target + L3 复用
  同一资源对象）；
- main-loop 等待段保留且被测试钉住（缺失时 rc=124 挂起——Archi 探针实证）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import beacon, runner as mrunner
from godot_qa_toolkit.mutation.runner import run_mutation


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    return proj, target


class TestBeaconClassification:
    """beacon 失败分类矩阵（Python 侧消费契约）。"""

    def test_load_null_rc3_is_invalid_mutant(self):
        tail = "--- gqt: stderr ---\nGQT_BEACON_FAIL: mutant failed to " \
               "compile/load: res://calc.gd"
        assert beacon.classify_beacon_failure(3, tail) == "invalid_mutant"

    def test_hash_mismatch_rc4_is_run_error(self):
        tail = "--- gqt: stderr ---\nGQT_BEACON_FAIL: disk hash mismatch " \
               "for res://calc.gd"
        assert beacon.classify_beacon_failure(4, tail) == "run_error"

    def test_cfg_missing_rc4_is_run_error(self):
        tail = "GQT_BEACON_FAIL: GQT_MUTATION_CFG env missing"
        assert beacon.classify_beacon_failure(4, tail) == "run_error"

    def test_marker_absent_returns_none(self):
        assert beacon.classify_beacon_failure(3, "Parse Error: whatever") is None
        assert beacon.classify_beacon_failure(0, "* test_x\n[passed]") is None

    def test_nonzero_without_marker_is_not_beacon_failure(self):
        assert beacon.classify_beacon_failure(1, "SCRIPT ERROR: boom") is None


class TestBeaconEndToEndWiring:
    """端到端（fake GUT）：beacon 失败档进 verdict 的通路。"""

    def test_hash_mismatch_becomes_run_error_not_survived(self, tmp_path,
                                                          monkeypatch):
        proj, target = _write_project(tmp_path)

        def fake(root, timeout_s=60, tests_glob=None):
            return (4, "GQT_BEACON_FAIL: disk hash mismatch for res://calc.gd")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        s = r["summary"]
        assert s["survived"] == 0 and s["run_errors"] == s["mutants"] == 2
        assert r["ok"] is False

    def test_load_null_becomes_invalid_mutant(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)

        def fake(root, timeout_s=60, tests_glob=None):
            return (3, "GQT_BEACON_FAIL: mutant failed to compile/load")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        # 口径注记：mutants 计数沿用既有 _tally 五档（runtime invalid 走
        # invalid_mutants 独立键）；invalid_mutant 属操作符 bug 档、不进
        # ok 门禁（§SPEC-017 既有语义）——绝不折算成 survived。
        assert r["summary"]["invalid_mutants"] == 2
        assert r["summary"]["survived"] == 0

    def test_no_gut_evidence_blocks_survived(self, tmp_path, monkeypatch):
        """L4：rc=0 但输出无任何 GUT 运行流证据 → run_error（不得 survived）。"""
        proj, target = _write_project(tmp_path)

        def fake(root, timeout_s=60, tests_glob=None):
            return (0, "everything looks fine, no test markers here")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        s = r["summary"]
        assert s["survived"] == 0
        assert s["run_errors"] == s["mutants"] == 2

    def test_no_gut_evidence_blocks_killed(self, tmp_path, monkeypatch):
        """L4：rc=1 有失败行但无运行流标记（截断窗口形态）→ run_error。"""
        proj, target = _write_project(tmp_path)
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "* test_base\n  [passed]")
            # 无 `* test_` / `[Failed]` token——kill 判据可见的流缺失
            return (1, "SCRIPT ERROR: mutant crashed the engine")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = run_mutation(str(target), str(proj), budget=2)
        s = r["summary"]
        assert s["killed"] == 0
        assert s["run_errors"] == 2


class TestBeaconTemplatePins:
    """宿主脚本模板硬约束（终案原文钉死，防回归删改）。"""

    def test_main_loop_wait_section_present(self):
        """main-loop 等待段保留——缺失时 rc=124 挂起（Archi 探针实证）。"""
        assert "Engine.get_main_loop() == null" in beacon.BEACON_GD_TEMPLATE
        assert "create_timer" in beacon.BEACON_GD_TEMPLATE
        assert "max_iter" in beacon.BEACON_GD_TEMPLATE

    def test_static_init_executes_once(self):
        """beacon load() 触发的 static init 只执行一次：模板内对目标的
        load 恰好一次（L2），L3 复用同一 `script` 变量不二次 load。"""
        tpl = beacon.BEACON_GD_TEMPLATE
        assert tpl.count('load(target_res)') == 1
        # L3 比较复用同一资源对象（资源缓存同源断言）
        assert "inst.get_script() != script" in tpl

    def test_four_levels_present_in_order(self):
        tpl = beacon.BEACON_GD_TEMPLATE
        i_hash = tpl.index("disk hash mismatch")
        i_load = tpl.index("mutant failed to compile/load")
        i_auto = tpl.index("autoload instance not mutant-sourced")
        assert i_hash < i_load < i_auto, "L1 磁盘哈希 → L2 load → L3 autoload"

    def test_failure_codes_distinct(self):
        assert beacon.BEACON_INVALID_RC == 3
        assert beacon.BEACON_RUN_ERROR_RC == 4
        # 模板内退出码使用与契约一致
        assert beacon.BEACON_GD_TEMPLATE.count("quit(3)") >= 1
        assert beacon.BEACON_GD_TEMPLATE.count("quit(4)") >= 1

    def test_gut_cli_tail_preserved(self):
        """除 beacon 外与官方入口逐行同构：gut_cli.main 启动序列保留。"""
        assert "res://addons/gut/cli/gut_cli.gd" in beacon.BEACON_GD_TEMPLATE
        assert "cli.main()" in beacon.BEACON_GD_TEMPLATE

    def test_cfg_payload_shape(self, tmp_path):
        slot = tmp_path / "slot"
        slot.mkdir()
        cfg = beacon.write_beacon_cfg(str(slot), "res://calc.gd",
                                      "abc123", "SaveManager")
        import json
        payload = json.loads(cfg.read_text(encoding="utf-8"))
        assert payload == {"target_res": "res://calc.gd",
                           "mutant_sha256": "abc123",
                           "autoload": "SaveManager"}

    def test_beacon_script_per_pid_named(self, tmp_path):
        slot = tmp_path / "slot"
        slot.mkdir()
        p1 = beacon.seed_beacon_script(str(slot))
        assert ".gqt_beacon_host_" in p1.name
        assert p1.read_text(encoding="utf-8") == beacon.BEACON_GD_TEMPLATE


class TestAutoloadWiring:
    def test_autoload_map_parses_project_godot(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.godot").write_text(
            'config_version=5\n\n[application]\n'
            'run/main_scene="res://scene.tscn"\n\n'
            '[autoload]\n'
            'SaveManager="*res://autoload/save_manager.gd"\n'
            'Events="res://autoload/events.gd"\n\n'
            '[display]\n')
        amap = beacon.autoload_map(str(proj))
        assert amap == {"res://autoload/save_manager.gd": "SaveManager",
                        "res://autoload/events.gd": "Events"}

    def test_autoload_name_for_target(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.godot").write_text(
            '[autoload]\nSaveManager="*res://autoload/save_manager.gd"\n')
        assert beacon.autoload_name_for(
            str(proj), "res://autoload/save_manager.gd") == "SaveManager"
        assert beacon.autoload_name_for(
            str(proj), "res://systems/other.gd") == ""

    def test_no_project_godot_returns_empty(self, tmp_path):
        assert beacon.autoload_map(str(tmp_path)) == {}

    def test_non_autoload_section_res_entries_not_parsed(self, tmp_path):
        """[autoload] 外段落的 ``key="res://…"`` 行不得进 autoload 表
        （误入 → L3 对非 autoload 目标错误发起同源断言 → run_error 风暴）。"""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "project.godot").write_text(
            'config_version=5\n\n[application]\n'
            'boot_splash="res://ui/splash.png"\n'
            'main_scene="res://main.tscn"\n\n'
            '[autoload]\nEvents="*res://autoload/events.gd"\n\n'
            '[editor]\nplugin="res://addons/tool/plugin.gd"\n')
        assert beacon.autoload_map(str(proj)) == {
            "res://autoload/events.gd": "Events"}
        assert beacon.autoload_name_for(
            str(proj), "res://ui/splash.png") == ""
