"""真实 Godot 启动的一次性环境隔离（§SPEC-019 HIGH-3 / R5 补漏）。

每次拉起 Godot（validator / GUT / worker）都必须用**全新一次性**的
HOME / XDG_* / TMPDIR，退出即销毁：真实用户目录零写入由构造保证——
不得依赖调用方在外部设置 HOME 充当产品隔离。

平台语义（与 `mutation/inject.py` 的 WSLENV 放行纪律一致，但不解析
二进制、不调用外部工具——隔离路径必须零子进程副作用）：

- **Linux 原生**（非 WSL）：仅 Linux 路径隔离；APPDATA 属 win64 语义
  不设；WSLENV 原样继承——不清空、不追加（无副作用）。
- **WSL**（`WSL_DISTRO_NAME` 存在，可经 interop 拉起 Windows Godot）：
  Windows 侧只认 Windows 路径且**必须**经 WSLENV 显式放行（`/w` = 仅
  Windows 侧可见），否则 Windows 侧回落到宿主 %APPDATA%（真实用户
  目录）。隔离值以 `\\\\wsl.localhost\\<distro>\\...` UNC 形态给出（纯
  函数构造），**绝不透传宿主 Windows 用户路径**；已有 WSLENV 追加不
  覆写（与 inject._win_env 同款 LOW-4 纪律）。

隔离根放在系统临时目录（不在项目树内），因此交付树与生成暂存区零写入。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager

# **仅 Windows 侧使用**的变量（顺序即 WSLENV 追加序）：只有这些做 UNC
# 形态与 WSLENV 放行。Linux 侧使用的 HOME / XDG_* / TMPDIR 始终保持绝对
# Linux 路径——WSL 同样可运行 Linux 原生 Godot，把 Linux 侧变量改成
# UNC 会让 user:// 落到未预期位置（甚至相对路径落到项目目录）。
_WIN_SHARED_VARS = ("APPDATA", "LOCALAPPDATA", "TMP", "TEMP")
_LINUX_SIDE_VARS = ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
                    "XDG_CACHE_HOME", "TMPDIR")


def _wsl_distro() -> str | None:
    """WSL 发行版名（WSL 总是设置 WSL_DISTRO_NAME）；非 WSL 返回 None。"""
    distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    return distro or None


def _unc_path(path: str, distro: str) -> str:
    """Linux 绝对路径的 Windows 侧 UNC 形态（纯函数，零子进程）。

    Windows Godot 经 `\\\\wsl.localhost\\<distro>\\...` 写入 WSL 文件系统——
    与宿主 %APPDATA% 完全隔离。"""
    if not path.startswith("/"):
        return path
    return "\\\\wsl.localhost\\" + distro + path.replace("/", "\\")


@contextmanager
def isolated_godot_env():
    """产出一份一次性隔离环境变量；退出时整目录销毁。"""
    root = tempfile.mkdtemp(prefix="gqt-godot-iso-")
    try:
        native = {
            "HOME": os.path.join(root, "home"),
            "XDG_DATA_HOME": os.path.join(root, "xdg-data"),
            "XDG_CONFIG_HOME": os.path.join(root, "xdg-config"),
            "XDG_CACHE_HOME": os.path.join(root, "xdg-cache"),
            "TMPDIR": os.path.join(root, "tmp"),
        }
        for path in native.values():
            os.makedirs(path, exist_ok=True)
        env = dict(os.environ)
        env.update(native)
        env["TMP"] = native["TMPDIR"]
        env["TEMP"] = native["TMPDIR"]
        distro = _wsl_distro()
        if distro:
            # 按**变量目标侧**分工：仅 Windows 侧变量做 UNC + WSLENV
            # （Windows Godot 用隔离后的 APPDATA/TMP）；Linux 侧变量仍是
            # 绝对 Linux 路径（WSL 内 Linux 原生 Godot 用隔离后的 XDG/TMPDIR）。
            win_dirs = {
                "APPDATA": os.path.join(root, "appdata"),
                "LOCALAPPDATA": os.path.join(root, "localappdata"),
            }
            for path in win_dirs.values():
                os.makedirs(path, exist_ok=True)
            env.update(win_dirs)
            for name in _WIN_SHARED_VARS:
                env[name] = _unc_path(env[name], distro)
            existing = os.environ.get("WSLENV", "").rstrip(":")
            ours = ":".join(f"{name}/w" for name in _WIN_SHARED_VARS)
            env["WSLENV"] = f"{existing}:{ours}" if existing else ours
            for name in _LINUX_SIDE_VARS:
                if not env.get(name, "").startswith("/"):
                    env[name] = native[name]
        else:
            # Linux 原生通道：APPDATA 属 win64 语义（不设）；WSLENV 原样
            # 继承——Linux 引擎无副作用，既不放行也不清空。
            env.pop("APPDATA", None)
            env.pop("LOCALAPPDATA", None)
        yield env
    finally:
        shutil.rmtree(root, ignore_errors=True)
