"""SEE-1589 hardener（⑤）：将本轮 A/B 改动面的语义变异击杀测试补齐。

只增强测试判别力，不改产品代码。每个用例瞄准一个已被变异注入证明
「现有测试抓不到」的语义点（见 hardener 报告的注入—击杀账本）：

- C2：autoload 裸名只应对**真实标识符**成边；注释/字符串里的名字不是
  compile-time 引用边（此前只有 class_name 侧有对应断言）。
- P2：`.gqt*` 自生成顶层条目（beacon 宿主 / worker 会话 / 选测物化目录）
  不得计入 prewarm 输入摘要。
- P3：`_timed_import` 必须真实测量导入墙钟（供 stage_reconciliation 归属）。
- S2：`_missing_required` 必须检出「项目侧存在、槽内缺失」的必需测试目录。
- S7：fresh 农场回退路径同样要带具名 reason / proof_binding 披露。
- ST3：`farm_provision`/`route_scan` 必须进入分片阶段并入键集。
- E3：串行门必须把 selection_meta 透传给 run_mutation（zero_ref/来源不丢）。
- E4：零引用短路终态必须披露实际 selection_mode。
- E5：`_inject_one` 必须按签名组数钳制并发并披露 jobs_limit_reason。
- CL2：measure run 的行映射可用性必须校验新鲜度（陈旧产物不得冒充可用）。
"""

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli
from godot_qa_toolkit.mutation import executor, prewarm_proof, shadowpool
from godot_qa_toolkit.mutation.channel_router import route_target
from godot_qa_toolkit.mutation.stagetiming import (
    EXEC_STAGE_KEYS,
    StageClock,
    merge_execution_stages,
)


# --------------------------------------------------------------------------
# C2 — autoload 裸名：注释/字符串不构成 compile-time 边
# --------------------------------------------------------------------------
def _router_project(tmp_path, consumer_body):
    proj = tmp_path / "router"
    (proj / "autoload").mkdir(parents=True)
    (proj / "lib").mkdir(parents=True)
    (proj / "project.godot").write_text(
        "config_version=5\n\n[autoload]\n\n"
        'Bus="*res://autoload/bus.gd"\n', encoding="utf-8")
    (proj / "autoload" / "bus.gd").write_text(
        "extends Node\n", encoding="utf-8")
    (proj / "lib" / "consumer.gd").write_text(consumer_body, encoding="utf-8")
    return proj


def test_c2_autoload_name_in_comment_or_string_is_not_edge(tmp_path):
    proj = _router_project(
        tmp_path,
        "# Bus is only mentioned in a comment.\n"
        "extends RefCounted\n"
        'const LABEL := "Bus"\n')
    decision = route_target(str(proj), "res://autoload/bus.gd")
    # 注释/字符串里的 Bus 不产生 consumer→Bus 边：闭包只含目标自身。
    assert decision.closure == ("res://autoload/bus.gd",), decision.closure


def test_c2_real_autoload_identifier_still_makes_edge(tmp_path):
    proj = _router_project(
        tmp_path,
        "extends RefCounted\nvar b = Bus\n")  # 真实裸名引用
    decision = route_target(str(proj), "res://autoload/bus.gd")
    assert "res://lib/consumer.gd" in decision.closure, decision.closure


# --------------------------------------------------------------------------
# P2 — .gqt* 自生成条目不进 prewarm 输入摘要
# --------------------------------------------------------------------------
def test_c2_gqt_generated_entries_excluded_from_input_digest(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (proj / "a.gd").write_text("extends Node\n", encoding="utf-8")
    (proj / ".godot" / "imported").mkdir(parents=True)
    (proj / ".godot" / "uid_cache.bin").write_bytes(b"uid")
    (proj / ".godot" / "global_script_class_cache.cfg").write_text("{}")
    before = prewarm_proof._scan_state(str(proj))["input_digest"]

    (proj / ".gqt-attr-select" / "sel1").mkdir(parents=True)
    (proj / ".gqt-attr-select" / "sel1" / "test_x.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    (proj / ".gqt-worker").mkdir()
    (proj / ".gqt-worker" / "session.json").write_text("{}", encoding="utf-8")
    (proj / ".gqt_beacon_host_1.gd").write_text("extends Node\n",
                                                encoding="utf-8")
    after = prewarm_proof._scan_state(str(proj))["input_digest"]

    assert after == before, \
        ".gqt* 自生成条目不得进入 warm 证书输入摘要（否则恒 drift）"


# --------------------------------------------------------------------------
# P3 — _timed_import 真实测量墙钟
# --------------------------------------------------------------------------
def test_c2_timed_import_measures_wall_clock(tmp_path, monkeypatch):
    def _slow_import(_root):
        time.sleep(0.03)
        return True, "ok"

    monkeypatch.setattr(prewarm_proof, "_run_import", _slow_import)
    ok, reason, wall = prewarm_proof._timed_import(str(tmp_path))
    assert ok is True and reason == "ok"
    assert wall >= 0.02, f"import 墙钟未真实测量: {wall}"


# --------------------------------------------------------------------------
# S2 — _missing_required 检出「项目有、槽缺」的必需目录
# --------------------------------------------------------------------------
def test_c2_missing_required_detects_absent_slot_dir(tmp_path):
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    slot = tmp_path / "slot"
    slot.mkdir()  # 槽内缺 tests/unit
    missing = shadowpool._missing_required(
        str(proj), str(slot), ["res://tests/unit/"])
    assert missing == ["res://tests/unit/"], missing


def test_c2_missing_required_ignores_project_absent_glob(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    slot = tmp_path / "slot"
    slot.mkdir()
    # 项目侧不存在的 glob 不是槽脏（交 GUT 自身报错）
    assert shadowpool._missing_required(
        str(proj), str(slot), ["res://tests/nope/"]) == []


# --------------------------------------------------------------------------
# S7 — fresh 农场回退路径具名披露
# --------------------------------------------------------------------------
def _slot_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (proj / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n")
    return proj


def test_c2_fresh_farm_carries_named_reason(tmp_path):
    proj = _slot_project(tmp_path)
    slot = shadowpool._acquire_fresh(str(proj), "linux", ["calc.gd"], False)
    try:
        farm = slot.farm
        assert farm["mode"] == "fresh"
        assert farm["decision"] in {"created", "rebuilt"}
        assert farm["reason"], "fresh 农场不得静默（缺具名 reason）"
        assert "proof_binding" in farm
        assert isinstance(farm.get("validation"), list)
    finally:
        shadowpool.release_slot(slot)


# --------------------------------------------------------------------------
# ST3 — 分片阶段并入键集必须含 farm_provision / route_scan
# --------------------------------------------------------------------------
def test_c2_exec_stage_keys_include_new_stages():
    assert "farm_provision" in EXEC_STAGE_KEYS
    assert "route_scan" in EXEC_STAGE_KEYS


def test_c2_merge_execution_stages_merges_new_stages():
    clock = StageClock()
    merge_execution_stages(clock, {"farm_provision": 1.25,
                                   "route_scan": 2.5,
                                   "per_mutant": 0.5})
    d = clock.disclosure()
    assert d["farm_provision"] == 1.25
    assert d["route_scan"] == 2.5
    assert d["per_mutant"] == 0.5


# --------------------------------------------------------------------------
# E3 — 串行门透传 selection_meta 到 run_mutation
# --------------------------------------------------------------------------
def test_c2_serial_gate_forwards_selection_meta(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "calc.gd"
    target.write_text(
        "func add(a, b):\n\tif a > b:\n\t\treturn a - b\n\treturn a + b\n")
    meta = {"source": "runtime_mapping", "zero_ref": False,
            "tests": ["res://tests/unit/test_calc.gd"]}
    monkeypatch.setattr(executor, "_auto_scan_tests_glob_meta",
                        lambda *a, **k: ("res://tests/unit/", dict(meta)))
    captured = {}
    monkeypatch.setattr(executor, "run_mutation",
                        lambda *a, **k: captured.update(k) or
                        {"ok": True, "summary": {}})
    executor._single_file_serial_gate(
        [str(target)], str(proj), budget=50, timeout_s=60, tests_glob=None,
        jobs=16, dry_run=False, resume=False, binary_kind=None,
        timing_store=None, mutation_warning=None, timeout_factor=None,
        mutate_all=False, worker_channel=None, select_mode="full")
    assert captured.get("selection_meta") == meta, \
        "串行门必须把 selection_meta 透传给 run_mutation（来源/zero_ref 不丢）"


# --------------------------------------------------------------------------
# E4 — 零引用短路终态披露实际 selection_mode
# --------------------------------------------------------------------------
def test_c2_zero_ref_terminal_discloses_selection_mode(tmp_path):
    target = tmp_path / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n")
    res = executor._zero_ref_terminal(
        str(target), 3, {"zero_ref": True, "selection_mode": "file"})
    assert res is not None
    s = res["results"][0]["summary"]
    assert s["zero_ref_short_circuit"] is True
    assert s["selection_mode"] == "file"


# --------------------------------------------------------------------------
# E5 — _inject_one 按签名组数钳制并发并披露
# --------------------------------------------------------------------------
def test_c2_inject_one_applies_group_concurrency_cap():
    plan = SimpleNamespace(reused={}, diff_active=False, mutate_all=False,
                           executed={})
    ctx = {"plan": plan, "test_selection": {"source": "runtime_mapping"},
           "selection_mode": "line", "selection_groups": ["g1", "g2"],
           "requested_jobs": 8, "src": "", "executed": {}, "uncovered": [],
           "untouched": []}
    r = {"summary": {"killed": 0, "survived": 0, "timeout": 0,
                     "run_errors": 0, "suspect": 0, "mutants": 0}}
    executor._inject_one(r, ctx)
    s = r["summary"]
    assert s.get("jobs_limit_reason") == "selection_groups"
    assert s.get("jobs") == 2
    assert s.get("selection_groups") == 2


# --------------------------------------------------------------------------
# CL2 — measure run 行映射可用性校验新鲜度
# --------------------------------------------------------------------------
def test_c2_attribution_usable_rejects_stale_map(tmp_path):
    amap = tmp_path / "map.json"
    amap.write_text(json.dumps({
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 3, "probes_fired": 3}}}),
        encoding="utf-8")
    now = time.time()
    old = now - 3600
    os.utime(amap, (old, old))
    assert cli._attribution_usable(str(amap), now) is False, \
        "陈旧映射不得冒充本轮可用证据"
    os.utime(amap, (now, now))
    assert cli._attribution_usable(str(amap), now) is True
