"""SEE-1550 M0 磁盘影子槽原语层（hardlink COW 农场）。

影子 = 目标项目的 per-worker 隔离树：普通文件经硬链接共享原 inode（同设备
零拷贝；无硬链接文件系统/跨设备退真拷贝）；`.godot` 整树实拷贝（顶层拷贝
不够，`imported/` 等子树必须整树；禁止与真实仓或跨 worker 共享 inode）。
mutant 经「临时文件 + os.replace」断链写入影子内目标 `.gd`——每 mutant 一次
replace、无恢复步；槽归还时一次性还原目标并断言 ==原码。

二进制分域影子根（终案 §3.1）：
- Linux ELF → ext4 临时农场（XDG/tmp 下），KOL 规模约秒级；
- win64 wrapper → 项目 `.qa-cache` 内同盘根（保证 Windows 可见性与
  wslpath/UNC 语义；win64 godot 读不到 /tmp）。

建槽前置：先写 `.qa-cache/.gitignore` 为 ``*``——失败 fail-loud
（SlotProvisionError）。19k×8 槽的未跟踪文件会摧毁 git status 新鲜度通道
（沿用 coverlink/fsprobe 自忽略先例）。

目标仓零变异写入：真实仓只允许 `.qa-cache` 与 user:// 白名单——影子内的
mutant 写入不触碰源仓 inode（os.replace 断链语义）。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

POOL_DIRNAME = "mutation-pool"
FARM_DIRNAME = "gqt-mutation-farm"

ENV_POOL_BUDGET_MB = "GQT_MUTATION_POOL_BUDGET_MB"
ENV_POOL_MAX_SLOTS = "GQT_MUTATION_POOL_MAX_SLOTS"
ENV_FULL_CENSUS = "GQT_MUTATION_FULL_CENSUS"

DEFAULT_POOL_BUDGET_MB = 512
DEFAULT_POOL_MAX_SLOTS = 8

# 影子树排除域：git 内部、gqt 缓存（含池本身——池槽里再套池槽会递归建槽）
_EXCLUDED_DIRS = {".git", ".qa-cache"}

# .godot 每槽整树实拷贝——硬链接共享会让外部 --import 经槽 inode 反写/互踩
_GODOT_TREE = ".godot"

_GIT_DELTA_SLOW_S = 10.0
_FARM_MAX_AGE_S = 24 * 3600

# 影子树符号链接不进槽（is_file(follow_symlinks=False) 为假即跳过）——
# 影子必须自足；项目内 symlink 资源属异常布局，缺资源会由 GUT 显式报错
# 而非静默错判。

# per-process git status 缓存（每 run 一次；refresh 强制重测）
_GIT_DELTA_CACHE: dict[str, list[str]] = {}


class SlotProvisionError(RuntimeError):
    """影子槽制备/归还失败（fail-loud 契约：.gitignore 写失败、磁盘不足、
    还原断言不符等）。消息含操作、关键输入与当前状态；调用方转 run_error。"""


def sha1_file(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def stat_record(path: Path) -> dict | None:
    """文件三方校验用的轻量 stat 指纹（size/mtime_ns/inode）。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "inode": st.st_ino}


def ensure_qa_cache_ignored(project_root: str) -> Path:
    """建槽前置：`.qa-cache/.gitignore` = ``*``；失败 fail-loud。"""
    cache = Path(project_root) / ".qa-cache"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        gitignore = cache / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        elif gitignore.read_text(encoding="utf-8").strip() != "*":
            # 既有非自忽略内容会漏掉池文件 → git status 通道被摧毁；
            # 覆写为自忽略（与 coverlink/timing 先例同语义）。
            gitignore.write_text("*\n", encoding="utf-8")
        return cache
    except OSError as e:
        raise SlotProvisionError(
            f"cannot self-ignore .qa-cache under {project_root}: {e} — "
            f"mutation pool would pollute the target repo's git status "
            f"channel") from e


# 导入缓存判据：任一产物在场即视为已导入（godot 4.x `--import` 产物）。
_IMPORT_ARTIFACTS = ("uid_cache.bin", "imported",
                     "global_script_class_cache.cfg")


def require_import_cache(project_root: str) -> None:
    """§SPEC-015 前置守卫：真实 Godot 项目（project.godot 在场）缺 `.godot`
    或导入产物不全 → fail-fast（明确报错 + 修复指令），不得放行进逐位点
    600s timeout 级联（Revy O-1 实证：GUT 收集期挂起，每位点烧满超时）。
    G-1（QA 定向复验 FAIL 裁定）：判据为三产物**俱在才放行**——部分导入
    工程（如只跑过一次部分导入/中途取消）的 GUT 收集同样会挂起，`any()`
    语义放行了该形态，与「任一产物缺失即 fail-fast」判据相反。
    无 project.godot 的玩具/单测工程不设此门（保持既有替身测试形态）。
    """
    root = Path(project_root)
    if not (root / "project.godot").is_file():
        return
    godot_dir = root / ".godot"
    complete = godot_dir.is_dir() and all(
        (godot_dir / name).exists() for name in _IMPORT_ARTIFACTS)
    if complete:
        return
    state = "missing" if not godot_dir.is_dir() else "present but incompletely imported"
    raise SlotProvisionError(
        f"target project import cache {state}: {root}/.godot — engine test "
        f"collection would hang per site (Revy O-1). Run "
        f"`godot --headless --import --path {root}` first")


def binary_domain_root(project_root: str, kind: str) -> Path:
    """分域影子根（不创建目录）：linux→ext4 农场；win64→.qa-cache 同盘根。"""
    if kind == "win64":
        return (Path(project_root) / ".qa-cache" / POOL_DIRNAME / "slots")
    slug = hashlib.sha1(
        str(Path(project_root).resolve()).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / FARM_DIRNAME / slug


def godot_sentinel(project_root: str) -> dict:
    """`.godot` 新鲜度哨兵（uid_cache/class_cache/imported 的 mtime+size）。

    外部 ``godot --import`` 会改动这些路径——哨兵漂移 = 影子内 `.godot` 落后，
    必须整树重建（gqt 自记账：自家 prewarm 在建槽前发生，建槽后哨兵即锚）。
    """
    g = Path(project_root) / _GODOT_TREE
    if not g.is_dir():
        return {"present": False}

    def _sig(p: Path) -> list | None:
        try:
            st = p.stat()
            return [st.st_mtime_ns, st.st_size]
        except OSError:
            return None

    return {"present": True,
            "uid_cache": _sig(g / "uid_cache.bin"),
            "class_cache": _sig(g / "global_script_class_cache.cfg"),
            "imported": _sig(g / "imported")}


def repo_git_delta(project_root: str, refresh: bool = False) -> list[str]:
    """repo 侧新鲜度权威：``git status --porcelain`` 全量行（含 untracked）。

    每 run 一次（per-process 缓存）；KOL 实测 19.5–74.5s，超 10s stderr
    披露（最坏 ~75s 计入 warm 同步成本，终案 §3.4）。非 git 目录返回 []。
    """
    key = str(Path(project_root).resolve())
    if not refresh and key in _GIT_DELTA_CACHE:
        return _GIT_DELTA_CACHE[key]
    try:
        t0 = time.monotonic()
        r = subprocess.run(["git", "status", "--porcelain"], cwd=key,
                           capture_output=True, text=True, timeout=300)
        elapsed = time.monotonic() - t0
        if elapsed > _GIT_DELTA_SLOW_S:
            print(f"freshness: repo git status took {elapsed:.1f}s "
                  f"({key}) — counted into warm-sync cost", file=sys.stderr)
        lines = r.stdout.splitlines() if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError) as e:
        print(f"freshness: git status unavailable ({e}) — repo delta "
              f"treated as empty", file=sys.stderr)
        lines = []
    _GIT_DELTA_CACHE[key] = lines
    return lines


def delta_hash(lines: list[str]) -> str:
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


def _link_or_copy(src: Path, dst: Path) -> None:
    """hardlink COW 原语：同设备硬链接；无硬链接 FS/跨设备退真拷贝。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _populate(dst_root: Path, src_root: Path) -> tuple[int, int]:
    """递归建影子树；返回 (hardlinked, copied)。`.godot` 命中即整树实拷贝。"""
    linked = copied = 0
    dst_root.mkdir(parents=True, exist_ok=True)
    for entry in os.scandir(src_root):
        name = entry.name
        if name in _EXCLUDED_DIRS:
            continue
        src = Path(entry.path)
        dst = dst_root / name
        if name == _GODOT_TREE and entry.is_dir(follow_symlinks=False):
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied += 1
            continue
        if entry.is_dir(follow_symlinks=False):
            linked_n, copied_n = _populate(dst, src)
            linked += linked_n
            copied += copied_n
        elif entry.is_file(follow_symlinks=False):
            _link_or_copy(src, dst)
            try:
                shared = (os.stat(src).st_ino == os.stat(dst).st_ino)
            except OSError:
                shared = False
            if shared:
                linked += 1
            else:
                copied += 1
    return linked, copied


def create_slot(project_root: str, kind: str,
                slot_dir: Path | None = None) -> tuple[Path, dict]:
    """建影子槽：返回 (影子根, 制备统计 {linked, copied, slot_id, root})。

    调用方（shadowpool）决定槽目录（fresh 农场临时目录 / warm 池固定名）；
    本函数只负责 .gitignore 前置 + 树填充 + 统计。磁盘不足等 OSError 转
    SlotProvisionError（fail-loud，清理半成品）。
    """
    project = Path(project_root)
    ensure_qa_cache_ignored(project_root)
    if slot_dir is None:
        base = binary_domain_root(project_root, kind)
        try:
            base.mkdir(parents=True, exist_ok=True)
            slot_dir = Path(tempfile.mkdtemp(prefix="gqt-slot-", dir=base))
        except OSError as e:
            raise SlotProvisionError(
                f"cannot create shadow slot dir under {base}: {e}") from e
    else:
        try:
            slot_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SlotProvisionError(
                f"cannot create shadow slot dir {slot_dir}: {e}") from e
    try:
        linked, copied = _populate(slot_dir, project)
    except OSError as e:
        shutil.rmtree(slot_dir, ignore_errors=True)
        raise SlotProvisionError(
            f"shadow populate failed for {project} → {slot_dir} "
            f"(disk full?): {e}") from e
    return slot_dir, {"linked": linked, "copied": copied,
                      "slot_id": slot_dir.name, "root": str(slot_dir)}


def inject_mutant(slot_root: str, target_rel: str, mutated_src: str) -> str:
    """影子内目标 .gd 的 mutant 注入：临时文件 + os.replace（每 mutant 一次，
    无恢复步——下一个 mutant 直接覆盖上一个）。返回 mutant sha256。

    写失败（磁盘不足等）直接上抛 OSError——调用方转 run_error，绝不静默
    把原码留给引擎（那会假 survived）。
    """
    target = Path(slot_root) / target_rel
    tmp = target.parent / f".gqt_mut_{uuid.uuid4().hex[:12]}.tmp"
    data = mutated_src.encode("utf-8")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return hashlib.sha256(data).hexdigest()


def relink_from_repo(project_root: str, slot_root: str, target_rel: str) -> None:
    """从真实仓重链目标到影子（freshness 定向同步 / 槽归还原子）。

    重链 = 先链到影子内临时名再 os.replace——原子替换，中断残留只有临时
    文件（下轮定向同步清不掉也不影响判定：目标路径内容恒为仓内容）。
    """
    src = Path(project_root) / target_rel
    dst = Path(slot_root) / target_rel
    if not src.is_file():
        raise SlotProvisionError(
            f"repo target vanished during relink: {src}")
    tmp = dst.parent / f".gqt_relink_{uuid.uuid4().hex[:12]}.tmp"
    try:
        _link_or_copy(src, tmp)
        os.replace(tmp, dst)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise SlotProvisionError(
            f"relink {target_rel} into slot failed ({e})") from e


def restore_target(project_root: str, slot_root: str, target_rel: str) -> None:
    """槽归还：重链仓原版 + 读回断言 ==原码（终案 §3.1 一次性还原断言）。"""
    relink_from_repo(project_root, slot_root, target_rel)
    slot_bytes = (Path(slot_root) / target_rel).read_bytes()
    orig_bytes = (Path(project_root) / target_rel).read_bytes()
    if slot_bytes != orig_bytes:
        raise SlotProvisionError(
            f"slot return assert failed: {target_rel} in slot {slot_root} "
            f"differs from repo original after restore")


def prune_farm(kind: str, project_root: str, max_age_s: int = _FARM_MAX_AGE_S
               ) -> int:
    """fresh 农场遗留槽清理（崩溃残留；ext4 tmp 无池账本可查——按年龄）。

    只删超过 max_age 的槽目录；返回删除数。失败逐个披露不中断。
    """
    base = binary_domain_root(project_root, kind)
    if not base.is_dir():
        return 0
    removed = 0
    now = time.time()
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            if now - child.stat().st_mtime > max_age_s:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError as e:
            print(f"shadow farm prune skipped {child}: {e}", file=sys.stderr)
    return removed


def dir_size_bytes(path: Path) -> int:
    """目录体积（池 LRU 预算核算用）；统计中断返回已累计值。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                continue
    return total


def pool_budget_bytes() -> int:
    raw = os.environ.get(ENV_POOL_BUDGET_MB, "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_POOL_BUDGET_MB * 1024 * 1024
    return max(1, v) * 1024 * 1024


def pool_max_slots() -> int:
    """warm 池槽上限（§SPEC-203 测量旋钮：env 覆写，缺省 = DEFAULT 常量）。

    缺省路径读模块常量（保持既有 monkeypatch seam）；无效值 stderr 告警后
    回退默认（不静默）；钳制下限 1（与 pool_budget_bytes 的 max(1, v) 先例
    同语义）。"""
    raw = os.environ.get(ENV_POOL_MAX_SLOTS, "")
    if raw.strip() == "":
        return DEFAULT_POOL_MAX_SLOTS
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"shadowpool: invalid {ENV_POOL_MAX_SLOTS}={raw!r} — "
              f"falling back to default {DEFAULT_POOL_MAX_SLOTS}",
              file=sys.stderr)
        return DEFAULT_POOL_MAX_SLOTS


def full_census_requested() -> bool:
    """全量 census 仅显式 env / 建槽验收触发（终案 §3.4 规则 6）。"""
    return os.environ.get(ENV_FULL_CENSUS, "").strip() not in ("", "0", "false")
