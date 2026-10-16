"""SEE-1321 内存变异注入宿主 + per-process user:// 隔离（SPEC-006/007）。

- 变异体经 GDScript.source_code + take_over_path + reload 注入（SEE-1320 探针
  RELOAD_SEES_CHANGE/SWAP_LIVE_NODE_SEES_MUTATION 实证可用），磁盘目标文件
  全程保持原始内容——中断/超时不再留变异残留。
- 每个 Godot 子进程注入 per-process XDG_DATA_HOME 临时目录（Godot user://
  指向），消除并发/跨 workspace 共享 user:// 互踩（SEE-1317 竞态实证）。

win64 godot 读不到 /tmp 绝对路径（WSL 路径无法作为 FileAccess 打开），
宿主 cfg 必须落在 project 根内并经 wslpath 转 Windows 形式传 env。
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 宿主脚本按 PID 隔离：多 worker 并发时共用文件会先结束的 worker 的
# finally unlink() 拆掉后其余 worker 的后续 mutant 轮全部宿主找不到
# （File not found → 判定失真，SEE-1321 并发复现实证）。
def _host_gd_name() -> str:
    return f".gqt_mutation_host_{os.getpid()}.gd"

# 宿主脚本：先做内存注入（失败 exit 3 → runner 归 run_error），再完全复用
# GUT gut_cmdln 的启动序列（版本转换 + main loop 等待 + gut_cli.main）——
# 除注入外与官方入口行为逐行同构，optparse 从命令行收 -gdir/-gexit 参数。
HOST_GD_TEMPLATE = """\
extends SceneTree

func _init() -> void:
\tvar cfg_path := OS.get_environment("GQT_MUTATION_CFG")
\tif cfg_path == "":
\t\tprinterr("GQT_INJECT_FAIL: GQT_MUTATION_CFG env missing")
\t\tquit(3)
\t\treturn
\tvar f := FileAccess.open(cfg_path, FileAccess.READ)
\tif f == null:
\t\tprinterr("GQT_INJECT_FAIL: cannot open cfg " + cfg_path)
\t\tquit(3)
\t\treturn
\tvar cfg = JSON.parse_string(f.get_as_text())
\tif typeof(cfg) != TYPE_DICTIONARY:
\t\tprinterr("GQT_INJECT_FAIL: bad cfg json")
\t\tquit(3)
\t\treturn
\t# 注入必须延迟到 main loop 就绪（autoload 别名与 class_name 全局类在
\t# 引擎完成启动装配后才可解析——-s 主脚本的 _init 期间实测
\t# "Identifier not found: EventBus"，编译上下文不完整）
\tvar max_iter := 20
\tvar iter := 0
\tvar Loader = load("res://addons/gut/gut_loader.gd")
\twhile(Engine.get_main_loop() == null and iter < max_iter):
\t\tawait create_timer(.01).timeout
\t\titer += 1
\tif(Engine.get_main_loop() == null):
\t\tpush_error('Main loop did not start in time.')
\t\tquit(0)
\t\treturn
\t# 变异体经临时 impl 文件走正常 load 编译（完整项目上下文；GDScript.new()
\t# 直接编译会丢 class_name 全局类解析——save_manager.gd 上 SaveData 等
\t# 解析失败实测 err=43），编译成功后 take_over_path 接管原路径。
\t# 磁盘目标文件全程保持原始内容；impl 文件用后即删。
\tvar impl_res: String = String(cfg["impl_res"])
\tvar fo := FileAccess.open(impl_res, FileAccess.WRITE)
\tif fo == null:
\t\tprinterr("GQT_INJECT_FAIL: cannot write impl " + impl_res)
\t\tquit(3)
\t\treturn
\t# impl 是独立编译文件：顶层 class_name 与全局类注册表冲突（hides a
\t# global script class）——剥离 class_name 行；测试经 preload/load 路径
\t# 拿编译对象，不依赖全局类型名（SEE-1321 实测）。
\tvar lines := String(cfg["mutated_source"]).split("\n")
\tfor i in range(lines.size()):
\t\tif lines[i].begins_with("class_name ") or lines[i].begins_with("class_name\t"):
\t\t\tlines[i] = "# gqt-inject stripped: " + lines[i]
\tfo.store_string("\n".join(lines))
\tfo = null
\tvar script = load(impl_res)
\tDirAccess.remove_absolute(ProjectSettings.globalize_path(impl_res))
\tif script == null or not script.can_instantiate():
\t\tprinterr("GQT_INJECT_FAIL: mutant failed to compile via impl file")
\t\tquit(3)
\t\treturn
\tscript.take_over_path(String(cfg["target_res"]))

\tvar cli : Node = load('res://addons/gut/cli/gut_cli.gd').new()
\tget_root().add_child(cli)
\tcli.main()
"""


def wslpath_win(p: str) -> str | None:
    """WSL 绝对路径转 win64 形式（wslpath -w）；不可转换返回 None（调用方兜底）。

    runner._godot_project_path 与本模块 _win_path 共用此原语（转换子进程
    行为逐字段一致，勿分叉）。
    """
    try:
        r = subprocess.run(["wslpath", "-w", p], capture_output=True,
                           text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _win_path(p: str) -> str:
    """WSL 绝对路径转 win64 godot 可读形式；不可转换时原样返回。"""
    if not p.startswith("/"):
        return p
    return wslpath_win(p) or p


ENV_GODOT_BIN = "GQT_GODOT_BIN"

# 二进制类型判定结果缓存（per-process）：mutant 循环内每 mutant 调
# mutant_env，PATH 扫描/子进程探测不该重复付；显式 env 变更经 detect
# 强制参数绕过缓存。
_BINARY_KIND_CACHE: str | None = None


class BinaryConfigError(Exception):
    """`GQT_GODOT_BIN` 显式路径不可用（不存在/不可读/不可执行）——统一错误
    契约出口（§SPEC-006 JSON 契约 ok:false + error；cli 层转译）。消息含
    探测操作与输入路径上下文。"""


def _resolved_godot_path() -> str | None:
    """目标二进制的解析路径：GQT_GODOT_BIN 显式优先，否则 PATH 首匹配。

    显式 env 指向不存在/不可读路径 → BinaryConfigError（LOW-1：显式配置
    错误必须响亮且带契约形态，静默回退 win64 会把配置错误藏进 verdict）。
    """
    explicit = os.environ.get(ENV_GODOT_BIN, "").strip()
    if explicit:
        if not os.path.isfile(explicit):
            raise BinaryConfigError(
                f"{ENV_GODOT_BIN} path does not exist: {explicit}")
        if not os.access(explicit, os.R_OK):
            raise BinaryConfigError(
                f"{ENV_GODOT_BIN} path is not readable: {explicit}")
        if not os.access(explicit, os.X_OK):
            raise BinaryConfigError(
                f"{ENV_GODOT_BIN} path is not executable: {explicit}")
        return explicit
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, "godot")
        if os.access(cand, os.X_OK) and os.path.isfile(cand):
            return cand
    return None


def _kind_of_executable(path: str) -> str:
    """二进制类型：.exe 后缀 / wrapper exec 目标 / ELF 魔数。无法判定视为
    win64（现网缺省二进制为 win64 wrapper——P1 探针 binary_resolution 实证，
    缺省通道必须保持现网行为）。"""
    low = path.casefold()
    if low.endswith(".exe"):
        return "win64"
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return "win64"
    if head.startswith(b"\x7fELF"):
        return "linux"
    # wrapper（bash/python 脚本）：P1 实证 ~/.local/bin/godot 是 exec
    # win64 exe 的 bash wrapper——扫描文本段找 exec 目标。
    for line in head.decode("utf-8", errors="replace").splitlines():
        m = re.search(r'exec\s+"?([^"\s]+)"?', line)
        if m:
            return _kind_of_executable(m.group(1))
    return "win64"


def detect_binary_kind(*, refresh: bool = False) -> str:
    """目标 godot 二进制类型 ∈ {"win64", "linux"}（§SPEC-012 平台分支判据）。

    cli 层（mutation 子命令）在 dispatch 前调用一次并注入 env 判定；
    mutant_env 内部经此取类型。无法解析 → win64（现网缺省，行为不变）。
    """
    global _BINARY_KIND_CACHE
    if _BINARY_KIND_CACHE is not None and not refresh:
        return _BINARY_KIND_CACHE
    resolved = _resolved_godot_path()
    kind = _kind_of_executable(resolved) if resolved else "win64"
    _BINARY_KIND_CACHE = kind
    return kind


def godot_command() -> str:
    """启动 argv 首元：与 detect_binary_kind() 同一解析源（发现三修复）。

    断连根因：判定吃 GQT_GODOT_BIN/PATH 解析、启动 argv 硬编码 "godot"
    （PATH 首匹配）——显式 env 指向 Linux ELF 时实际拉起的仍是 win64
    wrapper，env 通道与引擎类型错配。同源化后：显式 env → 该路径；PATH
    → PATH 首匹配绝对路径；均无 → 字面量 "godot"（现网缺省不变）。
    """
    return _resolved_godot_path() or "godot"


def reset_binary_kind_cache() -> None:
    """测试钩子：清空 per-process 判定缓存。"""
    global _BINARY_KIND_CACHE
    _BINARY_KIND_CACHE = None


def _win_env(cfg_fs: str, user_dir: str | None) -> tuple[dict, str]:
    """win64 通道（既有语义逐字节保留）：UNC/wslpath + APPDATA + WSLENV。"""
    env: dict[str, str] = {}
    if user_dir:
        # 双通道隔离：win64 godot 无视 XDG_DATA_HOME、user:// 跟随
        # %APPDATA%（OS 探针实测 USER_DIR 收敛到自定义 APPDATA）——
        # XDG 同设只为 Linux godot 通道，win64-only 下保留既有双设形态。
        env["XDG_DATA_HOME"] = _win_path(user_dir)
        env["APPDATA"] = _win_path(user_dir)
    env["GQT_MUTATION_CFG"] = _win_path(cfg_fs)
    # win64 godot（WSL interop 启动）不继承 WSL 自定义 env——必须经
    # WSLENV 显式放行（/w = 仅 Windows 侧可见）。调用方已有 WSLENV 时
    # 追加不覆写（LOW-4）。
    existing_wslenv = os.environ.get("WSLENV", "").rstrip(":")
    ours = "GQT_MUTATION_CFG/w:XDG_DATA_HOME/w:APPDATA/w"
    env["WSLENV"] = f"{existing_wslenv}:{ours}" if existing_wslenv else ours
    return env, cfg_fs


def _linux_env(cfg_fs: str, user_dir: str | None) -> tuple[dict, str]:
    """linux 原生通道（§SPEC-012 发现一修复）：cfg/env 原生形态。

    P1 实证两形态缺口：UNC cfg → linux 引擎静默无效化注入（10/10 静默
    survived）；UNC user:// 与原生 impl 混合根 → SIGABRT。原生通道：
    XDG_DATA_HOME 原生 Linux 路径、不设 APPDATA（win64-only 语义）、
    不设 WSLENV（Linux godot 原生继承 env，WSLENV 无副作用也无需放行）。
    """
    env: dict[str, str] = {}
    if user_dir:
        env["XDG_DATA_HOME"] = user_dir
    env["GQT_MUTATION_CFG"] = cfg_fs
    return env, cfg_fs


# 本进程创建的临时文件登记表（SPEC-015）：SIGTERM handler 内 os._exit 跳过
# finally 与 atexit——handler 必须在此处显式调 cleanup_temp_files；atexit 兜底
# 覆盖 SIGINT/未知退出路径。registry 是 per-process 的（每个 worker 管自己
# PID 命名的文件），不跨进程共享。
_TEMP_FILES: set[str] = set()
_atexit_registered = False


def register_temp(path: str | os.PathLike) -> None:
    """登记临时文件；首次登记挂 atexit 兜底（SIGINT/finally 未覆盖的路径）。"""
    _TEMP_FILES.add(str(path))
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(cleanup_temp_files)
        _atexit_registered = True


def forget_temp(path: str | os.PathLike) -> None:
    """正常路径 finally unlink 后从登记表摘除（避免 atexit 空重复）。"""
    _TEMP_FILES.discard(str(path))


def cleanup_temp_files() -> None:
    """清理本进程登记的全部临时文件；单文件失败不中断其余清理（显式暴露）。"""
    for p in list(_TEMP_FILES):
        try:
            Path(p).unlink(missing_ok=True)
        except OSError as e:
            print(f"mutation temp cleanup failed for {p}: {e}", file=sys.stderr)
        finally:
            _TEMP_FILES.discard(p)


def make_user_dir() -> str | None:
    """per-process user:// 根（XDG_DATA_HOME 需绝对路径）；失败返回 None 兜底。"""
    try:
        return tempfile.mkdtemp(prefix="gqt-user-")
    except OSError as e:
        print(f"XDG_DATA_HOME isolation unavailable, continuing shared "
              f"user://: {e}", file=sys.stderr)
        return None


def mutant_env(target_res: str, mutated_src: str, project_root: str,
               seq: int) -> tuple[dict | None, str]:
    """单 mutant 的子进程 env 与宿主 cfg 文件路径。

    返回（env_extra, cfg_fs_path）；env 注入失败兜底时 env_extra 为 None。
    cfg 是 runner 临时清单（JSON: target_res + mutated_source + impl_res），
    调用方负责用后删除。
    """
    stem = Path(target_res).stem
    impl_res = f"res://.gqt_mutation_impl_{os.getpid()}_{seq}_{stem}.gd"
    # impl 磁盘文件虽由 Godot 子进程写、多数轮次正常自删，但 SIGTERM 命中
    # 写盘窗口时会残留（QA 复验 FAIL 实证 3 个）——dispatch 前即登记进
    # per-process 清理表，与 cfg/host 同一兜底路径
    impl_fs = str((Path(project_root) / impl_res.removeprefix("res://")).absolute())
    register_temp(impl_fs)
    cfg_path = Path(project_root) / f".gqt_mutation_cfg_{os.getpid()}_{seq}.json"
    payload = {"target_res": target_res, "mutated_source": mutated_src,
               "impl_res": impl_res}
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    cfg_fs = str(cfg_path.absolute())
    register_temp(cfg_fs)
    user_dir = make_user_dir()
    # §SPEC-012 平台分支：按目标 godot 二进制类型选通道（发现一 HIGH 修复）。
    kind = detect_binary_kind()
    if kind == "linux":
        return _linux_env(cfg_fs, user_dir)
    return _win_env(cfg_fs, user_dir)


def seed_host_script(project_root: str) -> Path:
    """写入（覆盖式）mutation host .gd（per-PID 文件名）；调用方 finally 删除。"""
    host = Path(project_root) / _host_gd_name()
    # 多进程写入同一 cwd 的同 pid 前缀不可能冲突；覆盖写以幂等
    host.write_text(HOST_GD_TEMPLATE, encoding="utf-8")
    register_temp(host)
    return host


def host_script_res() -> str:
    return "res://" + _host_gd_name()


def _impl_fs_path(project_root: str, cfg_path: Path) -> str | None:
    """从 cfg JSON 读 impl_res 换算磁盘路径；cfg 不可读返回 None（不吞异常）。

    SEE-1420 Phase 0 自 runner.py 迁入（<800 行纪律）：impl 临时文件清理
    属注入域关注点。"""
    try:
        import json
        payload = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        rel = payload["impl_res"].removeprefix("res://")
        return str((Path(project_root) / rel).absolute())
    except (OSError, ValueError, KeyError) as e:
        # cfg 已被中断前删除属正常轮次序——静默返回；其余异常显式暴露
        if not Path(cfg_path).exists():
            return None
        print(f"impl path resolution failed for {cfg_path}: {e}", file=sys.stderr)
        return None


# SEE-1420 Phase 2 自 runner.py 迁入（<800 行纪律）：中断恢复守卫属
# 目标文件写回 + 临时清理域，与 cleanup_temp_files 同模块闭环。
def make_sigterm_restore_handler(file_path: str, backup: str):
    """SIGTERM/SIGHUP 硬杀不保证执行 finally——handler 内先恢复再终止。

    exit code 128+sign（POSIX shell 约定），用 os._exit 跳过任何可能再次
    抛异常的清理路径，保证恢复写盘是最后动作。
    """
    def _handler(signum, frame):
        try:
            Path(file_path).write_text(backup, encoding="utf-8")
        except OSError as e:
            # 恢复失败必须显式暴露——静默吞掉会把变异体遗留进共享分支
            print(f"mutation SIGTERM restore failed for {file_path}: {e}",
                  file=sys.stderr)
        # SPEC-015：os._exit 跳过 finally 与 atexit——临时文件（cfg JSON +
        # 宿主 .gd）必须在此显式清理，否则中断后工作树残留（QA MEDIUM 实测
        # 12 个 untracked 文件）。
        cleanup_temp_files()
        os._exit(128 + signum)
    return _handler


class _interrupt_guard:
    """缺陷1 兜底恢复守卫：任何异常/硬杀路径均恢复原文件。

    per-mutant finally 覆盖不到 SIGTERM/SIGINT 硬杀与循环外未知异常（Revy QA
    TaskStop 实证遗留变异体进 git diff）。SIGTERM/SIGHUP 走 handler 主动恢复
    （硬杀不保证 finally），其余异常走 context manager finally 兜底——两路合
    一后原样上抛，不吞中断语义。信号安装失败（非主线程/受限环境）降级为仅
    finally 兜底。
    """

    def __init__(self, path: Path, backup: str):
        self._path = path
        self._backup = backup
        self._handlers: dict = {}

    def __enter__(self):
        restore_handler = make_sigterm_restore_handler(str(self._path), self._backup)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                self._handlers[sig] = signal.signal(sig, restore_handler)
            except (ValueError, OSError):
                pass  # 非主线程/受限环境：守卫降级为 finally 兜底
        return self

    def __exit__(self, exc_type, exc, tb):
        for sig, prev in self._handlers.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, OSError):
                pass
        # 幂等恢复——正常路径与 per-mutant finally 重复写同内容无副作用。
        try:
            self._path.write_text(self._backup, encoding="utf-8")
        except OSError as e:
            print(f"mutation restore failed for {self._path}: {e}", file=sys.stderr)
        return False
