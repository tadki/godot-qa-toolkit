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
    cfg_path = Path(project_root) / f".gqt_mutation_cfg_{os.getpid()}_{seq}.json"
    payload = {"target_res": target_res, "mutated_source": mutated_src,
               "impl_res": impl_res}
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")
    cfg_fs = str(cfg_path.absolute())
    register_temp(cfg_fs)
    env: dict[str, str] = {}
    user_dir = make_user_dir()
    if user_dir:
        # 双通道隔离：Linux godot 走 XDG_DATA_HOME；win64 godot（本项目
        # 实际二进制）无视 XDG_DATA_HOME、user:// 跟随 %APPDATA%（OS 探针
        # 实测 USER_DIR 收敛到自定义 APPDATA）——两者同设保证任一平台生效
        env["XDG_DATA_HOME"] = _win_path(user_dir)
        env["APPDATA"] = _win_path(user_dir)
    env["GQT_MUTATION_CFG"] = _win_path(cfg_fs)
    # win64 godot（WSL interop 启动）不继承 WSL 自定义 env——必须经
    # WSLENV 显式放行（/w = 仅 Windows 侧可见；Linux godot 下 WSLENV 是
    # 无副作用的普通变量）
    env["WSLENV"] = "GQT_MUTATION_CFG/w:XDG_DATA_HOME/w:APPDATA/w"
    return env, cfg_fs


def seed_host_script(project_root: str) -> Path:
    """写入（覆盖式）mutation host .gd（per-PID 文件名）；调用方 finally 删除。"""
    host = Path(project_root) / _host_gd_name()
    # 多进程写入同一 cwd 的同 pid 前缀不可能冲突；覆盖写以幂等
    host.write_text(HOST_GD_TEMPLATE, encoding="utf-8")
    register_temp(host)
    return host


def host_script_res() -> str:
    return "res://" + _host_gd_name()
