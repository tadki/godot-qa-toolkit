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

from .killbail import (BAIL_MARKER, DIRECT_SCRIPTS_MARKER, HOST_HUNG_MARKER,
                        SCRIPTS_FAIL_MARKER, SKIP_RED_MARKER,
                        TEST_FACE_MARKER)

DIRECT_HOST_NAME = ".gqt_direct_host_{pid}.gd"

# 顺序执行原语（路线 A/B 共用：worker 宿主经 WORKER_HOST_GD 组装本段；
# placeholder @BAIL@/@SCRIPTS@ 在模块尾部替换为真实 marker 常量）。
# GUT 事实依据：_init_run 不清 _test_collector——逐脚本调用后 collector 累积，
# 与一次跑全部脚本的终态计数一致；每脚本结果经
# `_test_collector.get_scripts()[i].tests` 的 was_run/is_failing 取名。
SEQUENTIAL_RUNNER_GD = r"""
# SEE-1615 §SPEC-HANG：执行前脚本清单可加载校验——wrapper/其 extends 目标
# 缺失时 GUT end_run 永不发射，等待将烧满上限（KOL 实测挂起）；不可加载即
# 具名 fail-fast（统一 marker → Python 侧具名 run_error）。
signal _chunk_settled


# SEE-1615 §SPEC-EVWAIT：end_run 回调栈内禁止 disconnect/free(gut)——
# 经 Object.call_deferred 推迟到 idle 帧再 emit 恢复协程（Signal 类型无
# call_deferred 成员，KOL 实测宿主解析失败）。
func _finish_chunk(end_fired: bool) -> void:
	_chunk_settled.emit()


func _gqt_verify_scripts(scripts: Array) -> String:
	# KOL 实测：parse-error wrapper（extends 目标缺失）load() 返回非 null
	# 但 can_instantiate()==false——两者都必须检出，否则 GUT 收集期挂起。
	for s in scripts:
		var res = load(String(s))
		if res == null or not res.can_instantiate():
			return String(s)
	return ""


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
# （GUT _init_run 不清 collector）。chunk 由调用方 cfg/req 注入（Python
# 侧 killbail.resolve_bail_chunk 单点解析；默认 32 = SEE-1606 性能门口径）。
func _gqt_run_scripts_sequential(gut: Node, scripts: Array,
		baseline_failures: Array, per_script_timeout_ms: int,
		chunk: int) -> Dictionary:
	var out := {"scripts_run": 0, "scripts_total": scripts.size(),
		"bail": false, "bail_at": -1, "bail_new_failure": "",
		"pass": 0, "fail": 0, "hung": false}
	var total := scripts.size()
	var start := 0
	while start < total and not bool(out["hung"]):
		var stop := mini(start + chunk, total)
		var idxs: Array = []
		for i in range(start, stop):
			idxs.append(i)
		var done := [false]
		var cb := func() -> void: done[0] = true
		# SEE-1615 §SPEC-EVWAIT：事件等待 + 一次性超时事件（无轮询）——
		# end_run 信号与超时事件同汇 _chunk_settled，语义与 HANG 判定不变。
		var settled := {"end": false, "cap": false}
		# on_end 由 end_run 发射栈内调用——经 Object.call_deferred 推迟出
		# 派发栈（Signal 无 call_deferred 成员；KOL 实测同步恢复会在信号
		# 派发中 disconnect/free(gut) → 锁定崩溃 → resp 永不写）
		var on_end := func() -> void:
			settled["end"] = true
			call_deferred("_finish_chunk", true)
		gut.end_run.connect(on_end)
		var cap := create_timer(per_script_timeout_ms / 1000.0)
		var on_cap := func() -> void:
			settled["cap"] = true
			call_deferred("_finish_chunk", false)
		cap.timeout.connect(on_cap)
		gut._test_the_scripts(idxs)
		await _chunk_settled
		gut.end_run.disconnect(on_end)
		cap.timeout.disconnect(on_cap)
		_chunk_settled.disconnect(on_cap)
		if settled["cap"]:
			out["hung"] = true
			print("@HUNG@ waited_ms=" + str(per_script_timeout_ms)
				+ " scripts_run=" + str(out["scripts_run"])
				+ "/" + str(out["scripts_total"]))
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


func _gqt_skip_red_tests(gut: Node, red_tests: Array) -> Dictionary:
	# SEE-1606 §SPEC-P2-R1 + D2：基线红测试从 collector 物理移除。身份 =
	# 脚本归属 + 测试名（red_tests 元素 "script_path::test_name"）——
	# 绝不按裸测试名跨脚本删除（D2：同名绿测是合法 killer）。仅命中
	# 脚本与名都匹配的测试；未命中的请求对如实计数披露。
	var out := {"requested": red_tests.size(), "marked": 0, "names": []}
	if red_tests.is_empty():
		return out
	var wanted := {}
	for pair in red_tests:
		# cfg JSON 往返后 pair 为 Array [script, name]（不再含 "::" 字面量）。
		if typeof(pair) == TYPE_ARRAY and pair.size() == 2:
			var sid := String(pair[0])
			var tname := String(pair[1])
			wanted[sid] = (wanted.get(sid, []) as Array)
			(wanted[sid] as Array).append(tname)
		elif typeof(pair) == TYPE_STRING:
			var parts := String(pair).split("::", true, 1)
			if parts.size() == 2:
				wanted[parts[0]] = (wanted.get(parts[0], []) as Array)
				(wanted[parts[0]] as Array).append(parts[1])
	var scripts: Array = gut._test_collector.get_scripts()
	for s in scripts:
		var script_id := String(s.path)
		var base := String(s.path).get_file()
		var names: Array = (wanted.get(script_id, [])
			+ wanted.get(base, [])) as Array
		if names.is_empty():
			continue
		var kept: Array = []
		for t in s.tests:
			if names.has(String(t.name)):
				out["marked"] = int(out["marked"]) + 1
				if out["names"].size() < 5:
					out["names"].append(base + "::" + String(t.name))
			else:
				kept.append(t)
		s.tests = kept
	print("@SKIPRED@ marked=" + str(out["marked"]) + " requested="
		+ str(out["requested"]) + " names=" + ",".join(out["names"]))
	return out


# SEE-1610 §SPEC-IMPL：test 级 allowlist 物理过滤（skip_red 的逆语义）。
# allow 元素 [script_id_or_basename, test_name]；test_name=="*" = 整脚本保留
# （script 车道语义，before_all 等脚本级触达不可 test 级收窄）。未命中脚本
# 的测试全部物理移除；kill 判定仍由差集语义完成（marker 只作披露对账）。
func _gqt_allow_tests(gut: Node, allow: Array,
		red_pairs: Array = []) -> Dictionary:
	# SEE-1610 D1/L1：requested/kept/removed 以「对」为账（L1 闭合桶）——
	# matched = 命中 collector 的对数；overlap_red = 其中已被 skip_red 物理
	# 移除（先删后留的重叠）的对数；stale = 其余未命中（陈旧/改名）。
	var out := {"requested": allow.size(), "kept": 0, "removed": 0,
		"whole": 0, "matched_pairs": 0, "overlap_red": 0}
	if allow.is_empty():
		return out
	var wanted := {}
	var red_names := {}
	for rp in red_pairs:
		if typeof(rp) == TYPE_ARRAY and rp.size() == 2:
			red_names[String(rp[0]) + "::" + String(rp[1])] = true
	for pair in allow:
		if typeof(pair) == TYPE_ARRAY and pair.size() == 2:
			var sid := String(pair[0])
			var tname := String(pair[1])
			wanted[sid] = (wanted.get(sid, []) as Array)
			(wanted[sid] as Array).append(tname)
	var scripts: Array = gut._test_collector.get_scripts()
	for s in scripts:
		var script_id := String(s.path)
		var base := String(s.path).get_file()
		var names: Array = (wanted.get(script_id, [])
			+ wanted.get(base, [])) as Array
		if names.is_empty():
			out["removed"] = int(out["removed"]) + s.tests.size()
			s.tests = []
			continue
		if names.has("*"):
			out["kept"] = int(out["kept"]) + s.tests.size()
			out["whole"] = int(out["whole"]) + 1
			out["matched_pairs"] = int(out["matched_pairs"]) + names.size()
			continue
		var kept: Array = []
		for t in s.tests:
			if names.has(String(t.name)):
				kept.append(t)
				out["matched_pairs"] = int(out["matched_pairs"]) + 1
		out["kept"] = int(out["kept"]) + kept.size()
		out["removed"] = int(out["removed"]) + (s.tests.size() - kept.size())
		s.tests = kept
	# 未命中对：区分「红测重叠（skip_red 已物理移除）」与陈旧/改名
	var unmatched := int(out["requested"]) - int(out["matched_pairs"])
	if unmatched > 0:
		for sid in wanted:
			for tname in wanted[sid]:
				if tname == "*":
					continue
				if red_names.has(String(sid) + "::" + String(tname)):
					out["overlap_red"] = int(out["overlap_red"]) + 1
	print("@TESTFACE@ requested=" + str(out["requested"]) + " kept="
		+ str(out["kept"]) + " removed=" + str(out["removed"])
		+ " whole=" + str(out["whole"]) + " matched="
		+ str(out["matched_pairs"]) + " overlap_red="
		+ str(out["overlap_red"]))
	return out
""".replace("@BAIL@", BAIL_MARKER).replace("@SCRIPTS@", DIRECT_SCRIPTS_MARKER).replace("@SKIPRED@", SKIP_RED_MARKER).replace("@TESTFACE@", TEST_FACE_MARKER).replace("@SCRIPTSFAIL@", SCRIPTS_FAIL_MARKER).replace("@HUNG@", HOST_HUNG_MARKER)

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
	# SEE-1615 §SPEC-EVWAIT：一次性 100ms 超时事件（非轮询）；未就绪即具名失败
	await create_timer(0.1).timeout
	if(Engine.get_main_loop() == null):
		printerr('GQT_BEACON_FAIL: main loop did not start in time')
		quit(4)
		return
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
	# SEE-1615 §SPEC-HANG：清单不可加载 → 具名 fail-fast（不等 end_run）
	var bad_script: String = _gqt_verify_scripts(scripts)
	if bad_script != "":
		print("@SCRIPTSFAIL@ path=" + bad_script)
		quit(5)
		return
	var gut: Node = load("res://addons/gut/gut.gd").new()
	get_root().add_child(gut)
	var GutUtils = load("res://addons/gut/utils.gd")
	var GutErrorTracker = load("res://addons/gut/error_tracker.gd")
	GutErrorTracker.register_logger(GutUtils.get_error_tracker())
	gut._ignore_pause_before_teardown = true
	for s in scripts:
		gut.add_script(String(s))
	# SEE-1606 §SPEC-P2-R1/D2：基线红测试物理跳过（脚本+名定位）。
	var skip_red: Dictionary = await _gqt_skip_red_tests(
		gut, cfg.get("skip_red_tests", []))
	# SEE-1610 §SPEC-IMPL：test 级 allowlist 过滤（skip_red 之后、执行前）。
	var allow: Dictionary = await _gqt_allow_tests(
		gut, cfg.get("allow_tests", []),
		cfg.get("skip_red_tests", []))
	# kill 即退只在开关开启时生效；bail 基线名单与跳过名单同源但语义独立。
	var bail_on: bool = bool(cfg.get("bail_enabled", false))
	var bail_names: Array = (cfg.get("bail_baseline_failures", [])
		if bail_on else [])
	var seq: Dictionary = await _gqt_run_scripts_sequential(
		gut, scripts, bail_names, int(cfg.get("host_wait_ms", 120000)),
		int(cfg.get("bail_chunk", 32)))
	seq["skip_red"] = skip_red
	seq["allow"] = allow
	get_root().remove_child(gut)
	gut.free()
	quit(1 if int(seq["fail"]) > 0 else 0)
""".replace("@SCRIPTSFAIL@", SCRIPTS_FAIL_MARKER) + SEQUENTIAL_RUNNER_GD


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
                     skip_red_tests: list[tuple[str, str]] | None = None,
                     allow_tests: list | None = None,
                     bail_chunk: int | None = None) -> Path:
    """单 mutant 直驱 cfg（beacon cfg 超集）；调用方 finally 删除。

    bail_baseline_failures 仅在 bail_enabled 时被宿主消费（kill 即退）；
    skip_red_tests（脚本+名配对）独立生效（§SPEC-P2-R1 物理跳过，与 bail 无关）；
    allow_tests（§SPEC-IMPL，[script, name] 或 [script, "*"]）在 skip_red
    之后物理保留命中测试；bail_chunk（§SPEC-PERF1）缺省经单点解析取
    env/默认（顺序执行 chunk 大小）。"""
    from .killbail import resolve_bail_chunk, resolve_host_wait_ms
    path = Path(slot_root) / f".gqt_beacon_cfg_{uuid.uuid4().hex[:12]}.json"
    payload = {"target_res": target_res, "mutant_sha256": mutant_sha256,
               "autoload": autoload_name,
               "tests_scripts": list(scripts),
               "baseline_failures": sorted(baseline_failures or []),
               "bail_enabled": bool(bail_enabled),
               "bail_baseline_failures": sorted(baseline_failures or []),
               "skip_red_tests": [list(p) for p in (skip_red_tests or [])],
               "allow_tests": [list(p) for p in (allow_tests or [])],
               "bail_chunk": resolve_bail_chunk(bail_chunk),
               "host_wait_ms": resolve_host_wait_ms()}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
