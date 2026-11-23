"""真实 Godot 启动的一次性环境隔离（§SPEC-019 HIGH-3）。

每次拉起 Godot（validator / GUT / worker）都必须用**全新一次性**的
HOME / XDG_* / TMPDIR（Windows 另含 APPDATA / LOCALAPPDATA），退出即销毁：
真实用户目录零写入由构造保证——不得依赖调用方在外部设置 HOME 充当
产品隔离。

隔离根放在系统临时目录（不在项目树内），因此交付树与生成暂存区零写入。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager


@contextmanager
def isolated_godot_env():
    """产出一份一次性隔离环境变量；退出时整目录销毁。"""
    root = tempfile.mkdtemp(prefix="gqt-godot-iso-")
    try:
        dirs = {
            "HOME": os.path.join(root, "home"),
            "XDG_DATA_HOME": os.path.join(root, "xdg-data"),
            "XDG_CONFIG_HOME": os.path.join(root, "xdg-config"),
            "XDG_CACHE_HOME": os.path.join(root, "xdg-cache"),
            "TMPDIR": os.path.join(root, "tmp"),
            "APPDATA": os.path.join(root, "appdata"),
            "LOCALAPPDATA": os.path.join(root, "localappdata"),
        }
        for path in dirs.values():
            os.makedirs(path, exist_ok=True)
        env = dict(os.environ)
        env.update(dirs)
        env["TMP"] = dirs["TMPDIR"]
        env["TEMP"] = dirs["TMPDIR"]
        # WSL 互操作：不得把宿主 Windows 路径透传给子进程（隔离语义）。
        env.pop("WSLENV", None)
        yield env
    finally:
        shutil.rmtree(root, ignore_errors=True)
