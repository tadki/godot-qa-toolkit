"""SEE-1550 L1 槽位池管理：win64/DrvFS 持久 warm 池 + Linux/ext4 fresh 农场，
分层新鲜度权威（终案 §3.4 D1 终版 / §4）。

分层权威，任何歧义整槽重建：
1. repo 侧每 run 一次 ``git status --porcelain`` delta（单点/卡尺 run 跳过
   全量同步——规则 8：定向三向 + repo 侧 stat/hash，不被全量同步淹没）；
2. gqt 自知变更账：mutant replace 只发生在持锁槽内、归还即重链仓原版——
   账本隐含在槽锁与归还断言里，无需独立存储；
3. 槽侧目标集三向校验（repo stat vs 槽 stat vs 池记录）——抓 SIGKILL 残留
   mutant（槽内 inode 已断链 ≠ 仓 inode），重链仓原版即修正；
4. `.godot` sentinel（uid_cache/class_cache/imported）漂移 → 整槽重建；
5. 槽归还 target==原码断言（shadow.restore_target）；
6. 全量 census 仅 env GQT_MUTATION_FULL_CENSUS=1 触发；
7. 抽样哈希只作巡检层，不作为守卫实现（终案明确拒绝抽样唯一守卫）。

并发：池账本 flock（pool.lock 串行化读改写）+ 每槽 flock（slots/<id>.lock，
持锁期间 worker 独占槽）。进程死亡 flock 自动释放 → 下一获取方按记录做
定向三向，残留 mutant 被重链修正。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - 原生 Windows：降级放行（godot_gate 同款）
    fcntl = None

from . import shadow
from .shadow import SlotProvisionError

POOL_SCHEMA = "see1550-pool/1"
WARNED_NO_FCNTL = False


def _pool_dir(project_root: str) -> Path:
    return Path(project_root) / ".qa-cache" / shadow.POOL_DIRNAME


def _pool_manifest_path(project_root: str) -> Path:
    return _pool_dir(project_root) / "pool.json"


class MutationSlot:
    """一次 mutation 执行持有的影子槽句柄。"""

    def __init__(self, root: str, project_root: str, kind: str, mode: str,
                 targets: list[str], targeted_only: bool = False):
        self.root = root
        self.project_root = project_root
        self.kind = kind
        self.mode = mode  # "fresh" | "warm"
        self.targets = [t.replace("\\", "/") for t in targets]
        self.targeted_only = targeted_only
        self.freshness: list[str] = []  # 分层权威逐层结论披露
        self.slot_id = Path(root).name if root else ""
        self.provision: dict = {}
        self._slot_lock_fd: int | None = None
        self.released = False


# ---------------------------------------------------------------------------
# flock 原语
# ---------------------------------------------------------------------------

def _warn_no_fcntl_once() -> None:
    global WARNED_NO_FCNTL
    if not WARNED_NO_FCNTL and fcntl is None:
        WARNED_NO_FCNTL = True
        print("shadowpool: fcntl unavailable — pool locks degrade to "
              "unlocked best-effort (native-Windows honest downgrade)",
              file=sys.stderr)


def _flock_ex(fd: int, blocking: bool) -> bool:
    if fcntl is None:
        return True
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
        return True
    except OSError:
        return False


class _PoolLock:
    """池账本读改写锁（RAII；范围 = 一次 acquire/release 的账本段）。"""

    def __init__(self, project_root: str):
        self.path = _pool_dir(project_root) / "pool.lock"
        self.fd: int | None = None

    def __enter__(self) -> "_PoolLock":
        _warn_no_fcntl_once()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        _flock_ex(self.fd, blocking=True)
        return self

    def __exit__(self, *exc) -> None:
        if self.fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def _try_slot_lock(project_root: str, slot_id: str) -> int | None:
    """尝试独占槽锁；占用中返回 None（换下一个槽）。fd 由调用方持有。"""
    lock_path = _pool_dir(project_root) / "slots" / f"{slot_id}.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return None
    if _flock_ex(fd, blocking=False):
        return fd
    os.close(fd)
    return None


# ---------------------------------------------------------------------------
# 池账本
# ---------------------------------------------------------------------------

def _load_pool(project_root: str) -> dict:
    """读池账本；损坏 = 空池 + stderr 披露（W 矩阵 manifest 损坏格）。"""
    path = _pool_manifest_path(project_root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": POOL_SCHEMA, "slots": {}}
    except (OSError, ValueError) as e:
        print(f"shadowpool: pool manifest corrupted ({e}) — rebuilding "
              f"pool bookkeeping (slot trees verified by targeted three-way)",
              file=sys.stderr)
        return {"schema": POOL_SCHEMA, "slots": {}}
    if not isinstance(raw, dict) or raw.get("schema") != POOL_SCHEMA \
            or not isinstance(raw.get("slots"), dict):
        print("shadowpool: pool manifest schema mismatch — treating as "
              "empty pool", file=sys.stderr)
        return {"schema": POOL_SCHEMA, "slots": {}}
    return raw


def _save_pool(project_root: str, pool: dict) -> None:
    path = _pool_manifest_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"pool.json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(pool, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        # 账本写失败 = 下轮按空池重建槽（安全方向）——披露但不中断本 run
        print(f"shadowpool: pool manifest write failed ({e}) — pool state "
              f"may rebuild next run", file=sys.stderr)


def _new_record(project_root: str, root: str, wiring_hash: str,
                targets: list[str], now: float) -> dict:
    return {"path": root, "created_at": now, "last_used_at": now,
            "wiring_hash": wiring_hash, "in_use": True,
            "delta_hash": "",
            "targets": {t: shadow.stat_record(Path(project_root) / t)
                        for t in targets},
            "sentinel": shadow.godot_sentinel(project_root),
            "size_bytes": shadow.dir_size_bytes(Path(root))}


# ---------------------------------------------------------------------------
# 新鲜度分层权威
# ---------------------------------------------------------------------------

def _sync_repo_delta(project_root: str, slot: MutationSlot, rec: dict) -> None:
    """规则 1：repo delta 同步（warm 每 run；targeted_only 跳过全量同步）。"""
    if slot.targeted_only:
        # 规则 8：单点/卡尺 run 不付全量同步——定向三向已覆盖判定面；
        # delta 锚置空 → 下次全量 run 会补一次同步（安全方向）。
        slot.freshness.append("repo_delta:skipped(targeted)")
        rec["delta_hash"] = ""
        return
    lines = shadow.repo_git_delta(project_root)
    cur = shadow.delta_hash(lines)
    if cur == rec.get("delta_hash"):
        slot.freshness.append("repo_delta:unchanged")
        return
    applied = 0
    root = Path(project_root)
    for line in lines:
        # porcelain 行：XY<space>path；rename 取 " -> " 右侧（新路径）
        path_part = line[3:] if len(line) > 3 else ""
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        path_part = path_part.strip().strip('"')
        if not path_part or path_part.endswith("/"):
            continue
        fs = root / path_part
        status = line[:2]
        if not fs.exists():
            try:
                (Path(slot.root) / path_part).unlink(missing_ok=True)
                applied += 1
            except OSError:
                pass
        elif fs.is_file():
            try:
                shadow.relink_from_repo(project_root, slot.root, path_part)
                applied += 1
            except SlotProvisionError:
                pass
    rec["delta_hash"] = cur
    slot.freshness.append(f"repo_delta:synced({applied} entries)")


def _targeted_three_way(project_root: str, slot: MutationSlot, rec: dict
                        ) -> bool:
    """规则 3：目标集定向三向（repo stat / 槽 stat / 池记录）+ 修正。

    返回 False = 出现歧义（仓目标被删等）→ 调用方整槽重建。SIGKILL 残留
    mutant 表现为槽 inode 断链且内容 ≠ 仓 → 重链仓原版即修正。
    """
    ok = True
    root = Path(project_root)
    targets = rec.get("targets") or {}
    for rel in slot.targets:
        repo_stat = shadow.stat_record(root / rel)
        if repo_stat is None:
            # 仓目标被删（W 矩阵「删除」格）→ 歧义 → 整槽重建
            ok = False
            slot.freshness.append(f"target:{rel}:missing-in-repo")
            continue
        slot_stat = shadow.stat_record(Path(slot.root) / rel)
        if slot_stat is None:
            shadow.relink_from_repo(project_root, slot.root, rel)
            slot.freshness.append(f"target:{rel}:relinked(missing-in-slot)")
        elif slot_stat != repo_stat:
            # inode 断链（mutant replace / SIGKILL 残留）：内容判定——
            # 槽内容 ≠ 仓内容 = 残留 mutant（SIGKILL 抓获格）；一致则仅
            # 重链恢复共享 inode。sha1 只在断链窗口付（非热路径）。
            slot_sha = shadow.sha1_file(Path(slot.root) / rel)
            residual = (slot_sha is not None
                        and slot_sha != shadow.sha1_file(root / rel))
            shadow.relink_from_repo(project_root, slot.root, rel)
            slot.freshness.append(
                f"target:{rel}:relinked(inode-drift"
                f"{'/residual-mutant' if residual else ''})")
        targets[rel] = repo_stat
    rec["targets"] = targets
    return ok


def _sentinel_layer(project_root: str, slot: MutationSlot, rec: dict) -> bool:
    """规则 4：`.godot` sentinel 比对；漂移 = 外部 import → 整槽重建。"""
    cur = shadow.godot_sentinel(project_root)
    if cur != (rec.get("sentinel") or {}):
        slot.freshness.append("godot_sentinel:drift")
        return False
    slot.freshness.append("godot_sentinel:match")
    return True


def _full_census_layer(project_root: str, slot: MutationSlot) -> bool:
    """规则 6：显式 env 才走全量 census（逐文件 sha1 比对）。"""
    if not shadow.full_census_requested():
        return True
    root = Path(project_root)
    mismatches = 0
    for gd in Path(slot.root).rglob("*.gd"):
        rel = gd.relative_to(slot.root).as_posix()
        src = root / rel
        if not src.is_file():
            mismatches += 1
            continue
        if shadow.sha1_file(gd) != shadow.sha1_file(src):
            mismatches += 1
    slot.freshness.append(f"full_census:mismatches={mismatches}")
    return mismatches == 0


# ---------------------------------------------------------------------------
# acquire / release
# ---------------------------------------------------------------------------

def acquire_slot(project_root: str, kind: str, targets: list[str],
                 wiring_hash: str, targeted_only: bool = False,
                 ) -> MutationSlot:
    """获取影子槽：linux→fresh 农场（每 run 新建）；win64→warm 池（复用 +
    分层新鲜度同步）。制备失败上抛 SlotProvisionError（调用方转 run_error）。"""
    if kind != "win64":
        return _acquire_fresh(project_root, kind, targets, targeted_only)
    return _acquire_warm(project_root, targets, wiring_hash, targeted_only)


def _acquire_fresh(project_root: str, kind: str, targets: list[str],
                   targeted_only: bool) -> MutationSlot:
    shadow.prune_farm(kind, project_root)
    root, provision = shadow.create_slot(project_root, kind)
    slot = MutationSlot(root=str(root), project_root=project_root, kind=kind,
                        mode="fresh", targets=targets,
                        targeted_only=targeted_only)
    slot.provision = provision
    slot.freshness.append(
        f"fresh-farm:created(linked={provision['linked']},"
        f"copied={provision['copied']})")
    return slot


def _acquire_warm(project_root: str, targets: list[str], wiring_hash: str,
                  targeted_only: bool) -> MutationSlot:
    with _PoolLock(project_root):
        pool = _load_pool(project_root)
        now = time.time()
        _prune_pool_locked(project_root, pool, now)
        candidates = sorted(
            pool["slots"].items(),
            key=lambda kv: kv[1].get("last_used_at", 0), reverse=True)
        for slot_id, rec in candidates:
            if rec.get("wiring_hash") != wiring_hash:
                continue
            fd = _try_slot_lock(project_root, slot_id)
            if fd is None:
                continue
            slot = MutationSlot(root=rec.get("path") or "", kind="win64",
                                project_root=project_root, mode="warm",
                                targets=targets, targeted_only=targeted_only)
            slot.slot_id = slot_id
            slot._slot_lock_fd = fd
            rec["in_use"] = True
            # D-1（hardener P6 实证）：标死记录 path="" 经 Path("")→'.'
            # 使 is_dir() 恒真，「缺失树→整槽重建」通道不触发——槽以 CWD
            # 为根复活并做 CWD 相对重链，写穿真实仓根。空 path 必须显式
            # 走重建通道。
            if not rec.get("path") or not Path(slot.root).is_dir():
                slot.freshness.append("slot_tree:missing")
                _rebuild_locked(project_root, pool, slot, rec, wiring_hash, now)
                return slot
            if (not _sentinel_layer(project_root, slot, rec)
                    or not _targeted_three_way(project_root, slot, rec)
                    or not _full_census_layer(project_root, slot)):
                # 歧义/哨兵漂移/census 不符 → 整槽重建（终案：任何歧义整槽重建）
                _rebuild_locked(project_root, pool, slot, rec, wiring_hash, now)
                return slot
            _sync_repo_delta(project_root, slot, rec)
            rec["last_used_at"] = now
            rec["wiring_hash"] = wiring_hash
            rec["sentinel"] = shadow.godot_sentinel(project_root)
            _save_pool(project_root, pool)
            slot.freshness.insert(0, "warm:reused")
            return slot
        # 无可复用槽 → 新建（id 含 uuid：同秒同 PID 的并发 acquire 不得碰撞）
        slot_id = f"slot-{int(now)}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        fd = _try_slot_lock(project_root, slot_id)
        root, provision = shadow.create_slot(
            project_root, "win64",
            slot_dir=_pool_dir(project_root) / "slots" / slot_id)
        rec = _new_record(project_root, str(root), wiring_hash, targets, now)
        pool["slots"][slot_id] = rec
        # 插入后再 prune（预算/槽数不变式对新态成立）——旧槽不被误删，
        # 新槽超额时最久未用者先出。
        _prune_pool_locked(project_root, pool, now)
        _save_pool(project_root, pool)
        slot = MutationSlot(root=str(root), project_root=project_root,
                            kind="win64", mode="warm", targets=targets,
                            targeted_only=targeted_only)
        slot.slot_id = slot_id
        slot._slot_lock_fd = fd
        slot.provision = provision
        slot.freshness.append(
            f"warm:created(linked={provision['linked']},"
            f"copied={provision['copied']})")
        return slot


def _rebuild_locked(project_root: str, pool: dict, slot: MutationSlot,
                    rec: dict, wiring_hash: str, now: float) -> None:
    """整槽重建（锁内）：删旧树 → 新建 → 重写记录（任何歧义整槽重建）。"""
    # D-1 同族：标死记录 path="" 时 slot.root 不指向旧树——旧树恒在
    # canonical slots/<slot_id> 下，按槽 id 清理（残留树不得被 overlay
    # 式半重建复活）。
    legacy = _pool_dir(project_root) / "slots" / slot.slot_id
    shutil.rmtree(legacy, ignore_errors=True)
    if slot.root and Path(slot.root) != legacy:
        shutil.rmtree(slot.root, ignore_errors=True)
    root, provision = shadow.create_slot(
        project_root, "win64",
        slot_dir=_pool_dir(project_root) / "slots" / slot.slot_id)
    slot.root = str(root)
    slot.provision = provision
    slot.freshness.append(
        f"rebuilt(linked={provision['linked']},copied={provision['copied']})")
    rec.update(_new_record(project_root, str(root), wiring_hash,
                           slot.targets, now))
    _save_pool(project_root, pool)


def _prune_pool_locked(project_root: str, pool: dict, now: float) -> None:
    """预算/LRU prune（锁内）：超槽数或超预算时删最久未用且未持锁的槽。

    体积用记录值（建槽/release 时核算）——acquire 热路径零 du 扫描。
    """
    slots = pool["slots"]
    max_slots = shadow.DEFAULT_POOL_MAX_SLOTS
    budget = shadow.pool_budget_bytes()

    def _oldest_unlocked() -> str | None:
        cand = [(v.get("last_used_at", 0), k) for k, v in slots.items()
                if not v.get("in_use")]
        if not cand:
            return None
        return min(cand)[1]

    def _drop(slot_id: str) -> int:
        rec = slots.pop(slot_id, None)
        size = int(rec.get("size_bytes") or 0) if rec else 0
        if rec:
            # D-1 同族：标死记录 path="" 不得因空 path 静默残留树——
            # canonical 位置按槽 id 兜底清理（零残留/磁盘预算门）。
            shutil.rmtree(rec.get("path") or "", ignore_errors=True)
            shutil.rmtree(_pool_dir(project_root) / "slots" / slot_id,
                          ignore_errors=True)
        try:
            (_pool_dir(project_root) / "slots" / f"{slot_id}.lock").unlink(
                missing_ok=True)
        except OSError:
            pass
        return size

    while len(slots) > max_slots:
        sid = _oldest_unlocked()
        if sid is None:
            break
        _drop(sid)
    total = sum(int(v.get("size_bytes") or 0) for v in slots.values())
    while total > budget:
        sid = _oldest_unlocked()
        if sid is None:
            break
        total -= _drop(sid)


def release_slot(slot: MutationSlot) -> list[str]:
    """槽归还：目标还原 + 断言 ==原码；warm 槽记录回写；fresh 槽删除。

    还原断言失败 = 槽状态存疑 → 池记录标死（下次获取按 missing tree 重建）
    并 stderr 显式披露（fail-loud，绝不静默）。
    """
    if slot.released:
        return slot.freshness
    slot.released = True
    problems: list[str] = []
    try:
        for rel in slot.targets:
            shadow.restore_target(slot.project_root, slot.root, rel)
        slot.freshness.append("release:targets-restored")
    except SlotProvisionError as e:
        problems.append(str(e))
        print(f"shadowpool: slot return assert FAILED ({e}) — slot marked "
              f"dead for next-run rebuild", file=sys.stderr)
    if slot.mode == "warm":
        with _PoolLock(slot.project_root):
            pool = _load_pool(slot.project_root)
            rec = pool["slots"].get(slot.slot_id)
            if rec is not None:
                rec["in_use"] = False
                rec["last_used_at"] = time.time()
                if problems:
                    rec["path"] = ""  # 标死：树路径清空 → 下次获取按缺失重建
                else:
                    rec["size_bytes"] = shadow.dir_size_bytes(Path(slot.root))
                _save_pool(slot.project_root, pool)
    else:
        shutil.rmtree(slot.root, ignore_errors=True)
    if slot._slot_lock_fd is not None:
        try:
            if fcntl is not None:
                fcntl.flock(slot._slot_lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(slot._slot_lock_fd)
            slot._slot_lock_fd = None
    return problems
