"""SEE-1550 §SPEC-020 第二批 ③——per-slot 常驻 GUT 直驱 worker
（exec_scheme=worker-reset/1，定案 A1–A9 照案实施）。

形态：每 run 进程启动一次；mutant 影盘写入时序与 M0 完全一致（Python 侧
`os.replace`）；引擎侧常驻循环逐腿执行 R-C 硬序：

    L1 磁盘读回哈希 → IGNORE 原位刷码（CACHE_MODE_IGNORE 强制读盘）→
    REPLACE 落缓存（CACHE_MODE_REPLACE）→ 逐闭包 reload（硬序，全 ok 才
    实例化）→ autoload 交叠防御 → fresh Gut 直驱（gut.gd test_scripts，
    E0 通路，await end_run）→ 腿末状态回报（loaded-source 哈希 / user://
    树 / 连接指纹）

reload 原语实证（本批 /tmp 探针）：`GDScript.reload()` 只重编译缓存文本、
不读盘（rc=0 假阳性）；单步 `CACHE_MODE_REPLACE` 被路径缓存挡住（同秒
写不可见）；**IGNORE 读盘 + REPLACE 落缓存两步**后 fresh `load()` 见新码
——R-C 的「逐闭包脚本 reload」就落在这两步上。

会话致命（定案硬规则，测试钉住）：闭包任一 reload 失败（parse/缺失，
rc≠0）/ loaded-source 哈希失配 / autoload 交叠 / 腿挂死 → 不再执行后续
mutant → respawn + 自上次 canary 窗口整批作废 + 冷重跑 + stderr 披露；
禁止静默改道或 IGNORE 后继续。

A9 opt-in：默认冷通道，worker 通道须显式启用（`--worker-channel` /
`GQT_MUTATION_WORKER=1`）；定案红线：baseline 永远实测走冷通道。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import shadow
from .channel_router import CHANNEL_WORKER, route_target
from .godot_gate import GlobalGodotGate
from .identity import COLD_SCHEME, WORKER_SCHEME

ENV_WORKER = "GQT_MUTATION_WORKER"
ENV_CANARY_EVERY = "GQT_WORKER_CANARY_EVERY"
DEFAULT_CANARY_EVERY = 25
WORKER_DIRNAME = ".gqt-worker"
HOST_SCRIPT_NAME = ".gqt_worker_host.gd"


def worker_enabled(flag: bool | None = None) -> bool:
    """A9 opt-in：显式 flag 或 env 才启用；默认冷通道。"""
    if flag is not None:
        return flag
    return os.environ.get(ENV_WORKER, "").strip() in ("1", "true", "yes")


def canary_every() -> int:
    raw = os.environ.get(ENV_CANARY_EVERY, "")
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_CANARY_EVERY
    except (TypeError, ValueError):
        return DEFAULT_CANARY_EVERY


def resolve_exec_scheme(project_root: str, target_res: str,
                        worker_flag: bool | None = None
                        ) -> tuple[str, object]:
    """(exec_scheme, route)：worker 显式启用且路由判 worker 才走
    worker-reset/1，否则冷通道（A9 opt-in）。route=None = 未启用未路由。"""
    if not worker_enabled(worker_flag):
        return COLD_SCHEME, None
    route = route_target(project_root, target_res)
    if route.channel == CHANNEL_WORKER:
        return WORKER_SCHEME, route
    return COLD_SCHEME, route


def identity_for_target(identity: dict | None, project_root: str,
                        target_res: str, worker_flag: bool | None = None
                        ) -> tuple[dict | None, object, str]:
    """按目标解析 scheme 并把 exec_scheme 写入身份包（file_key 隔离面）。"""
    scheme, route = resolve_exec_scheme(project_root, target_res, worker_flag)
    if identity is None:
        return None, route, scheme
    return {**identity, "exec_scheme": scheme}, route, scheme


def canary_due(leg_index: int, every: int | None = None) -> bool:
    """三层门时序（synthesis §4）：首 mutant 强制双通道预检 + 周期 renew。"""
    k = canary_every() if every is None else every
    return leg_index == 0 or leg_index % k == 0


def prepare_scene_refresh(slot_root: str, closure: list[str],
                          leg: int) -> list[dict]:
    """场景/资源成员的旁路刷新对（D-W2）：PackedScene 缓存持有加载时解析的
    脚本对象，正典路径单步 REPLACE 不重读磁盘、IGNORE 两步在宿主会话内
    挂死。字节复制到每腿唯一旁路路径（首次加载必 fresh，ext_resource 绑
    当前缓存的新脚本对象），引擎侧 take_over_path 接管正典路径。"""
    out: list[dict] = []
    for res in closure:
        if res.endswith(".gd"):
            continue
        src = Path(slot_root) / res.removeprefix("res://")
        if not src.is_file():
            continue
        side_dir = Path(slot_root) / WORKER_DIRNAME
        side_dir.mkdir(parents=True, exist_ok=True)
        side = side_dir / f"refresh-{leg}-{src.name}"
        shutil.copy2(src, side)
        out.append({"canonical": res,
                    "side": f"res://{WORKER_DIRNAME}/{side.name}"})
    return out


@dataclass
class WorkerLegResult:
    """单腿结果（引擎回报 + Python 侧解析）。"""

    leg: int
    ok: bool = False
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
    proc: subprocess.Popen | None = None
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

    # -- 生命周期 -------------------------------------------------------

    def __enter__(self) -> "WorkerSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        if self.state.proc is not None and self.state.proc.poll() is None:
            return
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
        gate = GlobalGodotGate(self.project_root)
        gate.__enter__()
        self.state.gate_ctx = gate
        # 每个会话一次性 spawn（定案：进程每 run 启动一次）
        argv = [self.godot_bin or self._godot_command(), "--headless",
                "--path", self._project_path(), "-s",
                "res://" + HOST_SCRIPT_NAME]
        # stdout 落盘（诊断面）：崩溃时 boot 失败错误带回尾段
        self._log_path = work / "worker-log.txt"
        log_fh = open(self._log_path, "w", encoding="utf-8")
        self.state.proc = subprocess.Popen(
            argv, cwd=self.slot_root, env=env,
            stdout=log_fh, stderr=subprocess.STDOUT, text=True)
        self.state.log_fh = log_fh
        self._await_boot()

    def _godot_command(self) -> str:
        from .inject import godot_command
        return godot_command()

    def _project_path(self) -> str:
        from .inject import godot_project_path
        return godot_project_path(self.slot_root, self.kind)

    def _await_boot(self, timeout_s: int = 120) -> None:
        boot = Path(self.slot_root) / WORKER_DIRNAME / "boot"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
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
            time.sleep(0.01)
        raise RuntimeError(f"worker boot timeout after {timeout_s}s")

    def close(self) -> None:
        try:
            work = Path(self.slot_root) / WORKER_DIRNAME
            if work.is_dir():
                (work / "stop").write_text("1", encoding="utf-8")
            if self.state.proc is not None:
                try:
                    self.state.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
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
                closure: list[str], tests_glob: str | None,
                scene_refresh: list[dict] | None = None) -> WorkerLegResult:
        """执行一条 R-C 腿；任何致命形态标记 self.dead 并如实返回。"""
        if not self.dead:
            self.start()  # 惰性启动（同一 run 进程只 spawn 一次）
        if self.dead:
            return WorkerLegResult(leg=leg, fatal=True,
                                   fatal_reason=self.dead_reason)
        work = Path(self.slot_root) / WORKER_DIRNAME
        req = {
            "leg": leg,
            "target_res": target_res,
            "mutant_sha256": mutant_sha256,
            "closure": list(closure),
            "tests_glob": tests_glob or "res://tests/",
            "scene_refresh": list(scene_refresh or []),
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
        deadline = time.monotonic() + self.leg_timeout_s
        while time.monotonic() < deadline:
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
            time.sleep(0.01)
        return None

    # -- 结果解释与门消费 ------------------------------------------------

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
        r.failing_tests = self._leg_failing_names(leg)
        reason = self._fatal_reason(r)
        if not reason:
            reason = self._state_drift(r)
        if reason:
            r.fatal = True
            r.fatal_reason = reason
        r.ok = (not r.fatal and r.hash_ok and r.gut_ran)
        return r

    def _fatal_reason(self, r: WorkerLegResult) -> str:
        """会话致命判定（定案硬规则）：reload 失败 / autoload 交叠 /
        loaded-source 哈希门失配——任一命中即 fatal。"""
        bad_reloads = {m: rc for m, rc in r.reload.items() if rc != 0}
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
        参考基线取首腿（boot 后干净态）；此后任一腿漂移即会话致命。"""
        st = self.state
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


# 引擎侧常驻宿主：R-C 硬序逐腿执行 + 文件会合协议。reload 原语 =
# CACHE_MODE_IGNORE（强制读盘，见模块 docstring 的实证）+ REPLACE（落缓存）；
# 腿末回报 loaded-source sha1（静态哈希门）/ user:// 树 / 连接指纹。
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
		if member.ends_with(".gd"):
			var rc := _reload_member(member)
			resp["reload"][member] = rc
			if rc != 0:
				all_ok = false
			else:
				var scr = load(member)
				if scr != null:
					resp["source_sha"][member] = scr.source_code.sha1_text()
		else:
			# 场景/资源成员经 scene_refresh 旁路对刷新（Python 侧逐腿字节复制
			# 到唯一旁路路径）——见 prepare_scene_refresh；此处不再直接 load
			# 正典路径（REPLACE 不重读已缓存资源、IGNORE 在宿主内挂死）。
			pass
	var refresh_list = req.get("scene_refresh")
	if refresh_list == null:
		refresh_list = []
	for rf in refresh_list:
		var canonical := String(rf.get("canonical", ""))
		var side := String(rf.get("side", ""))
		if side == "" or canonical == "":
			continue
		var fresh = ResourceLoader.load(side, "")
		if fresh == null:
			resp["reload"][canonical] = 43
			all_ok = false
		else:
			fresh.take_over_path(canonical)
			resp["reload"][canonical] = 0
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



def _parity_gate_leg(session, slot, target_res, closure, tests_glob,
                     baseline_failures, parity_inject_fn):
    """会话前置 parity 门（D-W2）：直驱失败集必须逐位等于冷 baseline 失败集。

    返回 (degrade_all, fatal_event)；披露（stderr）在本函数内完成，事件串
    由调用方汇入 fatal_events。门未启用（参数缺省）直接放行。"""
    if baseline_failures is None or parity_inject_fn is None:
        return False, None
    parity_sha, parity_err = parity_inject_fn()
    if parity_err is not None or session.dead:
        print("mutation worker: PARITY GATE unavailable — degrading "
              "this file to the cold channel (disclosed, not "
              "silently)", file=sys.stderr)
        return True, (f"parity leg unavailable: "
                      f"{parity_err or session.dead_reason}")
    pleg = session.run_leg(-1, target_res, parity_sha, list(closure),
                           tests_glob,
                           scene_refresh=prepare_scene_refresh(
                               slot.root, closure, -1))
    if pleg.fatal:
        print(f"mutation worker: PARITY GATE fatal — "
              f"{pleg.fatal_reason}; degrading this file to the "
              f"cold channel (disclosed, not silently)", file=sys.stderr)
        return True, f"parity leg fatal: {pleg.fatal_reason}"
    if pleg.gut_ran and pleg.failing_tests != baseline_failures:
        print("mutation worker: PARITY MISMATCH — direct-drive "
              "failing set differs from cold baseline on this "
              "suite; degrading this file to the cold channel "
              "(equivalence preserved, disclosed)", file=sys.stderr)
        return True, ("mode parity gate: direct-drive failing set diverges "
                      f"from cold baseline (worker="
                      f"{sorted(pleg.failing_tests)[:4]} "
                      f"cold={sorted(baseline_failures)[:4]})")
    return False, None


def _apply_canary(canary_cold, verdict, seq, session):
    """canary 双通道比对（D-W2 通道资格否证语义）。

    返回 (verdict, mismatch_event, mismatch)。失配时 stderr 披露在本函数
    内完成；窗口改冷权威 + 会话关闭 + 剩余冷降级由调用方执行（编排状态居
    编排层）。"""
    if canary_cold is None:
        return verdict, None, False
    if canary_cold["verdict"] == verdict["verdict"]:
        session.note_canary()
        return verdict, None, False
    print(f"mutation worker: CANARY MISMATCH at leg {seq} "
          f"(cold={canary_cold['verdict']} "
          f"worker={verdict['verdict']}) — channel "
          f"disqualified for this file: window invalidated "
          f"and ALL remaining legs degraded to cold "
          f"(disclosed, equivalence preserved)", file=sys.stderr)
    # canary 失配 = 双通道等价性被实证否证（D-W2 KOL 实测：mutant 才触发
    # 的 autoload 状态污染无法静态预判，且「作废窗口后继续 worker」会让
    # 后续未验证判定静默出货）——窗口内改冷权威 + 剩余全部冷降级。
    event = (f"canary mismatch leg {seq}: cold="
             f"{canary_cold['verdict']} "
             f"worker={verdict['verdict']}")
    return canary_cold, event, True  # 本腿已有冷权威判定


def run_worker_legs(mutants, slot, target_rel, target_res, closure, kind,
                    tests_glob, inject_fn, cold_runner, record_fn,
                    classify_fn, leg_timeout_s,
                    baseline_failures=None, parity_inject_fn=None):
    """§SPEC-020 W 通道逐腿执行（R-C 硬序 + 门消费 + 会话致命处置）。

    会话致命（闭包任一 reload rc≠0 / loaded-source 哈希门 fire / autoload
    交叠 / 腿挂死）→ 不再执行后续 mutant：respawn + **自上次 canary 窗口
    整批作废 + 冷重跑** + stderr 披露（定案硬规则，禁止静默改道/继续）。
    首腿与每 k 腿双通道 canary：cold oracle 与 worker 判定必须同档，失配
    = 通道资格否证（窗口冷权威 + 剩余全部冷降级，披露）。

    会话前置 parity 门（D-W2，`_parity_gate_leg`）：直驱 GUT 与 gut_cmdln
    对同一代码必须产出同一失败集，否则腿判定不可迁移；失配 → 披露 +
    整文件冷降级（等价性守恒）。

    编排/会话/门居本模块；verdict 语义（classify_fn）、冷重跑
    （cold_runner）、注入（inject_fn）与记录（record_fn）由 runner 注入
    ——避免反向依赖。复杂度纪律（R-DW5）：门判定下沉
    `_parity_gate_leg` / `_apply_canary`，编排循环只留状态迁移。
    """
    raw_pairs: list[tuple] = []
    fatal_events: list[str] = []
    window_start = 0
    session_failures = 0  # 防抖：respwan 连续失败 → 剩余腿全冷降级
    degrade_all = False  # parity 门失配：本文件剩余全部冷跑（非会话级）

    def _record(m, verdict, elapsed_s=0.0):
        raw_pairs.append((m, verdict))
        record_fn(m, verdict, elapsed_s)

    def _run_cold(m, seq):
        t0 = time.monotonic()
        return cold_runner(m, seq), time.monotonic() - t0

    def _recast_cold(start: int) -> None:
        """整批作废：窗口内 worker 判定全部改冷重跑（权威回归冷通道）。"""
        for idx in range(start, len(raw_pairs)):
            m, _old = raw_pairs[idx]
            verdict, elapsed = _run_cold(m, idx)
            raw_pairs[idx] = (m, verdict)
            record_fn(m, verdict, elapsed)

    def _new_session() -> WorkerSession:
        return WorkerSession(slot.root, slot.project_root, kind,
                             godot_bin=None, leg_timeout_s=leg_timeout_s)

    def _recover_fatal(seq, leg, m):
        """会话致命处置：窗口作废冷重跑 + respawn；返回 (verdict, elapsed)。"""
        nonlocal session, window_start, session_failures
        session_failures += 1
        print(f"mutation worker: SESSION FATAL at leg {seq} — "
              f"{leg.fatal_reason}; window {window_start}.."
              f"{len(raw_pairs)} invalidated, cold rerun", file=sys.stderr)
        _recast_cold(window_start)
        session.close()
        session = _new_session()
        window_start = len(raw_pairs)
        verdict, elapsed = _run_cold(m, seq)
        if session_failures >= 2:
            print("mutation worker: respawn failed twice — "
                  "degrading remaining legs to the cold channel "
                  "(disclosed, not silently)", file=sys.stderr)
        return verdict, elapsed

    session = _new_session()
    try:
        degrade_all, parity_event = _parity_gate_leg(
            session, slot, target_res, closure, tests_glob,
            baseline_failures, parity_inject_fn)
        if parity_event is not None:
            fatal_events.append(parity_event)
        for seq, m in enumerate(mutants):
            mutant_sha, inject_error = inject_fn(m)
            if inject_error is not None:
                _record(m, inject_error)
                continue
            canary_cold = cold_runner(m, seq) if canary_due(seq) else None
            if degrade_all or session_failures >= 2 or session.dead:
                # parity 失配 / 会话不可用（respawn 连续失败）：剩余腿全冷降级
                verdict, elapsed = _run_cold(m, seq)
                _record(m, verdict, elapsed)
                continue
            t_leg = time.monotonic()
            leg = session.run_leg(seq, target_res, mutant_sha, list(closure),
                                  tests_glob,
                                  scene_refresh=prepare_scene_refresh(
                                      slot.root, closure, seq))
            leg_elapsed = time.monotonic() - t_leg
            if leg.fatal:
                verdict, elapsed = _recover_fatal(seq, leg, m)
                fatal_events.append(leg.fatal_reason)
                _record(m, verdict, elapsed)
                continue
            session_failures = 0
            verdict = classify_fn(m, leg)
            verdict, canary_event, mismatch = _apply_canary(
                canary_cold, verdict, seq, session)
            if mismatch:
                fatal_events.append(canary_event)
                _recast_cold(window_start)
                session.close()
                degrade_all = True
            _record(m, verdict, leg_elapsed)
    finally:
        session.close()
    return raw_pairs, fatal_events


def execute_worker_branch(path, project_root, original_src, mutants,
                          tests_glob, baseline_failures, baseline_dirty,
                          slot, auto_name, target_rel, target_res, kind,
                          reporter, store2, prior_entries, scopes,
                          ownership_fn):
    """§SPEC-020 W 通道接线（薄层）：注入/冷跑/记录/分类回调注入编排层。

    runner 域符号在函数体内延迟 import（baseline.py 先例）——避免
    runner ↔ worker_channel 的模块级循环依赖。
    """
    from . import runner as _runner

    route = route_target(project_root, target_res)

    def _inject(m):
        try:
            return shadow.inject_mutant(
                slot.root, target_rel,
                _runner._apply_mutation(original_src, m)), None
        except (OSError, SlotProvisionError) as e:
            return None, _runner._mutant_record(
                m, "run_error",
                f"disk injection failed for {target_res}: {e}",
                src=original_src)

    def _cold(m, seq):
        return _runner._run_single_mutant(
            m, path, project_root, original_src, "", tests_glob, seq,
            baseline_failures, baseline_dirty, slot=slot,
            auto_name=auto_name, ownership_fn=ownership_fn,
            target_rel=target_rel, target_res=target_res)

    def _parity_inject():
        try:
            return shadow.inject_mutant(slot.root, target_rel, original_src), \
                None
        except (OSError, SlotProvisionError) as e:
            return None, f"parity injection failed for {target_res}: {e}"

    def _record(m, verdict, elapsed_s=0.0):
        if reporter is not None:
            reporter.record(m, verdict, elapsed_s)
        if store2 is not None:
            from .diffengine import make_entry, streak_for
            prior = prior_entries.get(m.stable_id)
            streak, v2 = streak_for(prior, verdict["verdict"])
            store2.record_entry(make_entry(m, verdict, v2, streak,
                                           scopes or {}, time.time()))

    from .reporting import classify_worker_leg
    return run_worker_legs(
        mutants, slot, target_rel, target_res, route.closure, kind,
        tests_glob, _inject, _cold, _record,
        lambda m, leg: classify_worker_leg(
            m, leg, baseline_failures, baseline_dirty, ownership_fn,
            project_root, original_src),
        max(60, int(_runner._adaptive_timeout or 60)),
        baseline_failures=set(baseline_failures or ()),
        parity_inject_fn=_parity_inject)

