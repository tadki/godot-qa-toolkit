"""SEE-1594 BTIDY（零行为拆分）：per-slot 常驻 worker 会话——spawn 一次、
逐腿 run、致命即死（req/resp 文件会合协议）。自 worker_channel.py 原行段
物理迁移。

**patch seam 纪律**：`time` / `subprocess` / `GlobalGodotGate` 被测试钉在
`worker_channel.*`——本模块经 `_wc.<name>` 动态解析，补丁点调用链不变。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import shadow
from . import worker_channel as _wc

WORKER_DIRNAME = ".gqt-worker"
HOST_SCRIPT_NAME = ".gqt_worker_host.gd"
# 引擎侧常驻宿主：R-C 硬序逐腿执行 + 文件会合协议（原 worker_channel 常量，
# 常量本体随会话实现迁移；worker_channel 再导入保持引用面不变）。
WORKER_HOST_GD = r"""extends SceneTree

const WORK := "res://.gqt-worker/"
const REQ := WORK + "req.json"
const RESP_TMP := WORK + "resp.tmp"
const RESP := WORK + "resp.json"
const STOP := WORK + "stop"


func _init() -> void:
	var max_iter := 20
	while Engine.get_main_loop() == null and max_iter > 0:
		await create_timer(.01).timeout
		max_iter -= 1
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(WORK))
	_touch_boot()
	while not FileAccess.file_exists(STOP):
		if FileAccess.file_exists(REQ):
			var req = JSON.parse_string(FileAccess.get_file_as_string(REQ))
			DirAccess.remove_absolute(ProjectSettings.globalize_path(REQ))
			if typeof(req) == TYPE_DICTIONARY:
				await _run_leg(req)
		await create_timer(0.01).timeout
	quit(0)


func _touch_boot() -> void:
	var f := FileAccess.open(WORK + "boot", FileAccess.WRITE)
	f.store_string("1")
	f = null


func _run_leg(req: Dictionary) -> void:
	var leg := int(req.get("leg", 0))
	var target_res := String(req["target_res"])
	var expected := String(req.get("mutant_sha256", ""))
	var members: Array = req.get("closure", [])
	_wipe_user()
	print("GQT_WORKER_LEG_BEGIN ", leg)
	var resp := {"leg": leg, "hash_ok": false, "reload": {},
		"source_sha": {}, "autoload_in_closure": [], "gut_ran": false,
		"pass": 0, "fail": 0}
	resp["hash_ok"] = _file_sha256(target_res) == expected
	resp["autoload_in_closure"] = _autoload_intersection(members)
	var all_ok := true
	var t_reload := Time.get_ticks_msec()
	for res in members:
		var member := String(res)
		# reload 集只含 .gd（compile-time 绑定唯一载体）；场景持有成员已被
		# 路由器 closure_scene_cached 谓词排除出 worker 通道（R-DW4 Owner
		# 裁决：PackedScene 缓存持有加载时脚本对象，腿间重置不可达）。
		if not member.ends_with(".gd"):
			continue
		var rc := _reload_member(member)
		if rc == 0:
			var scr = load(member)
			if scr != null:
				# D-DVP：软编译失败族——语法可解析但 Godot 编译拒绝的变异体
				# （can_instantiate=false）。与冷通道 beacon-invalid 同语义：
				# 目标成员 44 = 按位点 invalid（会话继续，编译失败不执行任何
				# 代码、不污染会话态）；非目标成员 44 = 原件自身状态，仅记录。
				if not scr.can_instantiate():
					rc = 44
				else:
					resp["source_sha"][member] = scr.source_code.sha1_text()
		resp["reload"][member] = rc
		if rc != 0:
			all_ok = false
	resp["timings"] = {"reload_ms": Time.get_ticks_msec() - t_reload}
	var t_gut := 0
	if bool(resp["hash_ok"]) and all_ok and resp["autoload_in_closure"].is_empty():
		t_gut = Time.get_ticks_msec()
		var gut: Node = load("res://addons/gut/gut.gd").new()
		get_root().add_child(gut)
		# 直驱对齐 GutRunner.run_tests 预跑面（D-W2 模式分歧根因）：error_tracker
		# 必须注册为 OS logger 才能捕获 push_warning/push_error——缺注册时
		# warning 断言全数假失败（KOL 真实套件实测坐实，与 gut_cmdln 判定分裂）。
		# headless pause 跳过选项与 GutRunner.is_headless 分支同款。
		var GutUtils = load("res://addons/gut/utils.gd")
		var GutErrorTracker = load("res://addons/gut/error_tracker.gd")
		GutErrorTracker.register_logger(GutUtils.get_error_tracker())
		gut._ignore_pause_before_teardown = true
		_add_tests(gut, String(req.get("tests_glob", "res://tests/")))
		var done := [false]
		gut.end_run.connect(func() -> void: done[0] = true)
		gut.test_scripts()
		var waited := 0
		while not done[0] and waited < 60000:
			await create_timer(0.01).timeout
			waited += 1
		resp["gut_ran"] = done[0]
		resp["pass"] = gut.get_pass_count()
		resp["fail"] = gut.get_fail_count()
		get_root().remove_child(gut)
		gut.free()
	resp["timings"]["gut_ms"] = Time.get_ticks_msec() - t_gut
	resp["user_files"] = _user_files()
	resp["connections"] = _connection_fingerprint()
	print("GQT_WORKER_LEG_END ", leg, " pass=", resp["pass"],
		" fail=", resp["fail"], " gut_ran=", resp["gut_ran"],
		" hash_ok=", resp["hash_ok"], " reload_ok=", all_ok)
	_emit_resp(resp)


func _emit_resp(resp: Dictionary) -> void:
	var f := FileAccess.open(RESP_TMP, FileAccess.WRITE)
	f.store_string(JSON.stringify(resp))
	f = null
	DirAccess.rename_absolute(ProjectSettings.globalize_path(RESP_TMP),
		ProjectSettings.globalize_path(RESP))


func _reload_member(res: String) -> int:
	var probe = ResourceLoader.load(res, "", ResourceLoader.CACHE_MODE_IGNORE)
	if probe == null:
		return 43
	var replaced = ResourceLoader.load(res, "", ResourceLoader.CACHE_MODE_REPLACE)
	return 0 if replaced != null else 43


func _wipe_user() -> void:
	# 腿间 user:// 清场：冷通道每 mutant 一个全新进程（user:// 天然干净），
	# worker 常驻进程须逐腿恢复同一基线，否则上一腿的存档/缓存文件污染
	# 下一腿判定（D-W2 残留逃逸面之一；目录本身为本次 run 私有，清场安全）。
	var dir := DirAccess.open("user://")
	if dir == null:
		return
	for fname in dir.get_files():
		dir.remove(fname)
	for subname in dir.get_directories():
		_wipe_user_recursive("user://" + subname + "/")


func _wipe_user_recursive(path: String) -> void:
	var dir := DirAccess.open(path)
	if dir == null:
		return
	for fname in dir.get_files():
		dir.remove(path + fname)
	for subname in dir.get_directories():
		_wipe_user_recursive(path + subname + "/")
	dir.remove(path.trim_suffix("/"))


func _file_sha256(res: String) -> String:
	var bytes := FileAccess.get_file_as_bytes(res)
	if bytes.is_empty():
		return ""
	var ctx := HashingContext.new()
	ctx.start(HashingContext.HASH_SHA256)
	ctx.update(bytes)
	return ctx.finish().hex_encode()


func _autoload_intersection(members: Array) -> Array:
	var autoload_paths := {}
	for prop in ProjectSettings.get_property_list():
		var pname := String(prop.get("name", ""))
		if pname.begins_with("autoload/"):
			var value := String(ProjectSettings.get_setting(pname))
			autoload_paths[value.trim_prefix("*")] = true
	var hits := []
	for res in members:
		if autoload_paths.has(String(res)):
			hits.append(String(res))
	return hits


func _add_tests(gut: Node, tests_glob: String) -> void:
	if tests_glob.ends_with(".gd"):
		gut.add_script(tests_glob)
	else:
		gut.add_directory(tests_glob)


func _user_files() -> Array:
	var out := []
	_walk_user("user://", out, 0)
	out.sort()
	return out


func _walk_user(path: String, out: Array, depth: int) -> void:
	if depth > 8:
		return
	var dir := DirAccess.open(path)
	if dir == null:
		return
	for fname in dir.get_files():
		out.append(path + fname)
	for sub in dir.get_directories():
		_walk_user(path + sub + "/", out, depth + 1)


func _connection_fingerprint() -> Array:
	var out := []
	_walk_connections(get_root(), out, 0)
	out.sort()
	return out


func _walk_connections(node: Node, out: Array, depth: int) -> void:
	if depth > 6 or out.size() > 500:
		return
	var script = node.get_script()
	var owner_id := ""
	if script != null:
		owner_id = String(script.resource_path)
	if owner_id == "":
		owner_id = node.get_class()
	for sig in node.get_signal_list():
		var sig_name := String(sig.get("name", ""))
		for conn in node.get_signal_connection_list(sig_name):
			var cb: Callable = conn.get("callable")
			out.append([owner_id, sig_name, String(cb.get_method())])
	for child in node.get_children():
		_walk_connections(child, out, depth + 1)
"""


@dataclass
class WorkerLegResult:
    """单腿结果（引擎回报 + Python 侧解析）。"""

    leg: int
    ok: bool = False
    target_compile_invalid: bool = False
    state_drift: bool = False
    fatal: bool = False
    fatal_reason: str = ""
    hash_ok: bool = False
    reload: dict[str, int] = field(default_factory=dict)
    source_sha: dict[str, str] = field(default_factory=dict)
    autoload_in_closure: list[str] = field(default_factory=list)
    gut_ran: bool = False
    pass_count: int = 0
    fail_count: int = 0
    failing_tests: set[str] = field(default_factory=set)
    user_files: list[str] = field(default_factory=list)
    connections: list[list] = field(default_factory=list)
    timings: dict = field(default_factory=dict)


@dataclass
class _SessionState:
    proc: _wc.subprocess.Popen | None = None
    log_fh: object | None = None
    legs_run: int = 0
    last_canary_leg: int = -1
    gate_ctx: object | None = None
    ref_connections: list | None = None
    ref_user_files: list | None = None


class WorkerSession:
    """per-slot 常驻 worker 会话：spawn 一次、逐腿 run、致命即死。

    传输以文件会合实现（req/resp JSON in `.gqt-worker/`）——避免 stdin
    缓冲在 WSL/win64 interop 下的不确定性；stdout 逐腿以 marker 分段解析
    失败测试名（与 M0 同一 GUT 输出判据）。
    """

    def __init__(self, slot_root: str, project_root: str, kind: str,
                 godot_bin: str | None = None, leg_timeout_s: int = 600,
                 env_extra: dict | None = None):
        self.slot_root = str(slot_root)
        self.project_root = project_root
        self.kind = kind
        self.godot_bin = godot_bin
        self.leg_timeout_s = leg_timeout_s
        self.env_extra = dict(env_extra or {})
        self.state = _SessionState()
        self.dead = False
        self.dead_reason = ""
        # §SPEC-202：累计 boot 耗时（秒）——respawn 会话由调用方收割后清零
        self.boot_s = 0.0

    # -- 生命周期 -------------------------------------------------------

    def __enter__(self) -> "WorkerSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        if self.state.proc is not None and self.state.proc.poll() is None:
            return
        t_boot = _wc.time.monotonic()
        work = Path(self.slot_root) / WORKER_DIRNAME
        work.mkdir(parents=True, exist_ok=True)
        # 上一会话的会合文件（尤其 stop）必须清掉，否则宿主秒退/误读陈旧
        # resp——spawn 前清场是协议正确性前提。
        for stale in ("boot", "stop", "resp.json", "resp.tmp", "req.json"):
            (work / stale).unlink(missing_ok=True)
        for stray in work.glob("req-*.json"):
            stray.unlink(missing_ok=True)
        (Path(self.slot_root) / HOST_SCRIPT_NAME).write_text(
            WORKER_HOST_GD, encoding="utf-8")
        env = dict(os.environ)
        # per-process user:// 隔离（与 M0 同款）；win64/WSLENV 语义复用
        # inject.mutant_env 的通道判定
        from .inject import detect_binary_kind, make_user_dir, mutant_env
        from . import beacon
        user_dir = make_user_dir()
        env_extra, _ = mutant_env(str(work / "placeholder.json"), user_dir)
        env.update(env_extra or {})
        env.update(self.env_extra)
        gate = _wc.GlobalGodotGate(self.project_root)
        gate.__enter__()
        self.state.gate_ctx = gate
        # 每个会话一次性 spawn（定案：进程每 run 启动一次）
        argv = [self.godot_bin or self._godot_command(), "--headless",
                "--path", self._project_path(), "-s",
                "res://" + HOST_SCRIPT_NAME]
        # stdout 落盘（诊断面）：崩溃时 boot 失败错误带回尾段
        self._log_path = work / "worker-log.txt"
        log_fh = open(self._log_path, "w", encoding="utf-8")
        self.state.proc = _wc.subprocess.Popen(
            argv, cwd=self.slot_root, env=env,
            stdout=log_fh, stderr=_wc.subprocess.STDOUT, text=True)
        self.state.log_fh = log_fh
        self._await_boot()
        self.boot_s += _wc.time.monotonic() - t_boot

    def _godot_command(self) -> str:
        from .inject import godot_command
        return godot_command()

    def _project_path(self) -> str:
        from .inject import godot_project_path
        return godot_project_path(self.slot_root, self.kind)

    def _await_boot(self, timeout_s: int = 120) -> None:
        boot = Path(self.slot_root) / WORKER_DIRNAME / "boot"
        deadline = _wc.time.monotonic() + timeout_s
        while _wc.time.monotonic() < deadline:
            if boot.exists():
                return
            if self.state.proc.poll() is not None:
                tail = ""
                log_path = Path(self.slot_root) / WORKER_DIRNAME / "worker-log.txt"
                try:
                    tail = log_path.read_text(encoding="utf-8",
                                              errors="replace")[-800:]
                except OSError:
                    pass
                raise RuntimeError(
                    f"worker exited before boot "
                    f"(rc={self.state.proc.returncode}) — log tail:\n{tail}")
            _wc.time.sleep(0.01)
        raise RuntimeError(f"worker boot timeout after {timeout_s}s")

    def close(self) -> None:
        try:
            work = Path(self.slot_root) / WORKER_DIRNAME
            if work.is_dir():
                (work / "stop").write_text("1", encoding="utf-8")
            if self.state.proc is not None:
                try:
                    self.state.proc.wait(timeout=10)
                except _wc.subprocess.TimeoutExpired:
                    self.state.proc.kill()
                    self.state.proc.wait(timeout=5)
        finally:
            try:
                if getattr(self.state, "log_fh", None) is not None:
                    self.state.log_fh.close()
                    self.state.log_fh = None
            except Exception:
                pass
            if self.state.gate_ctx is not None:
                try:
                    self.state.gate_ctx.__exit__(None, None, None)
                except Exception:
                    pass
                self.state.gate_ctx = None
            self.state.proc = None

    # -- 单腿执行 -------------------------------------------------------

    def run_leg(self, leg: int, target_res: str, mutant_sha256: str,
                closure: list[str], tests_glob: str | None) -> WorkerLegResult:
        """执行一条 R-C 腿；任何致命形态标记 self.dead 并如实返回。"""
        if not self.dead:
            self.start()  # 惰性启动（同一 run 进程只 spawn 一次）
        if self.dead:
            return WorkerLegResult(leg=leg, fatal=True,
                                   fatal_reason=self.dead_reason)
        work = Path(self.slot_root) / WORKER_DIRNAME
        self._target_res = target_res  # D-DVP：目标自身 reload 失败 = 位点级 invalid
        req = {
            "leg": leg,
            "target_res": target_res,
            "mutant_sha256": mutant_sha256,
            "closure": list(closure),
            "tests_glob": tests_glob or "res://tests/",
        }
        req_name = f"req-{leg}-{uuid.uuid4().hex[:8]}.json"
        (work / req_name).write_text(json.dumps(req), encoding="utf-8")
        resp_path = work / "resp.json"
        if resp_path.exists():
            resp_path.unlink()
        # 引擎侧只认固定名 req.json（单腿串行，无并发面）
        os.replace(work / req_name, work / "req.json")
        resp = self._await_resp(resp_path, leg)
        if resp is None:
            self._mark_dead(f"leg {leg} response timeout after "
                            f"{self.leg_timeout_s}s (worker hang unrecoverable)")
            return WorkerLegResult(leg=leg, fatal=True,
                                   fatal_reason=self.dead_reason)
        result = self._interpret(leg, resp)
        self.state.legs_run += 1
        if result.fatal:
            self._mark_dead(result.fatal_reason)
        return result

    def _await_resp(self, resp_path: Path, leg: int) -> dict | None:
        deadline = _wc.time.monotonic() + self.leg_timeout_s
        while _wc.time.monotonic() < deadline:
            if resp_path.exists():
                try:
                    doc = json.loads(resp_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    doc = None
                if isinstance(doc, dict) and int(doc.get("leg", -1)) == leg:
                    resp_path.unlink(missing_ok=True)
                    return doc
            if self.state.proc is not None and self.state.proc.poll() is not None:
                return None
            _wc.time.sleep(0.01)
        return None

    # -- 结果解释与门消费 ------------------------------------------------

    def _target_compile_invalid(self, leg: int, reload_rcs: dict) -> bool:
        """D-DVP：目标成员自身 reload 失败（43 硬解析 / 44 软编译拒绝）= 该
        变异体不可编译 → 位点级 invalid（parity 腿除外——其重载的是原码，
        原件编译状态非变异体责任）。"""
        return leg >= 0 and reload_rcs.get(self._target_res, 0) != 0

    def _interpret(self, leg: int, resp: dict) -> WorkerLegResult:
        r = WorkerLegResult(
            leg=leg,
            hash_ok=bool(resp.get("hash_ok")),
            reload={str(k): int(v) for k, v in
                    (resp.get("reload") or {}).items()},
            source_sha={str(k): str(v) for k, v in
                        (resp.get("source_sha") or {}).items()},
            autoload_in_closure=list(resp.get("autoload_in_closure") or []),
            gut_ran=bool(resp.get("gut_ran")),
            pass_count=int(resp.get("pass", 0)),
            fail_count=int(resp.get("fail", 0)),
            user_files=list(resp.get("user_files") or []),
            connections=list(resp.get("connections") or []),
            timings=dict(resp.get("timings") or {}),
        )
        r.target_compile_invalid = self._target_compile_invalid(leg, r.reload)
        r.failing_tests = self._leg_failing_names(leg)
        reason = self._fatal_reason(r)
        if not reason:
            drift = self._state_drift(r)
            if drift:
                reason = drift
                r.state_drift = True
        if reason:
            r.fatal = True
            r.fatal_reason = reason
        r.ok = (not r.fatal and not r.target_compile_invalid and r.hash_ok
                and r.gut_ran)
        return r

    def _fatal_reason(self, r: WorkerLegResult) -> str:
        """会话致命判定（定案硬规则）：reload 失败 / autoload 交叠 /
        loaded-source 哈希门失配——任一命中即 fatal。目标成员自身的
        reload 失败（43/44）是按位点 invalid，不算会话致命（D-DVP）。"""
        bad_reloads = {m: rc for m, rc in r.reload.items()
                       if rc != 0 and m != self._target_res}
        if bad_reloads:
            return (f"closure reload failed (rc≠0) on "
                    f"{sorted(bad_reloads)} — session fatal, batch "
                    f"invalidation required")
        if r.autoload_in_closure:
            return (f"autoload in reload closure {r.autoload_in_closure} "
                    f"— router exclusion violated at runtime")
        for member, sha in r.source_sha.items():
            disk = shadow.sha1_file(
                Path(self.slot_root) / member.removeprefix("res://"))
            if disk is None or disk != sha:
                return (f"loaded-source hash gate fired on {member} "
                        f"(disk={disk} loaded={sha}) — cache/disk divergence")
        return ""

    def _state_drift(self, r: WorkerLegResult) -> str:
        """腿间状态漂移门（D-W2 canary 拦截面扩大）：冷通道每 mutant 一个
        全新进程，连接指纹与 user:// 树逐腿天然全等；worker 常驻进程必须
        达到同一不变量，否则判定不可信（污染 → 假 kill / 假 survived）。
        参考基线锚定在**首个真实 mutant 腿**（leg>=0；parity 腿 -1 只比对
        判定不锚定指纹——D-DVP 实测：首次 GUT 运行的一次性 user:// 生成
        会使 parity 态与稳态腿不同，锚在 parity 会造成逐腿 0 风暴式误致命）。"""
        st = self.state
        if r.leg < 0:
            return ""  # parity 腿（-1）不参与漂移门：既不比对也不锚定参考
        if not r.gut_ran:
            # 非 GUT 执行腿（位点级 compile-invalid 等跳过 GUT 的腿）指纹无
            # 意义（wipe 后空树）——不参与比对也不得锚定参考（D-DVP 实测：
            # 空锚定会造成 ref=0 vs 35 的风暴式误致命）。
            return ""
        if st.ref_connections is None:
            st.ref_connections = list(r.connections)
            st.ref_user_files = list(r.user_files)
            return ""
        if r.connections != st.ref_connections:
            return (f"connection fingerprint drift at leg {r.leg} "
                    f"(ref={len(st.ref_connections)} entries, "
                    f"now={len(r.connections)}) — inter-leg state pollution, "
                    f"session fatal")
        if r.user_files != st.ref_user_files:
            return (f"user:// tree drift at leg {r.leg} "
                    f"(ref={len(st.ref_user_files)} files, "
                    f"now={len(r.user_files)}) — inter-leg state pollution, "
                    f"session fatal")
        return ""

    def _leg_failing_names(self, leg: int) -> set[str]:
        """从 worker 日志文件抽取该腿分段并解析失败测试名（M0 同判据）。"""
        from .gutrun import _parse_failing_tests
        seg: list[str] = []
        begin = f"GQT_WORKER_LEG_BEGIN {leg}"
        end = f"GQT_WORKER_LEG_END {leg} "
        try:
            for line in self._log_path.read_text(encoding="utf-8",
                                                 errors="replace").splitlines():
                if end in line:
                    if seg:
                        break
                    continue
                if begin in line:
                    seg = [line]
                    continue
                if seg:
                    seg.append(line)
        except OSError:
            return set()
        return _parse_failing_tests("\n".join(seg))

    def _mark_dead(self, reason: str) -> None:
        self.dead = True
        self.dead_reason = reason
        print(f"mutation worker: SESSION FATAL — {reason} "
              f"(respawn + batch invalidation + cold rerun)", file=sys.stderr)

    def invalidate_window(self) -> int:
        """自上次 canary 以来的腿数（整批作废窗口，runner 侧冷重跑）。"""
        since = self.state.last_canary_leg
        return self.state.legs_run - (since + 1) if since >= 0 else self.state.legs_run

    def note_canary(self) -> None:
        self.state.last_canary_leg = self.state.legs_run
