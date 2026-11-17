"""项目根解析与首次导入引导（SEE-1594 §SPEC-019 / 零配置）。

单一解析源：mutation / measure / risk / coverage 共用本模块，避免各命令各
自实现「向上找 project.godot」导致口径漂移。

解析顺序：
1. 显式 `--project-root` 优先；不存在或非目录 → fail-loud。
2. 否则从目标路径（无目标则 CWD）向上找最近的含 `project.godot` 的目录。
3. 找不到 → fail-loud（明确提示显式传 `--project-root`）。

`ensure_import_cache` 解决「全新鲜检出无 `.godot` 时默认路径必失败」的
零配置缺口：真实 Godot 项目缺导入缓存时，自动执行一次
`godot --headless --import`（与 prewarm 同源二进制解析），失败即 fail-loud。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class ProjectRootError(ValueError):
    """显式 project root 非法或无法自动发现。"""


class ImportBootstrapError(RuntimeError):
    """自动导入失败（缺二进制 / rc≠0 / 产物不全）。"""


def _is_godot_root(path: Path) -> bool:
    return (path / "project.godot").is_file()


def _walk_up(start: Path) -> Path | None:
    for cur in (start, *start.parents):
        if _is_godot_root(cur):
            return cur
    return None


def resolve_project_root(explicit: str | Path | None, *,
                         target: str | Path | None = None,
                         cwd: str | Path | None = None) -> Path:
    """解析被测 Godot 项目根（显式优先 → 目标/CWD 向上发现）。

    显式值存在但非目录视为非法（fail-loud）；缺省时找不到 `project.godot`
    同样 fail-loud——绝不猜测一个无关目录当前项目。"""
    if explicit is not None and str(explicit) != "":
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ProjectRootError(
                f"explicit --project-root does not exist: {path}")
        if not path.is_dir():
            raise ProjectRootError(
                f"explicit --project-root is not a directory: {path}")
        return path.resolve()

    starts: list[Path] = []
    if target is not None and str(target) != "":
        t = Path(target).expanduser()
        try:
            resolved = t.resolve()
        except OSError:
            resolved = t
        starts.append(resolved.parent if resolved.suffix else resolved)
    starts.append(Path(cwd).resolve() if cwd is not None else Path.cwd().resolve())

    tried = []
    for start in starts:
        if not start.exists():
            start = start.parent if start.parent.exists() else start
        found = _walk_up(start)
        if found is not None:
            return found
        tried.append(str(start))
    raise ProjectRootError(
        "could not discover a Godot project root (no project.godot found "
        f"walking up from: {', '.join(tried)}); pass --project-root explicitly")


def ensure_import_cache(project_root: str | Path, *, timeout: float = 300.0,
                        godot_bin: str | None = None) -> None:
    """真实 Godot 项目缺完整导入缓存时，自动 `godot --headless --import` 一次。

    非 Godot 目录（无 project.godot）直接返回（保持替身/玩具工程形态）。
    导入失败 / 超时 / 产物仍不全 → `ImportBootstrapError`（fail-loud）。
    """
    root = Path(project_root)
    if not (root / "project.godot").is_file():
        return
    # 确定性测试/离线环境逃生门（GQT_AUTO_IMPORT=0）：不触发真实引擎导入。
    if os.environ.get("GQT_AUTO_IMPORT", "1") in ("0", "false", "no"):
        return
    from .mutation.shadow import require_import_cache

    try:
        require_import_cache(str(root))
        return
    except Exception as needs:  # noqa: BLE001 — 缓存不全 → 进入自动导入
        needs_reason = str(needs)

    from .mutation.inject import godot_command

    cmd = godot_bin or godot_command()
    try:
        proc = subprocess.run(
            [cmd, "--headless", "--import", "--path", str(root)],
            cwd=str(root), env=dict(os.environ), capture_output=True,
            text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        # 缺二进制等无法导入的形态：保留 O-1 判据原文（含修复指令），
        # 让用户仍能读到「import cache missing … godot --headless --import」。
        raise ImportBootstrapError(
            f"auto import failed for {root}: {type(e).__name__}: {e} "
            f"(auto import needs godot); {needs_reason}") from e
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise ImportBootstrapError(
            f"auto import failed for {root}: rc={proc.returncode} "
            f"{' | '.join(tail)}; {needs_reason}")
    try:
        require_import_cache(str(root))
    except Exception as e:  # noqa: BLE001
        raise ImportBootstrapError(
            f"auto import did not produce a complete import cache for {root}: "
            f"{e}") from e
