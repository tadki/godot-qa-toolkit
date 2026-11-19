"""SEE-1606 §SPEC-P3 — 脚本粒度 kill 即退（对齐 Stryker `disableBail=false`）。

单一出处面：
- 开关解析（CLI `--no-kill-bail` / env `GQT_MUTATION_KILL_BAIL`）；默认开启，
  对齐 Stryker 默认 bail（"aborts a unit test run for a mutant as soon as one
  test fails"）。
- 选面判定（face_scripts）：direct-drive 顺序执行只在**gqt 自物化的选择目录**
  （`.gqt-attr-select/<id>/`，平面 wrapper 集）或单脚本 glob 上启用——这两类
  面的脚本清单可零歧义枚举，与 gut_cmdln `-gdir` 的收集结果逐字节一致；
  显式 `--tests` 目录 / `--select full` 全脸的 GUT 收集语义（gutconfig
  prefix/include_subdirs）不在本模块复制，禁用 bail 并具名披露
  （`unsupported_explicit_or_full`），绝不静默改道。
- 早停 marker 解析（`GQT_KILL_BAIL scripts=k/n new_fail=<test>`），宿主与
  Python 分类侧共用同一 marker 契约。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_KILL_BAIL = "GQT_MUTATION_KILL_BAIL"
SELECT_ROOT = ".gqt-attr-select"  # 与 truth_select.SELECT_ROOT 同值（字符串面）
BAIL_MARKER = "GQT_KILL_BAIL"
DIRECT_SCRIPTS_MARKER = "GQT_DIRECT_SCRIPTS"
# SEE-1606 §SPEC-P2-R1：基线红测试物理跳过 marker（宿主从 collector 移除
# 红测试后打点；Python 分类侧据此区分「干净批次」与「跑过但不算」）。
SKIP_RED_MARKER = "GQT_SKIP_RED"

# 顺序执行 chunk 大小：性能门实测（SEE-1606 DoD）——per-script 逐脚本调
# 用 `_test_the_scripts([i])` 的固定开销 ~0.46s/次，286 脚本宽面幸存腿
# 210.9s vs 现状 78.5s（2.69×）不达标；C=32 时幸存腿开销 ≈ (N/C)×0.46
# （286→9 次 ≈ +5%，落 A/A 噪声带内），被杀腿至多多跑 C-1 个脚本。
KILL_BAIL_CHUNK = 32

FACE_SELECTION_DIR = "selection_dir"
FACE_SINGLE_SCRIPT = "single_script"
FACE_UNSUPPORTED = "unsupported_explicit_or_full"

_OFF_VALUES = ("0", "false", "no")

_BAIL_MARKER_RE = re.compile(
    rf"{BAIL_MARKER} scripts=(\d+)/(\d+) new_fail=(\S+)")


def resolve_kill_bail(flag: bool | None = None) -> bool:
    """有效开关：显式 flag 最高优先；env 关断逃生门；默认开启。"""
    if flag is not None:
        return bool(flag)
    return os.environ.get(ENV_KILL_BAIL, "").strip().lower() \
        not in _OFF_VALUES


def disabled_by(flag: bool | None = None) -> str | None:
    """开关关闭时的归因（具名披露用）：flag / env / None。"""
    if flag is False:
        return "flag"
    if flag is None and os.environ.get(ENV_KILL_BAIL, "").strip().lower() \
            in _OFF_VALUES:
        return "env"
    return None


def face_scripts(project_root: str,
                 tests_glob: str | None) -> tuple[list[str] | None, str]:
    """(脚本清单, face 种类)。None = 该面不支持顺序执行（具名禁用）。

    - 选择目录（`.gqt-attr-select/<id>/`）：平面 wrapper 集，枚举 = 顶层
      `*.gd` 排序——与 gut_cmdln 对该目录的非递归收集一致（wrapper 名继承
      原脚本名，天然满足 prefix 过滤）。
    - 单脚本 glob：清单就是它本身。
    - 其余（显式 --tests 目录 / None 全脸）：GUT 收集语义依赖 gutconfig 的
      prefix/include_subdirs，本模块不复制 → 具名不支持。
    """
    if not tests_glob:
        return None, FACE_UNSUPPORTED
    g = tests_glob.replace("\\", "/")
    if g.endswith(".gd"):
        return [g], FACE_SINGLE_SCRIPT
    root = str(Path(project_root).resolve())
    # res:// 相对项目根，不相对 CWD——必须先拼项目根再 resolve。
    real = str((Path(root) / g.removeprefix("res://")).resolve()) \
        if not g.startswith("/") else g
    sel_marker = f"/{SELECT_ROOT}/"
    if sel_marker in real.replace("\\", "/") and real.startswith(root):
        sel_dir = Path(real)
        if sel_dir.is_dir():
            scripts = sorted(
                f"res://{p.relative_to(Path(root)).as_posix()}"
                for p in sel_dir.glob("*.gd") if p.suffix == ".gd")
            return scripts, FACE_SELECTION_DIR
    return None, FACE_UNSUPPORTED


_SKIP_RED_RE = re.compile(
    rf"{SKIP_RED_MARKER} marked=(\d+) requested=(\d+) names=(\S*)")


def parse_skip_red_marker(tail: str) -> dict | None:
    """红测试物理跳过 marker → 披露 dict；缺席 = 本腿未发生跳过（None）。"""
    m = _SKIP_RED_RE.search(tail or "")
    if m is None:
        return None
    names = [n for n in m.group(3).split(",") if n]
    return {"marked": int(m.group(1)), "requested": int(m.group(2)),
            "names": names}


def parse_bail_marker(tail: str) -> dict | None:
    """宿主早停 marker → 披露 dict；缺席 = 未早停（None）。"""
    m = _BAIL_MARKER_RE.search(tail or "")
    if m is None:
        return None
    return {"scripts_run": int(m.group(1)), "scripts_total": int(m.group(2)),
            "new_failure": m.group(3)}


def disclosure(enabled: bool, face_kind: str, scripts_n: int | None,
               disabled_by_reason: str | None) -> dict:
    """summary 级 kill_bail 披露（条件在场性由调用方决定，键恒给）。"""
    return {"enabled": bool(enabled), "face": face_kind,
            "scripts": scripts_n, "disabled_by": disabled_by_reason}
