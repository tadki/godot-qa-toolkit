"""SEE-1606 §SPEC-P3 路线 B — cold 一次性 direct-drive 宿主。

形态：每 mutant 仍是一个 fresh godot 进程（冷通道红线不变），但宿主不再
委托 gut_cmdln/gut_cli，而是 beacon L1-L3 核验后**进程内**按脚本顺序执行
（同一 Gut 实例逐脚本 `_test_the_scripts([i])`），任一脚本出现**相对
baseline 的新增失败**即中断后续脚本并打 `GQT_KILL_BAIL` marker。进程数与
现状逐位一致（不拆进程），早停只省面内剩余脚本的执行时间。

脚本清单由 Python 侧 killbail.face_scripts 枚举并经 cfg 注入——清单与
gut_cmdln 对该面的收集逐字节一致（face 判据见 killbail 模块 docstring）。
rc 契约：全部通过 = 0，存在失败 = 1（与既有分类面 rc==0/≠0 兼容）。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .killbail import (BAIL_MARKER, DIRECT_SCRIPTS_MARKER,
                        SKIP_RED_MARKER)

DIRECT_HOST_NAME = ".gqt_direct_host_{pid}.gd"

# 顺序执行原语（路线 A/B 共用：worker 宿主经 WORKER_HOST_GD 组装本段；
# placeholder @BAIL@/@SCRIPTS@ 在模块尾部替换为真实 marker 常量）。
# GUT 事实依据：_init_run 不清 _test_collector——逐脚本调用后 collector 累积，
# 与一次跑全部脚本的终态计数一致；每脚本结果经
# `_test_collector.get_scripts()[i].tests` 的 was_run/is_failing 取名。
SEQUENTIAL_RUNNER_GD = r"""
func _gqt_first_new_failure(names: Array, baseline: Array) -> String:
	for n in names:
		if not baseline.has(String(n)):
			return String(n)
	return ""


func _gqt_script_fail_names(gut: Node, script_index: int) -> Array:
	var names: Array = []
	var scripts: Array = gut._test_collector.get_scripts()
	if script_index < 0 or script_index >= scripts.size():
		return names
	for t in scripts[script_index].tests:
		if t.was_run and t.is_failing():
			names.append(String(t.name))
	return names


func _gqt_chunk_fail_names(gut: Node, from_i: int, to_i: int) -> Array:
	var names: Array = []
	for i in range(from_i, to_i):
		names.append_array(_gqt_script_fail_names(gut, i))
	return names


# SEE-1606 §SPEC-P3：脚本顺序分 chunk 执行 + 新增失败早停。同一 Gut 实例
# 逐 chunk `_test_the_scripts(indices)`（多索引一次调用 = 与一次跑全部的
# legacy 语义同构，仅 chunk 间插新增失败检查）；collector 跨调用累积
# （GUT _init_run 不清 collector）。32 由 Python 常量注入（32：性能
# 门实测口径见 killbail.KILL_BAIL_CHUNK）。
func _gqt_run_scripts_sequential(gut: Node, scripts: Array,
		baseline_failures: Array, per_script_timeout_ms: int) -> Dictionary:
	var out := {"scripts_run": 0, "scripts_total": scripts.size(),
		"bail": false, "bail_at": -1, "bail_new_failure": "",
		"pass": 0, "fail": 0, "hung": false}
	var chunk := 32
	var total := scripts.size()
	var start := 0
	while start < total and not bool(out["hung"]):
		var stop := mini(start + chunk, total)
		var idxs: Array = []
		for i in range(start, stop):
			idxs.append(i)
		var done := [false]
		var cb := func() -> void: done[0] = true
		gut.end_run.connect(cb)
		gut._test_the_scripts(idxs)
		var waited := 0
		while not done[0] and waited * 10 < per_script_timeout_ms:
			await create_timer(0.01).timeout
			waited += 1
		gut.end_run.disconnect(cb)
		if not done[0]:
			out["hung"] = true
			break
		out["scripts_run"] = stop
		out["pass"] = int(out["pass"]) + gut.get_pass_count()
		out["fail"] = int(out["fail"]) + gut.get_fail_count()
		var names := _gqt_chunk_fail_names(gut, start, stop)
		var new_failure := _gqt_first_new_failure(names, baseline_failures)
		if new_failure != "":
			out["bail"] = true
			out["bail_at"] = start
			out["bail_new_failure"] = new_failure
			print("@BAIL@ scripts=" + str(stop) + "/"
				+ str(out["scripts_total"]) + " new_fail=" + new_failure)
			break
		start = stop
	print("@SCRIPTS@=" + str(out["scripts_run"])
		+ "/" + str(out["scripts_total"]) + " bail=" + str(out["bail"]))
	return out


func _gqt_skip_red_tests(gut: Node, red_names: Array) -> Dictionary:
	# SEE-1606 §SPEC-P2-R1：基线红测试从 collector 物理移除（test 粒度），
	# 混合红绿脚本的红测试不再被 GUT 执行；标记数/名单经 marker 透出供
	# Python 侧机器核验。红测试名缺席于面（未命中）如实计数披露。
	var out := {"requested": red_names.size(), "marked": 0, "names": []}
	if red_names.is_empty():
		return out
	var scripts: Array = gut._test_collector.get_scripts()
	for s in scripts:
		var kept: Array = []
		for t in s.tests:
			if red_names.has(String(t.name)):
				out["marked"] = int(out["marked"]) + 1
				if out["names"].size() < 5:
					out["names"].append(String(t.name))
			else:
				kept.append(t)
		s.tests = kept
	print("@SKIPRED@ marked=" + str(out["marked"]) + " requested="
		+ str(out["requested"]) + " names=" + ",".join(out["names"]))
	return out
""".replace("@BAIL@", BAIL_MARKER).replace("@SCRIPTS@", DIRECT_SCRIPTS_MARKER).replace("@SKIPRED@", SKIP_RED_MARKER)

# cold 直驱宿主：beacon L1-L3 逐字节同构（failure 码 3/4 + marker 契约不变）
# → 顺序执行原语。cfg 增量键：tests_scripts / baseline_failures。
COLD_DIRECT_GD_TEMPLATE = r"""extends SceneTree

func _init() -> void:
	var cfg_path := OS.get_environment("GQT_MUTATION_CFG")
	if cfg_path == "":
		printerr("GQT_BEACON_FAIL: GQT_MUTATION_CFG env missing")
		quit(4)
		return
	var f := FileAccess.open(cfg_path, FileAccess.READ)
	if f == null:
		printerr("GQT_BEACON_FAIL: cannot open cfg " + cfg_path)
		quit(4)
		return
	var cfg = JSON.parse_string(f.get_as_text())
	if typeof(cfg) != TYPE_DICTIONARY:
		printerr("GQT_BEACON_FAIL: bad cfg json")
		quit(4)
		return
	var target_res := String(cfg["target_res"])
	var expect_hex := String(cfg["mutant_sha256"])
	var max_iter := 20
	var iter := 0
	while(Engine.get_main_loop() == null and iter < max_iter):
		await create_timer(.01).timeout
		iter += 1
	if(Engine.get_main_loop() == null):
		push_error('Main loop did not start in time.')
		quit(0)
		return
	var bytes := FileAccess.get_file_as_bytes(target_res)
	if bytes.is_empty():
		printerr("GQT_BEACON_FAIL: target unreadable on disk: " + target_res)
		quit(4)
		return
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(bytes)
	if ctx.finish().hex_encode() != expect_hex:
		printerr("GQT_BEACON_FAIL: disk hash mismatch for " + target_res)
		quit(4)
		return
	var script = load(target_res)
	if script == null or not script.can_instantiate():
		printerr("GQT_BEACON_FAIL: mutant failed to compile/load: " + target_res)
		quit(3)
		return
	var auto_name := String(cfg.get("autoload", ""))
	if auto_name != "":
		var inst := get_root().get_node_or_null(NodePath(auto_name))
		if inst == null or inst.get_script() == null or inst.get_script() != script:
			printerr("GQT_BEACON_FAIL: autoload instance not mutant-sourced: "
					+ auto_name)
			quit(4)
			return
	var scripts: Array = cfg.get("tests_scripts", [])
	if scripts.is_empty():
		printerr("GQT_BEACON_FAIL: direct host requires tests_scripts list")
		quit(4)
		return
	var gut: Node = load("res://addons/gut/gut.gd").new()
	get_root().add_child(gut)
	var GutUtils = load("res://addons/gut/utils.gd")
	var GutErrorTracker = load("res://addons/gut/error_tracker.gd")
	GutErrorTracker.register_logger(GutUtils.get_error_tracker())
	gut._ignore_pause_before_teardown = true
	for s in scripts:
		gut.add_script(String(s))
	# SEE-1606 §SPEC-P2-R1：基线红测试物理跳过（test 粒度，collector 移除）。
	var skip_red: Dictionary = await _gqt_skip_red_tests(
		gut, cfg.get("skip_red_test_names", []))
	# kill 即退只在开关开启时生效；bail 基线名单与跳过名单同源但语义独立。
	var bail_on: bool = bool(cfg.get("bail_enabled", false))
	var bail_names: Array = (cfg.get("bail_baseline_failures", [])
		if bail_on else [])
	var seq: Dictionary = await _gqt_run_scripts_sequential(
		gut, scripts, bail_names, 600000)
	seq["skip_red"] = skip_red
	get_root().remove_child(gut)
	gut.free()
	quit(1 if int(seq["fail"]) > 0 else 0)
""" + SEQUENTIAL_RUNNER_GD


def direct_host_name() -> str:
    # per-PID 文件名：warm 槽多 worker 并发复用互不覆写（beacon 同款隔离）。
    return DIRECT_HOST_NAME.format(pid=os.getpid())


def seed_direct_host(slot_root: str) -> Path:
    """写入（覆盖式）cold 直驱宿主 .gd 到影子根；返回 fs 路径。"""
    path = Path(slot_root) / direct_host_name()
    path.write_text(COLD_DIRECT_GD_TEMPLATE, encoding="utf-8")
    return path


def host_script_res(slot_root: str) -> str:
    return "res://" + direct_host_name()


def write_direct_cfg(slot_root: str, target_res: str, mutant_sha256: str,
                     autoload_name: str, scripts: list[str],
                     baseline_failures: list[str],
                     bail_enabled: bool = True,
                     skip_red_names: list[str] | None = None) -> Path:
    """单 mutant 直驱 cfg（beacon cfg 超集）；调用方 finally 删除。

    bail_baseline_failures 仅在 bail_enabled 时被宿主消费（kill 即退）；
    skip_red_test_names 独立生效（§SPEC-P2-R1 物理跳过，与 bail 无关）。"""
    path = Path(slot_root) / f".gqt_beacon_cfg_{uuid.uuid4().hex[:12]}.json"
    payload = {"target_res": target_res, "mutant_sha256": mutant_sha256,
               "autoload": autoload_name,
               "tests_scripts": list(scripts),
               "baseline_failures": sorted(baseline_failures or []),
               "bail_enabled": bool(bail_enabled),
               "bail_baseline_failures": sorted(baseline_failures or []),
               "skip_red_test_names": sorted(skip_red_names or [])}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
