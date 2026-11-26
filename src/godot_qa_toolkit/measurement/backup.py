"""SEE-1594 BTIDY（零行为拆分）：measurement 编排支撑——错误类型、
路径/键工具与 §SPEC-070/071 备份恢复走廊。自 orchestrator.py 原行段物理
迁移；`_recover_stale_backups` 按「记录校验 / 恢复计划 / 原子应用」分解
为三个 ≤50 行阶段函数，逻辑逐语句等价。"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


class MeasurementError(Exception):
    """measurement 编排入参/前置违例（recipe 非法、目标缺失、GUT 缺失）。"""


def file_key8(relpath: str) -> str:
    return hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:8]


_PROBE_LOCK_DEPTH: "dict[tuple[str, int], int]" = {}
_PROBE_LOCK_WAIT_S = 120.0


@contextmanager
def probe_rw_lock(project_root: "str | Path"):
    """C3-R14（§SPEC-032③）插桩改写/恢复的项目级互斥：`<root>/.qa-cache/
    probe-rw.lock` 的 O_EXCL 原子创建（拿不到 = 持有者存活，自旋等待，
    上限 _PROBE_LOCK_WAIT_S 后具名放弃互斥）。测量臂与增量腿把插桩文本
    写进源树、结束才恢复——并发两进程交错注入/恢复会把源文件留在插桩后
    的样子（QA D-NEW-1 实碰的数据损坏）。持锁段覆盖"注入 → GUT 执行 →
    恢复"全程。持有者进程死由陈旧锁清理承担（超过持有上限即视为陈旧）。

    进程内可重入（嵌套的增量腿与 run_measurement 取同一把锁）：按
    （锁路径, 线程 id）深度计数，同线程重入直接放行；跨线程/跨进程
    仍互斥。锁文件不可落盘（假 root / 只读挂载）→ 降级无锁并具名披露。"""
    import os as _os
    import threading as _threading
    import time as _time

    root = Path(project_root)
    lock_dir = root / ".qa-cache"
    lock_path = lock_dir / "probe-rw.lock"
    key = (str(lock_path.resolve()), _threading.get_ident())
    if _PROBE_LOCK_DEPTH.get(key, 0) > 0:
        _PROBE_LOCK_DEPTH[key] += 1
        try:
            yield
        finally:
            _PROBE_LOCK_DEPTH[key] -= 1
        return
    fd = None
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"probe-rw lock unavailable (no lock dir), proceeding "
              f"without mutual exclusion: {e}", file=sys.stderr)
        yield
        return
    t0 = _time.monotonic()
    while True:
        try:
            fd = _os.open(str(lock_path),
                          _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
            break
        except FileExistsError:
            if (_time.monotonic() - t0) > _PROBE_LOCK_WAIT_S:
                print(f"probe-rw lock still held after "
                      f"{_PROBE_LOCK_WAIT_S}s — proceeding without "
                      f"mutual exclusion (stale lock cleanup is best "
                      f"effort)", file=sys.stderr)
                yield
                return
            _time.sleep(0.05)
        except OSError as e:
            print(f"probe-rw lock unavailable, proceeding without "
                  f"mutual exclusion: {e}", file=sys.stderr)
            yield
            return
    _PROBE_LOCK_DEPTH[key] = 1
    try:
        yield
    finally:
        _PROBE_LOCK_DEPTH[key] = 0
        try:
            _os.unlink(str(lock_path))
        except OSError:
            pass
        if fd is not None:
            _os.close(fd)


def _rel_within_root(abs_path: str, root: Path) -> str:
    """§SPEC-070 FR-3：目标不在 project root 内 → MeasurementError
    fail-loud（裸 ValueError 会在 CLI 泄漏 traceback）。"""
    try:
        return str(Path(abs_path).relative_to(root))
    except ValueError as e:
        raise MeasurementError(
            f"target outside project root: {abs_path} (root={root})") from e


def batch_sink_name(run_uuid: str, relpath: str) -> str:
    return f"qa-coverage-hits-{run_uuid}-{file_key8(relpath)}.txt"


def _backup_base() -> Path:
    """§SPEC-070 FR-1/R26：备份根必须进程/用户私有——/tmp 上的共享备份根
    可被任意本地用户预置/篡改 started.json（恢复信任边界）。per-uid 私有
    目录 + lstat（不跟随 symlink）+ O_NOFOLLOW dirfd 验证消除
    mkdir/lstat 竞争窗口；无 uid 的非 POSIX 平台显式 fail-loud，不做
    不安全回退。任一校验失败即 MeasurementError。"""
    import stat as _stat

    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise MeasurementError(
            "private backup root requires a POSIX uid; non-POSIX "
            "platforms are unsupported (fail-loud, no unsafe fallback)")
    uid = getuid()
    base = Path(tempfile.gettempdir()) / f"gqt-canonical-backups-{uid}"
    if not base.exists():
        try:
            base.mkdir(mode=0o700)
        except FileExistsError:
            pass
    st = base.lstat()  # symlink 呈现 S_ISLNK，不跟随
    if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISDIR(st.st_mode):
        raise MeasurementError(
            f"backup root is not a private directory: {base}")
    if st.st_uid != uid or st.st_mode & 0o077:
        raise MeasurementError(
            f"backup root is not private: {base} "
            f"(uid={st.st_uid}, mode={oct(st.st_mode & 0o777)})")
    try:
        dir_fd = os.open(base, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_DIRECTORY)
    except OSError as e:
        raise MeasurementError(
            f"backup root failed O_NOFOLLOW directory check: {base}: {e}")
    os.close(dir_fd)
    return base


def _contained_restore_target(path_str, root: Path) -> Path | None:
    """§SPEC-070 FR-1：恢复目标须解析后位于 project root 内且叶子非
    symlink——否则返回 None（该记录整体跳过，不写不删）。本检查是快速
    预过滤；实际写入经 §SPEC-071 H2 的 pinned-dirfd 逐组件走廊重新验证。"""
    p = Path(path_str)
    if p.is_symlink():
        return None
    try:
        resolved = p.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _open_pinned_parent(root_fd: int, root: Path,
                        resolved: Path) -> int:
    """§SPEC-071 H2：从已 pin 的 project root dirfd 出发，对
    resolved.parent 的每个组件以 O_NOFOLLOW|O_DIRECTORY 逐级打开——
    containment 检查与写入之间不存在按 pathname 重新解析的窗口：
    containment 后任一组件被换成 root 外 symlink，open 即 ELOOP 失败
    （记录跳过、root 外零改动）；打开完成后 dirfd 钉住原 inode，后续
    竞争换名不影响写入落点。"""
    rel = resolved.relative_to(root)
    cur = os.dup(root_fd)
    try:
        for comp in rel.parts[:-1]:
            nxt = os.open(comp, os.O_RDONLY | os.O_NOFOLLOW
                          | os.O_DIRECTORY, dir_fd=cur)
            os.close(cur)
            cur = nxt
        return cur
    except BaseException:
        os.close(cur)
        raise


def _atomic_write_via_dir_fd(parent_fd: int, name: str, text: str) -> None:
    """§SPEC-070/071：恢复写入原子化——经 pinned parent dirfd 以
    O_CREAT|O_EXCL|O_NOFOLLOW（0600、不可预测随机后缀）创建临时文件并
    os.replace（同 dirfd）：临时名不可抢注、不跟随 symlink、无 pathname
    重解析窗口、崩溃/并发下半写状态不可见。"""
    import secrets

    tmp = f".{name}.gqt-tmp-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _trusted_backup_record(d: Path, uid: int | None,
                           project_root: Path) -> dict | None:
    """§SPEC-071 L1：marker schema 全字段类型校验 + 私有性校验——不可信
    记录返回 None（不写不删、不抛裸异常），坏 marker 不阻塞后续恢复。"""
    marker = d / "started.json"
    if not marker.is_file() or d.is_symlink():
        return None
    try:
        d_st = d.stat()
        m_st = marker.stat()
    except OSError:
        return None
    if uid is not None and (d_st.st_uid != uid
                            or m_st.st_uid != uid):
        return None
    if d_st.st_mode & 0o022:  # 目录开放组/他者写位 → 不可信
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    if record.get("project_root") != str(project_root):
        return None
    files = record.get("files")
    if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in files.items()):
        return None
    created = record.get("created", [])
    if not isinstance(created, list) or not all(
            isinstance(x, str) for x in created):
        return None
    pg = record.get("project_godot")
    if pg is not None and not isinstance(pg, str):
        return None
    return {"files": files, "created": created, "pg": pg}


def _prepare_restore_plan(root_fd: int, root_resolved: Path,
                          record: dict):
    """预过滤（快速拒绝越界/symlink 叶子）+ 全量 parent fd 预开（任一
    组件 walk 失败 → 整记录跳过，零写入并关闭已开 fd）。"""
    files = record["files"]
    created = record["created"]
    pg = record["pg"]
    plan: list[tuple[int, str, str]] = []
    opened: list[int] = []
    trusted = True
    try:
        for src, original in files.items():
            target = _contained_restore_target(src, root_resolved)
            if target is None:
                trusted = False
                break
            pfd = _open_pinned_parent(root_fd, root_resolved,
                                      target)
            opened.append(pfd)
            plan.append((pfd, target.name, original))
        if trusted:
            pg_fd = None
            if pg is not None:
                pg_fd = os.dup(root_fd)
                opened.append(pg_fd)
            created_fds: list[tuple[int, str]] = []
            for c in created:
                target = _contained_restore_target(c, root_resolved)
                if target is None:
                    trusted = False
                    break
                cfd = _open_pinned_parent(root_fd, root_resolved,
                                          target)
                opened.append(cfd)
                created_fds.append((cfd, target.name))
            if not trusted:
                raise OSError("untrusted created target")
    except OSError:
        trusted = False
    if not trusted:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
        return None
    return plan, created_fds, pg_fd, opened


def _apply_restore(plan: list[tuple[int, str, str]],
                   created_fds: list[tuple[int, str]],
                   pg_fd: int | None, pg: str | None,
                   opened: list[int]) -> bool:
    """全部 parent fd 就位后才开始写（all-or-nothing）；无论成败都关闭
    全部 fd。返回是否全部成功（失败时调用方跳过删除/计数）。"""
    trusted = True
    try:
        if pg_fd is not None:
            _atomic_write_via_dir_fd(pg_fd, "project.godot", pg)
        for pfd, name, original in plan:
            _atomic_write_via_dir_fd(pfd, name, original)
        for cfd, name in created_fds:
            try:
                os.unlink(name, dir_fd=cfd)
            except FileNotFoundError:
                pass
    except OSError:
        trusted = False
    finally:
        for fd in opened:
            try:
                os.close(fd)
            except OSError:
                pass
    return trusted


def _recover_stale_backups(project_root: Path) -> int:
    """崩溃恢复：回收本项目的陈旧备份目录（恢复原文、删除备份）。

    §SPEC-070 FR-1：恢复不信任共享目录内容——备份根/目录/marker 须当前
    用户私有（POSIX ownership + 目录无组/他者写位 + 拒绝 symlink）。
    §SPEC-071 H2：写入经 pinned-root 逐组件 O_NOFOLLOW dirfd 走廊，
    containment 检查与写入之间无按 pathname 重新解析窗口，确定性竞态
    注入下 root 外零改动。"""
    restored = 0
    uid = os.getuid() if hasattr(os, "getuid") else None
    base = _backup_base()
    root_resolved = project_root.resolve()
    try:
        root_fd = os.open(root_resolved, os.O_RDONLY | os.O_NOFOLLOW
                          | os.O_DIRECTORY)
    except OSError:
        return 0
    try:
        for d in sorted(base.iterdir()):
            record = _trusted_backup_record(d, uid, project_root)
            if record is None:
                continue
            prepared = _prepare_restore_plan(root_fd, root_resolved,
                                             record)
            if prepared is None:
                continue
            plan, created_fds, pg_fd, opened = prepared
            if not _apply_restore(plan, created_fds, pg_fd,
                                  record["pg"], opened):
                continue
            shutil.rmtree(d, ignore_errors=True)
            restored += 1
    finally:
        os.close(root_fd)
    return restored
