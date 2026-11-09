"""SEE-1550 注入域公共原语：二进制解析链 + per-process user:// 隔离 + 平台
env 形态（win64/WSLENV、linux 原生）。

SEE-1550 M0 起注入 = 磁盘影子注入（shadow.py 槽 + beacon.py 宿主）：mutant
在引擎启动前经 os.replace 落影子内目标 `.gd`，引擎从磁盘读到变异体——本
模块不再承载任何注入机制，只保留与执行通道相关的共用原语：

- Godot 二进制单一解析源（§SPEC-1525-002）：``godot_command()`` /
  ``detect_binary_kind()`` / ``godot_project_path()``——GQT_GODOT_BIN 全局
  默认、命令级覆盖同语义；
- per-process ``user://`` 隔离（SEE-1317 竞态实证，保留）；
- win64 godot 读不到 /tmp 绝对路径（WSL 路径无法作为 FileAccess 打开）：
  cfg/env 必须落在 project 根内并经 wslpath 转 Windows 形式传 env；
- 临时文件登记表（atexit 兜底清理）。
"""

from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


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
ENV_MUTATION_CFG = "GQT_MUTATION_CFG"

# 二进制类型判定结果缓存（per-process）：mutant 循环内每 mutant 调
# mutant_env，PATH 扫描/子进程探测不该重复付；显式 env 变更经 detect
# 强制参数绕过缓存。
_BINARY_KIND_CACHE: str | None = None


class BinaryConfigError(Exception):
    """`GQT_GODOT_BIN` 显式路径不可用（不存在/不可读/不可执行）——统一错误
    契约出口（§SPEC-006 JSON 契约 ok:false + error；cli 层转译）。消息含
    探测操作与输入路径上下文。"""


def _require_usable_binary(path: str, source_label: str) -> str:
    """显式二进制路径校验（env 与 --godot-bin 两通道同款契约）：不存在/
    不可读/不可执行 → BinaryConfigError，消息含来源标签与输入路径——
    静默回退会把配置错误藏进 verdict（LOW-1 契约）。"""
    if not os.path.isfile(path):
        raise BinaryConfigError(f"{source_label} path does not exist: {path}")
    if not os.access(path, os.R_OK):
        raise BinaryConfigError(f"{source_label} path is not readable: {path}")
    if not os.access(path, os.X_OK):
        raise BinaryConfigError(f"{source_label} path is not executable: {path}")
    return path


def _resolved_godot_path() -> str | None:
    """目标二进制的解析路径：GQT_GODOT_BIN 显式优先，否则 PATH 首匹配。

    显式 env 指向不存在/不可读路径 → BinaryConfigError（LOW-1：显式配置
    错误必须响亮且带契约形态，静默回退 win64 会把配置错误藏进 verdict）。
    """
    explicit = os.environ.get(ENV_GODOT_BIN, "").strip()
    if explicit:
        return _require_usable_binary(explicit, ENV_GODOT_BIN)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, "godot")
        if os.access(cand, os.X_OK) and os.path.isfile(cand):
            return cand
    return None


def apply_godot_bin_override(explicit_path: str | None) -> None:
    """§SPEC-1525-002 per-component 覆盖：--godot-bin > GQT_GODOT_BIN > PATH。

    命令级覆盖经 os.environ 写回同源解析链——detect_binary_kind() /
    godot_command() / _godot_project_path 单一出处，各子命令不得另写一套
    解析；校验复用 _require_usable_binary（标签 --godot-bin）。flag 未传
    时零动作（§SPEC-1525-006 冻结红线：不触碰解析面任何字节）。
    """
    if explicit_path is None:
        return
    stripped = explicit_path.strip()
    if not stripped:
        raise BinaryConfigError(
            "--godot-bin requires a non-empty path to a godot binary")
    os.environ[ENV_GODOT_BIN] = _require_usable_binary(stripped, "--godot-bin")


def godot_project_path(project_root: str, kind: str | None = None) -> str:
    """--path 形态与二进制类型同源（§SPEC-1525-002 HIGH 风险面统一原语）。

    win64 通道保留既有语义：/mnt/ 绝对路径经 wslpath -w 转 Windows 形式
    （不可转换原样）——行为与 af728f5 逐字节一致；linux 通道不转换（Linux
    godot 直接读 /mnt/... 原生路径，翻转成 D:\\... 反而不可读）。kind 缺省
    经 detect_binary_kind() 实时解析（同一解析源，不得各写一套）。
    """
    if kind is None:
        kind = detect_binary_kind()
    if kind != "win64":
        return project_root
    if os.path.isabs(project_root) and project_root.startswith("/mnt/"):
        return wslpath_win(project_root) or project_root
    return project_root


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
    env[ENV_MUTATION_CFG] = _win_path(cfg_fs)
    # win64 godot（WSL interop 启动）不继承 WSL 自定义 env——必须经
    # WSLENV 显式放行（/w = 仅 Windows 侧可见）。调用方已有 WSLENV 时
    # 追加不覆写（LOW-4）。
    existing_wslenv = os.environ.get("WSLENV", "").rstrip(":")
    ours = f"{ENV_MUTATION_CFG}/w:XDG_DATA_HOME/w:APPDATA/w"
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
    env[ENV_MUTATION_CFG] = cfg_fs
    return env, cfg_fs


def mutant_env(cfg_fs: str, user_dir: str | None) -> tuple[dict | None, str]:
    """单 mutant 子进程 env（beacon cfg 经 env 注入宿主）。

    返回（env_extra, cfg_fs_path）；按目标 godot 二进制类型选通道
    （§SPEC-012 平台分支）。cfg 由调用方写入并负责清理。
    """
    kind = detect_binary_kind()
    if kind == "linux":
        return _linux_env(cfg_fs, user_dir)
    return _win_env(cfg_fs, user_dir)


# 本进程创建的临时文件登记表（SPEC-015）：SIGTERM handler 内 os._exit 跳过
# finally 与 atexit——需要硬杀路径清理的调用方自行挂 handler 调
# cleanup_temp_files；atexit 兜底覆盖 SIGINT/未知退出路径。registry 是
# per-process 的，不跨进程共享。
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
