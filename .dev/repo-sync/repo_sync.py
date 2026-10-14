#!/usr/bin/env python3
"""Repo sync logic for bidirectional zero-latency auto-sync.

Four-state model:
  SYNCED       : local == remote
  LOCAL_AHEAD  : local ahead of remote (push)
  REMOTE_AHEAD : remote ahead of local (pull)
  DIVERGED     : both ahead of merge-base (block + alert)

Usage:
  python3 repo_sync.py [--config path] [--direction {bidirectional,push-only}] [--dry-run] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import fcntl


# ---------------------------------------------------------------------------
# Constants / Enums
# ---------------------------------------------------------------------------


class SyncState(str, Enum):
    SYNCED = "SYNCED"
    LOCAL_AHEAD = "LOCAL_AHEAD"
    REMOTE_AHEAD = "REMOTE_AHEAD"
    DIVERGED = "DIVERGED"


class Direction(str, Enum):
    BIDIRECTIONAL = "bidirectional"
    PUSH_ONLY = "push-only"


class ConflictResolution(str, Enum):
    REBASE_FIRST = "rebase_first"
    MERGE_FIRST = "merge_first"
    BLOCK_ONLY = "block_only"


DEFAULT_CONFIG_PATH = Path(__file__).with_name("repo_sync_config.json")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Sentinel replaced at config load time with the machine-specific main repo
# path (GQT_REPO_ROOT env var). Lets repo_sync_config.json stay machine-neutral.
_REPO_ROOT_SENTINEL = "__GQT_REPO_ROOT__"


def _resolve_repo_path(raw: str) -> str:
    """Resolve the repo_path config value, expanding the GQT_REPO_ROOT sentinel."""
    if _REPO_ROOT_SENTINEL in raw:
        root = os.environ.get("GQT_REPO_ROOT", "").strip()
        if not root:
            raise RuntimeError(
                "repo_sync_config.json uses the __GQT_REPO_ROOT__ sentinel but "
                "GQT_REPO_ROOT is not set. Define it via the Environment= line in "
                ".dev/systemd/godot-qa-toolkit-start.service (or export it before "
                "running .dev/launch/godot-qa-toolkit-start.sh)."
            )
        raw = raw.replace(_REPO_ROOT_SENTINEL, root)
    return os.path.expanduser(raw)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Resolve repo_path (may use the __GQT_REPO_ROOT__ sentinel) before deriving
    # relative paths against it.
    repo_root = Path(_resolve_repo_path(cfg["repo_path"]))
    cfg["repo_path"] = str(repo_root)
    for key in ("log_dir", "state_file"):
        val = cfg.get(key, "")
        if val and not Path(val).is_absolute():
            cfg[key] = str(repo_root / val)
    return cfg


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def setup_logging(
    config: dict[str, Any], quiet: bool = False, file_log: bool = True
) -> logging.Logger:
    logger = logging.getLogger("repo_sync")
    logger.setLevel(logging.DEBUG)

    # Clear existing handlers in case of repeated calls
    logger.handlers = []

    if not quiet:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    # File log writes into repo_path (log_dir) — skipped in dry-run mode
    # (SPEC-012: observation must not write into the target checkout).
    if file_log:
        log_dir = Path(config["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "repo-sync.log", encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(file_handler)

    return logger


def write_state(config: dict[str, Any], state: dict[str, Any]) -> None:
    # dry-run 是纯观测语义（SPEC-012）：state 决断不得落盘进 repo_path，
    # 否则对规范检出的验收会留下 .dev/logs/ 未跟踪残留。
    if state.get("dry_run", False):
        return
    state_path = Path(config["state_file"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# File lock (flock)
# ---------------------------------------------------------------------------


def _try_reclaim_stale_lock(fd: int) -> bool:
    """If the lock holder PID is dead, re-acquire the lock non-blocking.

    Returns True when the stale lock was reclaimed (fd now holds the lock).
    """
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        existing = os.read(fd, 32).decode("utf-8").strip()
    except (OSError, ValueError):
        return False
    if not existing.isdigit():
        return False
    try:
        os.kill(int(existing), 0)
    except OSError:
        # PID is dead — stale lock, overwrite (non-blocking)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            return False
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        return True
    return False


def acquire_lock(lock_path: str, timeout: float = 0.0) -> int | None:
    """Acquire exclusive file lock. Returns fd or None on failure.

    When LOCK_NB fails, checks if the lock holder PID is still alive.
    If the PID is dead, treats the lock as stale and re-acquires it.
    """
    if sys.platform == "win32":
        # Windows: use msvcrt for file locking
        import msvcrt

        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            if timeout > 0:
                end = time.time() + timeout
                while time.time() < end:
                    try:
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        return fd
                    except OSError:
                        time.sleep(0.2)
                os.close(fd)
                return None
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return fd
        except OSError:
            return None

    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    if timeout > 0:
        end = time.time() + timeout
        while time.time() < end:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except (OSError, BlockingIOError):
                if _try_reclaim_stale_lock(fd):
                    return fd
                time.sleep(0.2)
        os.close(fd)
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except (OSError, BlockingIOError):
        # Immediate (non-blocking) acquisition failed — check stale
        if _try_reclaim_stale_lock(fd):
            return fd
        os.close(fd)
        return None


def release_lock(fd: int | None, lock_path: str) -> None:
    if fd is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(lock_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git_run(
    repo_path: str, args: list[str], check: bool = True, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = ["git", "-C", repo_path] + args
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "check": check,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if timeout is not None:
        kwargs["timeout"] = timeout
    return subprocess.run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Orphan gitlink helpers
# ---------------------------------------------------------------------------


def detect_orphan_gitlinks(repo_path: str, timeout: int | None = None) -> list[str]:
    """Return list of orphan gitlink paths (mode 160000 but no .gitmodules entry)."""
    orphan_paths: list[str] = []
    r = git_run(repo_path, ["ls-files", "--stage"], check=False, timeout=timeout)
    if r.returncode != 0:
        return orphan_paths

    # Parse registered submodule paths from .gitmodules if present
    registered: set[str] = set()
    gitmodules_path = Path(repo_path) / ".gitmodules"
    if gitmodules_path.is_file():
        try:
            content = gitmodules_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("path"):
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        registered.add(parts[1].strip())
        except OSError:
            pass

    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "160000":
            path = parts[3] if len(parts) > 3 else parts[-1]
            if path not in registered:
                orphan_paths.append(path)

    return orphan_paths


def remove_orphan_gitlink(
    repo_path: str, path: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Remove an orphan gitlink from the index."""
    r = git_run(repo_path, ["rm", "--cached", path], check=False, timeout=timeout)
    if r.returncode == 0:
        return True, f"removed orphan gitlink: {path}"
    return False, f"failed to remove orphan gitlink {path}: {r.stderr.strip()}"


def _is_gitlink_related_error(stderr: str) -> bool:
    """Check if stderr indicates a submodule/gitlink related failure."""
    indicators = ["could not write index", "submodule", "gitlink"]
    err_lower = stderr.lower()
    return any(ind in err_lower for ind in indicators)


def _cleanup_gitlinks_and_retry_stash(
    repo_path: str, stash_cmd: list[str], error_output: str, timeout: int | None = None
) -> subprocess.CompletedProcess[str] | None:
    """If error_output indicates a gitlink issue, clean orphans and retry stash.

    Returns the retry CompletedProcess if cleanup was performed,
    or None if the error was not gitlink-related or no orphans found.
    """
    if not _is_gitlink_related_error(error_output):
        return None
    orphans = detect_orphan_gitlinks(repo_path, timeout=timeout)
    if not orphans:
        return None
    for orphan_path in orphans:
        remove_orphan_gitlink(repo_path, orphan_path, timeout=timeout)
    return git_run(repo_path, stash_cmd, check=False, timeout=timeout)


def _try_stash_with_recovery(
    repo_path: str, stash_on_dirty: bool, timeout: int | None = None
) -> tuple[bool, str, bool]:
    """Attempt stash; on gitlink-related failure, clean orphan gitlinks and retry once."""
    ok, msg, had_stash = do_stash_if_dirty(
        repo_path, stash_on_dirty, recovery=False, timeout=timeout
    )
    if ok or had_stash:
        return ok, msg, had_stash

    # Stash failed — try gitlink cleanup and retry
    stash_cmd = ["stash", "push", "--include-untracked", "-m", "auto-sync-stash"]
    retry = _cleanup_gitlinks_and_retry_stash(
        repo_path, stash_cmd, msg, timeout=timeout
    )
    if retry is not None:
        if retry.returncode == 0:
            return True, "stashed (after gitlink cleanup)", True
        return (
            False,
            f"stash failed after gitlink cleanup: {retry.stderr.strip()}",
            False,
        )

    return ok, msg, had_stash


def _is_transient_network_error(stderr: str) -> bool:
    """Check if stderr indicates a transient network/TLS error worth retrying."""
    indicators = [
        "gnutls",
        "handshake failed",
        "tls connection",
        "could not resolve host",
        "connection timed out",
        "operation timed out",
        "network is unreachable",
        "failed to connect",
        "connection reset",
        "ssl",
    ]
    err_lower = stderr.lower()
    return any(ind in err_lower for ind in indicators)


def classify_error(message: str) -> str:
    """Classify an error as 'transient' or 'persistent' for circuit-breaker logic.

    Transient errors are expected to self-heal on retry (network hiccups,
    temporary file locks, concurrent worktree occupation). Persistent errors
    require manual intervention or code changes (merge conflicts, corrupted
    state, disabled features, in-progress git operations).
    """
    msg = message.lower()

    # Transient indicators (will self-heal on retry)
    transient_indicators = [
        "gnutls",
        "handshake failed",
        "tls connection",
        "could not resolve host",
        "connection timed out",
        "operation timed out",
        "network is unreachable",
        "failed to connect",
        "connection reset",
        "ssl",
        "index.lock",
        "file exists",
        "resource temporarily unavailable",
        "cannot lock ref",
        "could not lock",
        "another worktree",  # concurrent worktree occupation
        "checked out in another worktree",
        "is being checked out",
    ]
    if any(ind in msg for ind in transient_indicators):
        return "transient"

    # Persistent indicators (require human action)
    persistent_indicators = [
        "stash pop conflicted",
        "needs manual resolution",
        "unmerged",
        "diverged",
        "manual intervention",
        "stash_on_dirty disabled",
        "block_only",
        "merge/rebase/cherry-pick in progress",
        "submodule in intermediate",
        "config branch is",
        "switch to master failed",
        "would be overwritten by checkout",
        "untracked working tree files would be overwritten",
        "working tree dirty",
        "stash failed",
    ]
    if any(ind in msg for ind in persistent_indicators):
        return "persistent"

    # Default: treat unknown errors as persistent so they don't loop forever
    return "persistent"


# ---------------------------------------------------------------------------
# Branch guard helpers (SEE-869: ensure main repo is on master before sync)
# ---------------------------------------------------------------------------


def get_current_branch(repo_path: str, timeout: int | None = None) -> str:
    """Return current branch name; 'HEAD' for detached HEAD; '' on error."""
    r = git_run(
        repo_path, ["rev-parse", "--abbrev-ref", "HEAD"], check=False, timeout=timeout
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _resolve_git_dir(repo_path: str) -> Path | None:
    """Resolve the actual .git directory, following worktree gitdir pointers."""
    git_path = Path(repo_path) / ".git"
    if not git_path.exists():
        return None
    if git_path.is_dir():
        return git_path
    # Worktree: .git is a file like 'gitdir: /path/to/main/.git/worktrees/xxx'
    try:
        content = git_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    raw = content[len("gitdir:") :].strip()
    resolved = Path(raw)
    if not resolved.is_absolute():
        resolved = (Path(repo_path) / resolved).resolve()
    return resolved if resolved.is_dir() else None


def has_git_operation_in_progress(repo_path: str) -> bool:
    """Check for in-progress merge/rebase/cherry-pick/revert operations."""
    git_dir = _resolve_git_dir(repo_path)
    if git_dir is None:
        return False
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
        if (git_dir / marker).exists():
            return True
    if (git_dir / "rebase-merge").is_dir() or (git_dir / "rebase-apply").is_dir():
        return True
    return False


def has_submodule_changes(repo_path: str, timeout: int | None = None) -> bool:
    """Detect submodule intermediate state (modified/conflict/uninitialized).

    Only runs if .gitmodules exists. Returns True on '+' (modified), 'U'
    (conflict), or '-' (uninitialized) status from `git submodule status`.
    """
    if not (Path(repo_path) / ".gitmodules").is_file():
        return False
    r = git_run(
        repo_path, ["submodule", "status"], check=False, timeout=timeout
    )
    if r.returncode != 0:
        return False
    for line in r.stdout.splitlines():
        if not line:
            continue
        if line[0] in ("+", "U", "-"):
            return True
    return False


def is_master_worktree_occupied(
    repo_path: str, timeout: int | None = None
) -> tuple[bool, str | None]:
    """Check if master is checked out in another worktree.

    Returns (occupied, other_worktree_path). Only triggers if a worktree OTHER
    than repo_path has refs/heads/master as its checked-out branch.
    """
    r = git_run(
        repo_path, ["worktree", "list", "--porcelain"], check=False, timeout=timeout
    )
    if r.returncode != 0:
        return False, None
    try:
        current = Path(repo_path).resolve()
    except OSError:
        current = Path(repo_path)

    lines = r.stdout.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("worktree "):
            i += 1
            continue
        wt_path_str = line[len("worktree ") :].strip()
        branch: str | None = None
        i += 1
        while i < len(lines) and lines[i].strip():
            sub = lines[i]
            if sub.startswith("branch "):
                branch = sub[len("branch ") :].strip()
            i += 1
        if branch == "refs/heads/master":
            try:
                wt_path = Path(wt_path_str).resolve()
            except OSError:
                wt_path = Path(wt_path_str)
            if wt_path != current:
                return True, wt_path_str
    return False, None


def switch_to_master(
    repo_path: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Switch to master; create from origin/master if local master absent."""
    r = git_run(repo_path, ["switch", "master"], check=False, timeout=timeout)
    if r.returncode == 0:
        return True, "switched to master"
    err = r.stderr.strip() if r.stderr else ""
    lowered = err.lower()
    if (
        "invalid reference" in lowered
        or "did not match" in lowered
        or "unknown revision" in lowered
    ):
        r2 = git_run(
            repo_path,
            ["switch", "-c", "master", "origin/master"],
            check=False,
            timeout=timeout,
        )
        if r2.returncode == 0:
            return True, "created master from origin/master"
        return (
            False,
            r2.stderr.strip() if r2.stderr else "failed to create master from origin/master",
        )
    return False, err


def ensure_on_master_branch(
    repo_path: str,
    branch: str,
    stash_on_dirty: bool,
    dry_run: bool,
    logger: logging.Logger,
    timeout: int | None = None,
) -> tuple[bool, str, bool]:
    """Ensure repo is on the configured branch (master) before sync.

    Implements Archi's boundary-case matrix (SEE-869 Phase 2b):
      1. Config branch must be 'master' (else persistent failure)
      2. Already on master → no-op
      3. In-progress merge/rebase/cherry-pick/revert → persistent failure
      4. Submodule intermediate state → persistent failure
      5. Master checked out in another worktree → transient skip
      6. Dirty tree → stash once (or fail if stash_on_dirty disabled)
      7. Switch to master (handles detached HEAD and other branches)

    Returns (ok, message, had_stash). had_stash=True means the caller must
    `do_stash_pop()` after the sync body completes.

    dry_run=True: only detection and reporting — no stash/switch/pop.
    """
    # 1. Config validation (#10)
    if branch != "master":
        return (
            False,
            f"config branch is '{branch}'; only 'master' is supported for main-repo protection",
            False,
        )

    # 2. Current branch detection
    current = get_current_branch(repo_path, timeout=timeout)
    if not current:
        return False, "could not detect current branch", False
    if current == "master":
        return True, "already on master", False

    blocked, reason = _master_guard_blockers(repo_path, timeout=timeout)
    if blocked:
        return False, reason, False

    # 5. Worktree occupation (#7) — transient skip
    occupied, wt_path = _master_occupied_elsewhere(repo_path, timeout=timeout)
    if occupied:
        return False, wt_path, False

    # 6. Dirty tree stash (#1, pre-switch)
    return _stash_and_switch(
        repo_path, current, stash_on_dirty, dry_run, logger, timeout=timeout
    )


def _master_guard_blockers(
    repo_path: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Blocking preconditions for the branch guard (boundary matrix #3/#4).

    Returns (blocked, reason). Worktree occupation and dirty-tree cases are
    handled by the caller (they are skip/stash cases, not hard blockers).
    """
    # 3. In-progress operation (#3)
    if has_git_operation_in_progress(repo_path):
        return (
            True,
            "merge/rebase/cherry-pick in progress; abort required before switching",
        )
    # 4. Submodule intermediate state (#4)
    if has_submodule_changes(repo_path, timeout=timeout):
        return (
            True,
            "submodule in intermediate state; manual cleanup required",
        )
    return False, ""


def _master_occupied_elsewhere(
    repo_path: str, timeout: int | None = None
) -> tuple[bool, str]:
    occupied, wt_path = is_master_worktree_occupied(repo_path, timeout=timeout)
    if not occupied:
        return False, ""
    return True, (
        f"master is checked out in another worktree ({wt_path}); skipping"
    )


def _stash_and_switch(
    repo_path: str,
    current: str,
    stash_on_dirty: bool,
    dry_run: bool,
    logger: logging.Logger,
    timeout: int | None = None,
) -> tuple[bool, str, bool]:
    """Dirty-tree stash + switch to master (boundary matrix #6/#7)."""
    is_dirty = is_working_tree_dirty(repo_path, timeout=timeout)
    if is_dirty and not stash_on_dirty:
        return False, "working tree dirty (stash_on_dirty disabled)", False

    # dry_run: report intent, do not mutate repo state
    if dry_run:
        action = "stash dirty tree and switch" if is_dirty else "switch"
        return (
            True,
            f"dry-run: would {action} from {current} to master",
            False,
        )

    had_stash = False
    if is_dirty:
        stash_ok, stash_msg, had_stash = do_stash_if_dirty(
            repo_path, stash_on_dirty, timeout=timeout
        )
        if not stash_ok:
            return False, f"stash failed: {stash_msg}", False
        if had_stash and has_submodule_changes(repo_path, timeout=timeout):
            do_stash_pop(repo_path, timeout=timeout)
            return (
                False,
                "submodule in intermediate state; manual cleanup required",
                False,
            )

    # 7. Switch to master (handles detached HEAD #2 and switch failure #9)
    switch_ok, switch_msg = switch_to_master(repo_path, timeout=timeout)
    if not switch_ok:
        if had_stash:
            do_stash_pop(repo_path, timeout=timeout)
        return False, f"switch to master failed: {switch_msg}", False

    logger.info("branch guard: switched from %s to master", current)
    return True, f"switched from {current} to master", had_stash



def git_fetch(
    repo_path: str, remote: str, timeout: int | None = None
) -> tuple[bool, str]:
    # --prune removes stale remote-tracking refs (e.g. remotes/origin/agent/*)
    # when they are deleted on the remote. SEE-1033
    r = git_run(repo_path, ["fetch", "--prune", remote], check=False, timeout=timeout)
    return r.returncode == 0, r.stderr.strip() if r.stderr else r.stdout.strip()


def git_fetch_with_retry(
    repo_path: str,
    remote: str,
    timeout: int | None = None,
    retries: int = 3,
    retry_delay: float = 2.0,
) -> tuple[bool, str]:
    """Fetch with retry on transient network errors."""
    for attempt in range(1, retries + 1):
        ok, err = git_fetch(repo_path, remote, timeout=timeout)
        if ok:
            return True, err
        if _is_transient_network_error(err) and attempt < retries:
            time.sleep(retry_delay)
            continue
        return ok, err
    return False, err


def get_sha(repo_path: str, ref: str, timeout: int | None = None) -> str | None:
    r = git_run(repo_path, ["rev-parse", ref], check=False, timeout=timeout)
    return r.stdout.strip() if r.returncode == 0 else None


def detect_sync_state(
    repo_path: str, remote: str, branch: str, timeout: int | None = None
) -> SyncState:
    local_sha = get_sha(repo_path, branch, timeout=timeout)
    remote_sha = get_sha(repo_path, f"{remote}/{branch}", timeout=timeout)

    if not local_sha or not remote_sha:
        return SyncState.DIVERGED

    if local_sha == remote_sha:
        return SyncState.SYNCED

    r = git_run(
        repo_path,
        ["merge-base", branch, f"{remote}/{branch}"],
        check=False,
        timeout=timeout,
    )
    base_sha = r.stdout.strip() if r.returncode == 0 else None
    if not base_sha:
        return SyncState.DIVERGED

    if local_sha == base_sha:
        return SyncState.REMOTE_AHEAD
    if remote_sha == base_sha:
        return SyncState.LOCAL_AHEAD
    return SyncState.DIVERGED


def is_working_tree_dirty(repo_path: str, timeout: int | None = None) -> bool:
    r = git_run(repo_path, ["status", "--porcelain"], check=False, timeout=timeout)
    return bool(r.stdout.strip())


def do_pull_ff_only(
    repo_path: str,
    remote: str,
    branch: str,
    stash_on_dirty: bool,
    timeout: int | None = None,
) -> tuple[bool, str]:
    had_stash = False
    stash_cmd = ["stash", "push", "--include-untracked", "-m", "auto-sync-stash"]
    if is_working_tree_dirty(repo_path, timeout=timeout):
        if not stash_on_dirty:
            return False, "working tree dirty (stash_on_dirty disabled)"
        r = git_run(repo_path, stash_cmd, check=False, timeout=timeout)
        if r.returncode != 0:
            stderr = r.stderr.strip() if r.stderr else ""
            retry = _cleanup_gitlinks_and_retry_stash(
                repo_path, stash_cmd, stderr, timeout=timeout
            )
            if retry is not None:
                if retry.returncode != 0:
                    return (
                        False,
                        f"stash failed after gitlink cleanup: {retry.stderr.strip()}",
                    )
                had_stash = True
            else:
                return False, f"stash failed: {stderr}"
        else:
            had_stash = True

    try:
        r = git_run(
            repo_path,
            ["pull", "--ff-only", remote, branch],
            check=False,
            timeout=timeout,
        )
        pull_ok = r.returncode == 0
        pull_msg = (
            "pull success" if pull_ok else f"pull --ff-only failed: {r.stderr.strip()}"
        )
    except Exception as e:
        pull_ok = False
        pull_msg = f"pull exception: {e}"

    # Submodule content must follow the new pointers (SEE-1268) — a pointer-only
    # pull leaves the child working trees stale, which downstream consumers read
    # as a failed sync. Best-effort: record failure without discarding the pull.
    if pull_ok:
        sub_ok, sub_msg = do_submodule_update(repo_path, timeout=timeout)
        pull_msg = f"{pull_msg}; {sub_msg}"
        if not sub_ok:
            pull_ok = False

    if had_stash:
        r = git_run(repo_path, ["stash", "pop"], check=False, timeout=timeout)
        if r.returncode != 0:
            if pull_ok:
                return (
                    False,
                    "pull success but stash pop conflicted, needs manual resolution",
                )
            else:
                return False, f"{pull_msg}; additionally stash pop failed"

    return pull_ok, pull_msg


def do_submodule_update(repo_path: str, timeout: int | None = None) -> tuple[bool, str]:
    """Materialize submodule content to match recorded pointers (SEE-1268).

    Pointer-level ops (pull/push/rebase) move gitlinks but never populate the
    child working trees, so a synced master would still miss the godot-mcp
    addon content (the only remaining submodule — gqt split as a standalone
    repo, SEE-1396). Runs only when .gitmodules exists; `--init` also clears
    the '-'
    uninitialized status that has_submodule_changes() treats as a sync-blocking
    intermediate state.

    Self-healing: the vendored→submodule migration leaves the pre-switch files
    on disk, and `git submodule update --init` refuses to clone into a
    non-empty directory — it then fails on EVERY cycle, which is how D-drive
    master ended up without addons/godot_mcp/plugin.cfg. On that specific
    failure the residue is MOVED ASIDE (never deleted) and the init retried
    once; the message names what moved so the condition is never silent.
    """
    if not (Path(repo_path) / ".gitmodules").is_file():
        return True, "no submodules (.gitmodules absent)"

    ok, err = _run_submodule_init(repo_path, timeout)
    if ok:
        return True, "submodule update --init --recursive success"

    if not _is_residue_blocked(err):
        # Not the migration class — a real fetch/URL failure. Surface it.
        return False, f"submodule update --init --recursive failed: {err}"

    moved_ok, moved_msg = _recover_submodule_residue(repo_path, timeout)
    if not moved_ok:
        return False, (
            f"submodule update --init --recursive failed ({err}); "
            f"residue recovery failed: {moved_msg}"
        )

    ok2, err2 = _run_submodule_init(repo_path, timeout)
    if not ok2:
        return False, (
            f"submodule update --init --recursive still failing after moving "
            f"residue aside ({moved_msg}): {err2}"
        )
    return True, f"submodule update success after residue recovery ({moved_msg})"


def _run_submodule_init(repo_path: str, timeout: int | None) -> tuple[bool, str]:
    r = git_run(
        repo_path,
        ["submodule", "update", "--init", "--recursive"],
        check=False,
        timeout=timeout,
    )
    return r.returncode == 0, (r.stderr.strip() if r.stderr else r.stdout.strip())


# Git's clone-into-populated-directory signatures. `--force` does NOT help here
# (verified: it re-fails), so the only recovery is clearing the path.
_RESIDUE_MARKERS = ("not an empty directory", "failed to clone")


def _is_residue_blocked(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _RESIDUE_MARKERS)


def _uninitialized_submodule_paths(repo_path: str, timeout: int | None) -> list[str]:
    """Paths git reports as '-' (registered but not checked out)."""
    r = git_run(repo_path, ["submodule", "status"], check=False, timeout=timeout)
    if r.returncode != 0:
        return []
    paths: list[str] = []
    for line in r.stdout.splitlines():
        if not line.startswith("-"):
            continue
        # Format: "-<sha> <path>[ (describe)]"
        parts = line[1:].split()
        if len(parts) >= 2:
            paths.append(parts[1])
    return paths


def _recover_submodule_residue(repo_path: str, timeout: int | None) -> tuple[bool, str]:
    """Move non-empty uninitialized submodule paths aside so --init can clone.

    Reversible by construction: the residue is relocated to
    .dev/logs/submodule-residue/<path>-<epoch>/ inside the repo (gitignored),
    never removed. Best-effort `deinit` first drops any stale registration.
    """
    paths = _uninitialized_submodule_paths(repo_path, timeout)
    if not paths:
        return False, "no uninitialized submodule path to recover"

    backup_root = Path(repo_path) / ".dev" / "logs" / "submodule-residue"
    moved: list[str] = []
    for rel in paths:
        target = Path(repo_path) / rel
        if not target.exists():
            continue
        if not any(target.iterdir()):
            # Empty dir: git would have cloned fine, so this is not the blocker.
            continue
        dest = backup_root / f"{rel.replace('/', '_')}-{int(time.time())}"
        try:
            # Move the working-tree residue FIRST: `git submodule deinit -f`
            # empties the submodule directory, so running it before the move
            # would destroy the very files this recovery promises to preserve.
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(dest.name + f"-{os.getpid()}")
            shutil.move(str(target), str(dest))
            git_run(repo_path, ["submodule", "deinit", "-f", "--", rel], check=False, timeout=timeout)
            # A stale .git/modules/<path> registration makes the re-clone fail
            # even with the worktree dir gone (verified against git ≥2.38), so
            # relocate it too — same reversible treatment as the worktree dir.
            stale_modules = _git_path(repo_path, f"modules/{rel}", timeout)
            if stale_modules and stale_modules.exists():
                shutil.move(str(stale_modules), str(dest) + ".gitmodules")
        except OSError as e:
            return False, f"could not move residue for {rel} to {dest}: {e}"
        moved.append(f"{rel} -> {dest}")

    if not moved:
        return False, "residue recovery found nothing to move (init failure not path-related)"
    return True, "; ".join(moved)


def _git_path(repo_path: str, rel: str, timeout: int | None) -> Path | None:
    """Resolve a path inside the repo's git dir (worktree-safe)."""
    r = git_run(repo_path, ["rev-parse", "--git-path", rel], check=False, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else Path(repo_path) / p


def do_push(
    repo_path: str, remote: str, branch: str, timeout: int | None = None
) -> tuple[bool, str]:
    # GAP-1 (SEE-1268): a parent commit whose gitlinks point at SHAs absent
    # from the submodule remotes clones dead for everyone else. Verify before
    # pushing; refusal is loud, naming the submodule + SHA (禁静默失败).
    guard_ok, guard_msg = ensure_gitlinks_reachable(repo_path, timeout=timeout)
    if not guard_ok:
        return False, f"push blocked: dangling gitlink — {guard_msg}"

    r = git_run(repo_path, ["push", remote, branch], check=False, timeout=timeout)
    if r.returncode != 0:
        return False, f"push failed: {r.stderr.strip()}"
    return True, "push success"


def _looks_like_not_our_ref(output: str) -> bool:
    low = output.lower()
    return "not our ref" in low or "no such ref" in low


def _sha_reachable_via_tips(
    child_wt: str, url: str, sha: str, probe_timeout_s: int
) -> bool:
    """True when `sha` is an ancestor of some tip on the remote (or the tip
    itself). Uses ls-remote tips + local ancestry checks against the child's
    own object store — transport-independent, no remote history download."""
    r = git_run(child_wt, ["ls-remote", url], check=False, timeout=probe_timeout_s)
    if r.returncode != 0:
        # Can't see the remote at all — not a dangling verdict; let the push
        # proceed (the push itself will fail loudly if the remote is broken).
        return True
    for line in r.stdout.splitlines():
        tip_sha = line.split("\t", 1)[0].strip()
        if tip_sha == sha:
            return True
        # merge-base needs both objects locally; a tip that is newer than our
        # last child fetch is NOT yet in the object store, making every
        # merge-base fail → a false "dangling" verdict that fail-closes gate2
        # (SEE-1292 production case: squash-merged 0221ae0 absent locally for
        # the entire circuit-breaker window). Remedy: fetch the tip once (shallow
        # unbounded fetch of a remote tip is bounded by the repo's real history)
        # before the ancestry probe; genuine fetch failure keeps fail-close.
        m = git_run(child_wt, ["merge-base", "--is-ancestor", sha, tip_sha],
                    check=False)
        if m.returncode != 0:
            f = git_run(child_wt, ["fetch", "--quiet", url, tip_sha],
                        check=False, timeout=probe_timeout_s)
            if f.returncode != 0:
                continue
            m = git_run(child_wt, ["merge-base", "--is-ancestor", sha, tip_sha],
                        check=False)
        if m.returncode == 0:
            return True
    return False


def _iter_gitlinks(repo_path: str, timeout: int | None) -> list[tuple[str, str]]:
    """Yield (path, sha) for every gitlink recorded in the index."""
    r = git_run(repo_path, ["ls-files", "--stage"], check=False, timeout=timeout)
    links: list[tuple[str, str]] = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "160000":
            links.append((parts[3], parts[1]))
    return links


def ensure_gitlinks_reachable(
    repo_path: str,
    timeout: int | None = None,
    probe_timeout_s: int = 30,
    recursive: bool = False,
    _path_prefix: str = "",
) -> tuple[bool, str]:
    """Verify every gitlink's SHA is fetchable from its submodule remote (GAP-1).

    Technique (probed against git 2.x, file & https transports):
      `git fetch --dry-run <remote> <sha>` inside the child worktree —
      rc=0 → the remote can serve that commit; rc!=0 with "not our ref"
      → the commit is missing upstream (dangling).

    Skips gracefully when: no .gitmodules, child worktree not initialized
    ('-' status — repo-sync's materialize owns that state), or the child's
    remote cannot be resolved. probe_timeout_s bounds each network round-trip
    so a hung remote cannot stall the sync loop.

    (c) 递归骨架（SEE-1268 §7.4(c)）：recursive=True 且某子库工作区有自己的
    .gitmodules 时对其递归一次（深度限 1 层——递归调用强制 recursive=False，
    避免无限下钻）；dangling 文案经 _path_prefix 带父路径前缀。默认 False，
    与既有行为零变化（两子库均无嵌套，走不到递归分支）。
    """
    if not (Path(repo_path) / ".gitmodules").is_file():
        return True, "no submodules (.gitmodules absent)"

    links = _iter_gitlinks(repo_path, timeout)
    if not links:
        return True, "no gitlinks in index"

    for path_rel, sha in links:
        child = Path(repo_path) / path_rel
        # '-' uninitialized: no local child repo; the materialize step owns it.
        if not (child / ".git").exists():
            continue
        # Resolve the child's configured origin URL (not the .gitmodules URL —
        # worktree-local `remote.origin.url` is what a push from here would use).
        r = git_run(str(child), ["remote", "get-url", "origin"],
                    check=False, timeout=timeout)
        if r.returncode != 0 or not r.stdout.strip():
            continue
        url = r.stdout.strip()
        display = f"{_path_prefix}{path_rel}"

        r = git_run(str(child), ["fetch", "--dry-run", "--quiet", url, sha],
                    check=False, timeout=probe_timeout_s)
        if r.returncode != 0 and _looks_like_not_our_ref(r.stderr + r.stdout):
            # Authoritative on http remotes: the server refused to serve the sha.
            err = (r.stderr.strip() or r.stdout.strip())[:200]
            return False, (
                f"submodule '{display}' commit {sha} is NOT on its remote "
                f"({url}) — 先补推子库再推父仓 / push the submodule first. "
                f"detail: {err}"
            )
        # fetch succeeded — but on file-protocol remotes that success is not
        # proof (local transport resolves objects the remote lacks, probed
        # against git 2.x). Cross-check ancestry: the gitlink must be an
        # ancestor of some remote tip, else treat as dangling.
        if not _sha_reachable_via_tips(str(child), url, sha, probe_timeout_s):
            return False, (
                f"submodule '{display}' commit {sha} is NOT an ancestor of any "
                f"tip on its remote ({url}) — 先补推子库再推父仓 / push the "
                f"submodule first."
            )

        # (c) 递归：本子库通过后，若启用递归且它有嵌套 .gitmodules，下钻一次。
        if recursive and (child / ".gitmodules").is_file():
            nested_ok, nested_msg = ensure_gitlinks_reachable(
                str(child),
                timeout=timeout,
                probe_timeout_s=probe_timeout_s,
                recursive=False,  # 深度限 1 层
                _path_prefix=f"{display}/",
            )
            if not nested_ok:
                return False, nested_msg
    return True, "all gitlink SHAs reachable from their submodule remotes"



def do_stash_if_dirty(
    repo_path: str,
    stash_on_dirty: bool,
    recovery: bool = True,
    timeout: int | None = None,
) -> tuple[bool, str, bool]:
    """Stash dirty working tree. Returns (ok, msg, had_stash).

    When recovery=True and stash fails with a gitlink-related error,
    automatically cleans orphan gitlinks and retries once.
    """
    if not is_working_tree_dirty(repo_path, timeout=timeout):
        return True, "", False
    if not stash_on_dirty:
        return False, "working tree dirty (stash_on_dirty disabled)", False
    stash_cmd = ["stash", "push", "--include-untracked", "-m", "auto-sync-stash"]
    r = git_run(repo_path, stash_cmd, check=False, timeout=timeout)
    if r.returncode != 0:
        stderr = r.stderr.strip() if r.stderr else ""
        if recovery:
            retry = _cleanup_gitlinks_and_retry_stash(
                repo_path, stash_cmd, stderr, timeout=timeout
            )
            if retry is not None and retry.returncode == 0:
                return True, "stashed (after gitlink cleanup)", True
        return False, f"stash failed: {stderr}", False
    return True, "stashed", True


def do_stash_pop(repo_path: str, timeout: int | None = None) -> tuple[bool, str]:
    r = git_run(repo_path, ["stash", "pop"], check=False, timeout=timeout)
    if r.returncode != 0:
        return False, f"stash pop failed: {r.stderr.strip()}"
    return True, "stash pop success"


def do_rebase(
    repo_path: str, remote: str, branch: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Rebase local branch onto remote. Returns (ok, msg)."""
    r = git_run(
        repo_path, ["rebase", f"{remote}/{branch}"], check=False, timeout=timeout
    )
    if r.returncode == 0:
        return True, "rebase success"
    return False, f"rebase failed: {r.stderr.strip()}"


def do_rebase_abort(repo_path: str, timeout: int | None = None) -> tuple[bool, str]:
    r = git_run(repo_path, ["rebase", "--abort"], check=False, timeout=timeout)
    if r.returncode == 0:
        return True, "rebase aborted"
    return False, f"rebase abort failed: {r.stderr.strip()}"


def do_pull_merge(
    repo_path: str, remote: str, branch: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Pull with merge strategy (--no-rebase --no-edit). Returns (ok, msg)."""
    r = git_run(
        repo_path,
        ["pull", "--no-rebase", "--no-edit", remote, branch],
        check=False,
        timeout=timeout,
    )
    if r.returncode == 0:
        return True, "merge pull success"
    return False, f"merge pull failed: {r.stderr.strip()}"


def resolve_diverged(
    repo_path: str,
    remote: str,
    branch: str,
    stash_on_dirty: bool,
    strategy: ConflictResolution,
    logger: logging.Logger,
    dry_run: bool = False,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Attempt automatic conflict resolution for DIVERGED state.

    Strategies:
      rebase_first: rebase → push; on conflict → abort → merge → push; on merge conflict → block
      merge_first:  merge → push; on conflict → block
      block_only:   immediate block + alert
    """
    result: dict[str, Any] = {
        "action": "resolve_diverged",
        "state": "DIVERGED",
        "success": False,
        "message": "",
        "resolution_steps": [],
        "dry_run": dry_run,
    }

    if dry_run:
        result["message"] = (
            f"dry-run: would attempt {strategy.value} conflict resolution"
        )
        return result

    if strategy == ConflictResolution.BLOCK_ONLY:
        result["message"] = (
            "DIVERGED: block_only strategy, manual intervention required"
        )
        return result

    # Stash dirty working tree before rebase/merge operations
    stash_ok, stash_msg, had_stash = do_stash_if_dirty(
        repo_path, stash_on_dirty, timeout=timeout
    )
    if not stash_ok:
        result["message"] = f"DIVERGED: cannot proceed — {stash_msg}"
        return result
    if had_stash:
        result["resolution_steps"].append("stash")

    try:
        if strategy == ConflictResolution.REBASE_FIRST:
            result = _resolve_rebase_first(
                repo_path, remote, branch, result, logger, timeout=timeout
            )
        elif strategy == ConflictResolution.MERGE_FIRST:
            result = _resolve_merge_first(
                repo_path, remote, branch, result, logger, timeout=timeout
            )
    finally:
        if had_stash:
            pop_ok, pop_msg = do_stash_pop(repo_path, timeout=timeout)
            result["resolution_steps"].append(
                f"stash_pop({'ok' if pop_ok else 'fail'})"
            )
            if not pop_ok and result["success"]:
                result["success"] = False
                result["message"] += f"; {pop_msg}"

    return result


def _resolve_rebase_first(
    repo_path: str,
    remote: str,
    branch: str,
    result: dict[str, Any],
    logger: logging.Logger,
    timeout: int | None = None,
) -> dict[str, Any]:
    ok, msg = do_rebase(repo_path, remote, branch, timeout=timeout)
    result["resolution_steps"].append(f"rebase({'ok' if ok else 'fail'})")

    if ok:
        logger.info("rebase succeeded for DIVERGED resolution")
        push_ok, push_msg = do_push(repo_path, remote, branch, timeout=timeout)
        result["resolution_steps"].append(f"push({'ok' if push_ok else 'fail'})")
        if push_ok:
            sub_ok, sub_msg = do_submodule_update(repo_path, timeout=timeout)
            result["resolution_steps"].append(f"submodule_update({'ok' if sub_ok else 'fail'})")
            result["success"] = sub_ok
            if not sub_ok:
                result["message"] = f"resolved DIVERGED via rebase: {push_msg}; submodule content not materialized: {sub_msg}"
            else:
                result["message"] = f"resolved DIVERGED via rebase: {push_msg}; submodule content materialized"
        else:
            result["message"] = f"rebase succeeded but push failed: {push_msg}"
        return result

    logger.warning("rebase failed, attempting abort then merge fallback: %s", msg)
    abort_ok, abort_msg = do_rebase_abort(repo_path, timeout=timeout)
    result["resolution_steps"].append(f"rebase_abort({'ok' if abort_ok else 'fail'})")

    merge_ok, merge_msg = do_pull_merge(repo_path, remote, branch, timeout=timeout)
    result["resolution_steps"].append(f"merge({'ok' if merge_ok else 'fail'})")

    if merge_ok:
        logger.info("merge fallback succeeded for DIVERGED resolution")
        push_ok, push_msg = do_push(repo_path, remote, branch, timeout=timeout)
        result["resolution_steps"].append(f"push({'ok' if push_ok else 'fail'})")
        if push_ok:
            sub_ok, sub_msg = do_submodule_update(repo_path, timeout=timeout)
            result["resolution_steps"].append(f"submodule_update({'ok' if sub_ok else 'fail'})")
            result["success"] = sub_ok
            if not sub_ok:
                result["message"] = f"resolved DIVERGED via merge fallback: {push_msg}; submodule content not materialized: {sub_msg}"
            else:
                result["message"] = f"resolved DIVERGED via merge fallback: {push_msg}; submodule content materialized"
        else:
            result["message"] = f"merge succeeded but push failed: {push_msg}"
        return result

    result["message"] = (
        "DIVERGED: rebase and merge both failed, manual intervention required"
    )
    return result


def _resolve_merge_first(
    repo_path: str,
    remote: str,
    branch: str,
    result: dict[str, Any],
    logger: logging.Logger,
    timeout: int | None = None,
) -> dict[str, Any]:
    merge_ok, merge_msg = do_pull_merge(repo_path, remote, branch, timeout=timeout)
    result["resolution_steps"].append(f"merge({'ok' if merge_ok else 'fail'})")

    if merge_ok:
        logger.info("merge succeeded for DIVERGED resolution")
        push_ok, push_msg = do_push(repo_path, remote, branch, timeout=timeout)
        result["resolution_steps"].append(f"push({'ok' if push_ok else 'fail'})")
        if push_ok:
            sub_ok, sub_msg = do_submodule_update(repo_path, timeout=timeout)
            result["resolution_steps"].append(f"submodule_update({'ok' if sub_ok else 'fail'})")
            result["success"] = sub_ok
            if not sub_ok:
                result["message"] = f"resolved DIVERGED via merge: {push_msg}; submodule content not materialized: {sub_msg}"
            else:
                result["message"] = f"resolved DIVERGED via merge: {push_msg}; submodule content materialized"
        else:
            result["message"] = f"merge succeeded but push failed: {push_msg}"
        return result

    result["message"] = "DIVERGED: merge failed, manual intervention required"
    return result


# ---------------------------------------------------------------------------
# SEE-1285: gitlink auto-forward (three-gate, fail-close)
# ---------------------------------------------------------------------------

# Gate 1 maps a GitHub Actions check-run conclusion set to a three-state
# verdict. Anything not provably green/fail lands on "unknown" and is treated
# as not satisfied (fail-close) with backoff retry on the next cycle.
_CI_SUCCESS = "success"


def classify_ci_conclusion(job_conclusions: list[dict[str, Any]] | None) -> str:
    """Classify child CI verdict as 'success' | 'fail' | 'unknown'.

    `job_conclusions` is the list of check-run dicts for the child's
    build-and-test workflow on the pushed branch. success requires ALL
    conclusions to be success; any explicit non-success conclusion is 'fail';
    running/missing/error → 'unknown' (retry with backoff next cycle).
    """
    if not job_conclusions:
        return "unknown"
    verdicts = [
        (j.get("conclusion") or "").lower() for j in job_conclusions
    ]
    if any(v in ("failure", "timed_out", "cancelled", "startup_failure") for v in verdicts):
        return "fail"
    if all(v == _CI_SUCCESS for v in verdicts):
        return "success"
    return "unknown"


def _select_gating_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pick the gating workflow check-runs for Gate 1.

    A fork push produces publish/release check-runs (`skipped`) beside the
    `build-and-test` one (ci.yml). Pool-wide classification would turn any
    `skipped` neighbour into 'unknown' even when CI is green (D2, HIGH).
    Filter to the gating check-run when present; fall back to the full pool
    when absent — fail-close either way.
    """
    if not runs:
        return runs
    gated = [
        r for r in runs
        if isinstance(r, dict) and r.get("name") == "build-and-test"
    ]
    return gated if gated else runs


def default_ci_query(child_remote_url: str, branch: str, sha: str) -> str:
    """Query the child repo's CI conclusion via GitHub API (Gate 1 producer).

    Uses `gh api` (authenticated CLI, the same tool the platform runs). The
    repo is derived from the child remote URL. Any failure (no gh, network,
    repo not on GitHub) yields 'unknown' — never raises.
    """
    if "github.com" not in child_remote_url:
        return "unknown"
    slug = child_remote_url.rstrip("/")
    for prefix in ("https://github.com/", "git@github.com:"):
        if slug.startswith(prefix):
            slug = slug[len(prefix):]
            break
    if slug.endswith(".git"):
        slug = slug[:-4]
    try:
        r = subprocess.run(
            ["gh", "api",
             f"repos/{slug}/commits/{sha}/check-runs?per_page=100",
             "--jq", "[.check_runs[] | {name, conclusion, status}]"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        if r.returncode != 0:
            return "unknown"
        runs = json.loads(r.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError, OSError):
        return "unknown"
    # build-and-test is the child repo's gating workflow (ci.yml); when it is
    # absent fall back to the full set — fail-close either way.
    return classify_ci_conclusion(_select_gating_runs(runs))


def verify_lag_interval_integrity(
    child_worktree: str, old_sha: str, new_sha: str, timeout: int | None = None
) -> tuple[bool, str]:
    """Gate 3 — the lag interval must have decidable history: new_sha must
    contain old_sha as an ancestor (single merge-base --is-ancestor check).
    A squash/rebase/force-push rewrites the interval, breaking ancestry:
    old_sha unreachable from new_sha makes the whole interval suspect —
    fail-close, never forward.

    Returns (ok, first_bad_sha). ok=False → fail-close, never forward.
    """
    if old_sha == new_sha:
        return True, ""
    m = git_run(
        child_worktree, ["merge-base", "--is-ancestor", old_sha, new_sha],
        check=False, timeout=timeout,
    )
    if m.returncode == 0:
        return True, ""
    return False, old_sha


def evaluate_forward_gates(
    parent_repo: str,
    sub_path: str,
    child_remote_url: str,
    child_branch: str,
    new_sha: str,
    ci_conclusion: str,
    timeout: int | None = None,
) -> tuple[bool, str | None, str]:
    """Run the three SEE-1285 gates in order; first failure wins.

    Gate 1 (CI): ci_conclusion must be exactly 'success' — unknown/fail/empty
      all block (fail-close, backoff retry on next cycle for unknown).
    Gate 2 (pointer health): delegates to the GAP-1 check
      `ensure_gitlinks_reachable` — reused, never reimplemented or bypassed.
    Gate 3 (interval integrity): parent gitlink → new_sha must be a
      contiguous history segment in the child.

    Returns (allowed, failing_gate_or_None, message).
    """
    # Gate 1 — child CI green (three-state: success / fail / unknown)
    if ci_conclusion != _CI_SUCCESS:
        return False, "gate1", (
            f"gate1 not satisfied: child CI conclusion={ci_conclusion or 'unknown'!r} "
            f"(success required; unknown → backoff retry)"
        )
    # Gate 2 — pointer health via GAP-1 (reuse, never bypass)
    guard_ok, guard_msg = ensure_gitlinks_reachable(
        parent_repo, timeout=timeout
    )
    if not guard_ok:
        return False, "gate2", f"gate2 not satisfied (GAP-1): {guard_msg}"

    # Gate 3 — interval integrity
    child_wt = str(Path(parent_repo) / sub_path)
    if (Path(child_wt) / ".git").exists():
        old_sha = _iter_gitlinks(parent_repo, timeout)
        old = next((sha for p, sha in old_sha if p == sub_path), None)
        if old:
            # G-1（生产观察）：materialize 滞后时子对象库缺新 head 对象，
            # merge-base 对不可判定提交返回非 0，被误判为区间抹除 fail-close。
            # 判定前先 fetch 目标 sha 到本地对象库；fetch 失败维持 fail-close
            # （对象不可得 = 不可判定 → 安全侧）。不动工作树 checkout 状态
            # （仅 fetch，不动 materialize；checkout 对齐由 forward 段处理）。
            f = git_run(child_wt, ["fetch", "--quiet", child_remote_url, new_sha],
                        check=False, timeout=timeout)
            if f.returncode != 0:
                return False, "gate3", (
                    f"gate3 not satisfied: cannot fetch {new_sha[:12]} from "
                    f"{child_remote_url} (object unavailable); fail-close"
                )
            ok, bad = verify_lag_interval_integrity(child_wt, old, new_sha, timeout=timeout)
            if not ok:
                return False, "gate3", (
                    f"gate3 not satisfied: interval history broken — commit {bad[:12]} "
                    f"unreachable from {new_sha[:12]} (squash/rebase/force-push suspected); "
                    f"manual review required"
                )
    return True, None, "all gates passed"


def _query_child_remote_head(repo_path: str, url: str, branch: str, timeout: int | None) -> str | None:
    r = git_run(repo_path, ["ls-remote", url, branch], check=False, timeout=30)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split("\t", 1)[0].strip()


def do_forward_gitlinks(
    config: dict[str, Any],
    logger: logging.Logger,
    direction: Direction = Direction.BIDIRECTIONAL,
    dry_run: bool = False,
    ci_query: Any = None,
) -> dict[str, Any]:
    """SEE-1285: auto-forward lagging submodule gitlinks (three-gate, fail-close).

    Per `.gitmodules` submodule: compare parent index gitlink vs child remote
    branch head; lagging → run the three gates (CI green / GAP-1 reachable /
    interval integrity) → all pass = bump gitlink in ONE commit and push master
    (normal push path: GAP-1 runs again inside do_push; branch protection and
    push-guard semantics are those of a manual push — never bypassed). Any gate
    failing = fail-close: no commit, decision recorded in state.json
    (`forward` section) + daemon log only — never issue comments.

    Per-sub per-cycle throttle lives in the dedup: an already-forwarded remote
    head no longer lags, so re-forwarding cannot happen until the child moves.

    ci_query injects the Gate 1 producer (tests); default is the GitHub API
    query `default_ci_query`. `unknown` verdicts back off by failing the cycle
    (error_category=transient), which the daemon treats as non-counting.
    """
    repo_path = os.path.expanduser(config["repo_path"])
    remote = config["remote"]
    branch = config["branch"]
    subprocess_timeout = config.get("subprocess_timeout_seconds")
    query = ci_query if ci_query is not None else default_ci_query

    result: dict[str, Any] = {
        "action": "gitlink_forward",
        "state": None,
        "success": True,
        "message": "no lagging submodule",
        "dry_run": dry_run,
        "forward": {},
    }

    submodules = _parse_gitmodules_submodules(repo_path)
    if not submodules:
        return result

    links = dict(_iter_gitlinks(repo_path, subprocess_timeout))
    any_action = False
    all_ok = True
    messages: list[str] = []

    for sub in submodules:
        path_rel = sub.get("path", "")
        url = sub.get("url", "")
        sub_branch = sub.get("branch") or "master"
        if not path_rel or not url:
            continue
        gitlink_sha = links.get(path_rel)
        if not gitlink_sha:
            continue

        entry: dict[str, Any] = {"status": "synced"}
        head = _query_child_remote_head(repo_path, url, sub_branch, subprocess_timeout)
        if head is None:
            # Network/branch failure — skip silently (same boundary as (a) lag warn).
            entry["status"] = "skipped"
            result["forward"][path_rel] = entry
            continue
        if head == gitlink_sha:
            result["forward"][path_rel] = entry
            continue

        entry["target"] = head
        entry["target_sha"] = head
        outcome = _forward_one_submodule(
            config, logger, path_rel, url, gitlink_sha, head,
            query, dry_run, subprocess_timeout,
        )
        entry.update(outcome["entry"])
        result["forward"][path_rel] = entry
        messages.append(outcome["message"])
        all_ok = all_ok and outcome["ok"]
        any_action = any_action or outcome["action"]

    result["success"] = all_ok
    if messages:
        result["message"] = "; ".join(messages)
    # Fail-close decisions land in state.json (hint + Autopilot 巡检消费) —
    # never issue comments (v2 计划已取消该通道)。
    write_state(config, result)
    if not all_ok:
        result["error_category"] = _forward_error_category(result)
    return result


def _forward_one_submodule(
    config: dict[str, Any],
    logger: logging.Logger,
    path_rel: str,
    url: str,
    gitlink_sha: str,
    head: str,
    query: Any,
    dry_run: bool,
    subprocess_timeout: int | None,
) -> dict[str, Any]:
    """Run the three gates and (if allowed) forward one lagging submodule.

    Returns {ok, action, message, entry} — entry fields merged into the
    per-sub forward entry by the caller.
    """
    repo_path = os.path.expanduser(config["repo_path"])
    remote = config["remote"]
    branch = config["branch"]
    entry: dict[str, Any] = {}
    messages: list[str] = []
    ok = True
    action = False

    # Gate 1.
    try:
        conclusion = query(url, config["branch"], head)
    except Exception as e:  # noqa: BLE001 — producer bugs must not crash the cycle
        logger.warning("gate1 ci query crashed for %s: %s", path_rel, e)
        conclusion = "unknown"
    allowed, gate, msg = evaluate_forward_gates(
        repo_path, path_rel, url, config["branch"], head,
        ci_conclusion=conclusion, timeout=subprocess_timeout,
    )
    entry["gate"] = gate or "none"
    entry["detail"] = msg
    # hint 消费（gitlink-sync-hint.sh）用该字段落「CI 红 / 结论未知」两态；
    # 非 gate1 场景不污染条目（hint 只在 gate==gate1 分支读取）。
    if gate == "gate1":
        entry["gate1_conclusion"] = conclusion
    if not allowed:
        entry["status"] = "blocked"
        logger.warning("gitlink forward blocked for '%s' at %s: %s", path_rel, gate, msg)
        return {"ok": False, "action": False,
                "message": f"{path_rel}: blocked at {gate} — {msg}", "entry": entry}

    # All gates passed — bump gitlink to head in ONE commit, push master.
    if dry_run:
        entry["status"] = "would-forward"
        return {"ok": True, "action": True,
                "message": f"{path_rel}: dry-run would forward {gitlink_sha[:12]}→{head[:12]}",
                "entry": entry}

    # Materialize the new head into the child worktree so the index bump is
    # consistent with what a human `git add <submodule>` would record.
    child_wt = str(Path(repo_path) / path_rel)
    if (Path(child_wt) / ".git").exists():
        _g_ok = git_run(child_wt, ["fetch", url, head], check=False,
                        timeout=subprocess_timeout)
        if _g_ok.returncode == 0:
            git_run(child_wt, ["checkout", "--detach", head], check=False,
                    timeout=subprocess_timeout)
    r = git_run(repo_path, ["add", path_rel], check=False, timeout=subprocess_timeout)
    if r.returncode != 0:
        entry["status"] = "error"
        logger.error("gitlink forward add failed for '%s': %s", path_rel, r.stderr.strip())
        return {"ok": False, "action": False,
                "message": f"{path_rel}: add failed: {r.stderr.strip()}", "entry": entry}
    commit_msg = (
        f"chore(gitlink): auto-forward {path_rel} to {head[:12]} "
        f"(SEE-1285, CI green)"
    )
    r = git_run(repo_path, ["commit", "-m", commit_msg], check=False,
                timeout=subprocess_timeout)
    if r.returncode != 0:
        entry["status"] = "error"
        logger.error("gitlink forward commit failed for '%s': %s", path_rel, r.stderr.strip())
        return {"ok": False, "action": False,
                "message": f"{path_rel}: commit failed: {r.stderr.strip()}", "entry": entry}
    push_ok, push_msg = do_push(repo_path, remote, branch, timeout=subprocess_timeout)
    if not push_ok:
        entry["status"] = "error"
        logger.error("gitlink forward push failed for '%s': %s", path_rel, push_msg)
        return {"ok": False, "action": False,
                "message": f"{path_rel}: push failed: {push_msg}", "entry": entry}
    # Materialize child content at the new pointer (SEE-1268 discipline).
    do_submodule_update(repo_path, timeout=subprocess_timeout)
    entry["status"] = "forwarded"
    entry["from"] = gitlink_sha
    entry["from_sha"] = gitlink_sha
    logger.info("gitlink forward success for '%s': %s→%s", path_rel,
                gitlink_sha[:12], head[:12])
    return {"ok": True, "action": True,
            "message": f"{path_rel}: forwarded {gitlink_sha[:12]}→{head[:12]} (CI green)",
            "entry": entry}


def _forward_gate1_unknown_only(result: dict[str, Any]) -> bool:
    """True when every blocked entry is gate1-unknown (CI verdict undecidable).

    gate1 'unknown'（CI 结论不可判定）是瞬态——退避重试；CI 确实红
    （gate1_conclusion == 'fail'）及其余 gate 均为持久态，计入 daemon 熔断。
    """
    blocked = [
        e for e in result["forward"].values() if e.get("status") == "blocked"
    ]
    return (
        all(e.get("gate") == "gate1" for e in blocked)
        and any(e.get("gate1_conclusion") == "unknown" for e in blocked)
        and not any(e.get("gate1_conclusion") == "fail" for e in blocked)
    )


def _forward_error_category(result: dict[str, Any]) -> str:
    return (
        "transient" if _forward_gate1_unknown_only(result) else "persistent"
    )


# ---------------------------------------------------------------------------
# Alert via multica
# ---------------------------------------------------------------------------


def send_alert(config: dict[str, Any], message: str) -> tuple[bool, str]:
    issue_id = config.get("alert_issue_id")
    if not issue_id:
        return False, "alert_issue_id not configured"

    cmd = [
        "multica",
        "issue",
        "comment",
        "add",
        issue_id,
        "--content",
        message,
    ]
    r = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    return r.returncode == 0, r.stderr.strip() if r.stderr else r.stdout.strip()


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def _parse_gitmodules_submodules(repo_path: str) -> list[dict[str, str]]:
    """Parse .gitmodules into [{path, url, branch}] entries."""
    gm_path = Path(repo_path) / ".gitmodules"
    if not gm_path.is_file():
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in gm_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[submodule"):
            if current:
                entries.append(current)
            current = {}
        elif current is not None and "=" in line:
            key, _, val = line.partition("=")
            current[key.strip()] = val.strip()
    if current:
        entries.append(current)
    return entries


def _warn_submodule_lags(
    repo_path: str, config: dict[str, Any], logger: logging.Logger, timeout: int | None
) -> None:
    """(a) SYNCED 滞后告警（SEE-1268 §7.4(a)）：materialize 成功后逐子库比对
    remote branch head 与父库 index gitlink——落后仅 logger.warning 暴露，
    不拦截、不置 success=False；网络失败/分支不存在静默跳过；同一 remote
    head 只在变化时告警一次（state.json 去重，避免 5s 循环噪音）。

    返回告警后的 warned_heads 映射（并入 result 由 write_state 落盘）。
    """
    if not (Path(repo_path) / ".gitmodules").is_file():
        return

    links = _iter_gitlinks(repo_path, timeout)
    if not links:
        return
    link_map = {path: sha for path, sha in links}

    submodules = _parse_gitmodules_submodules(repo_path)
    if not submodules:
        return

    # 去重状态：上次告警的 remote head（同 head 不重复告警）。
    state_file = config.get("state_file")
    prior: dict[str, str] = {}
    if state_file and Path(state_file).is_file():
        try:
            prior = json.loads(Path(state_file).read_text(encoding="utf-8")).get(
                "lag_warned_heads", {}
            )
        except (json.JSONDecodeError, OSError):
            prior = {}
    warned_heads: dict[str, str] = dict(prior)

    for sub in submodules:
        path_rel = sub.get("path", "")
        if not path_rel or not sub.get("url"):
            continue
        gitlink_sha = link_map.get(path_rel)
        if not gitlink_sha:
            continue
        remote_head = _query_child_remote_head(
            repo_path, sub["url"], sub.get("branch", "master"), 30
        )
        if remote_head is None:
            continue  # 网络失败/分支不存在——静默跳过（§7.4(a) 边界）

        if remote_head == gitlink_sha:
            # 已追上——清除该子库的去重记录，下次再次落后时重新告警。
            warned_heads.pop(path_rel, None)
            continue
        # 落后（gitlink ≠ remote head）。仅状态变化告警。
        if prior.get(path_rel) == remote_head:
            continue
        _log_submodule_lag(logger, path_rel, gitlink_sha, sub["url"],
                           sub.get("branch", "master"), remote_head)
        warned_heads[path_rel] = remote_head

    return warned_heads


def _log_submodule_lag(
    logger: logging.Logger,
    path_rel: str,
    gitlink_sha: str,
    url: str,
    branch: str,
    remote_head: str,
) -> None:
    # LOW-1（code-review）：判定用「不等」，涵盖 behind / ahead / diverged
    # 三种关系；祖先校验需要远端 head 对象在本地（通常未 fetch），为判定
    # 多拉一次网络不值当。故采用评审给出的中性措辞选项——不谎称 "behind"，
    # 「需人工跟进」的语义不变（消费决策在主库 PR）。
    logger.warning(
        "submodule '%s' differs from its remote: gitlink %s vs %s#%s %s — "
        "父库 pin 旧版可能为设计意图；如需跟进，先推子库已就绪的指针变更"
        "（消费决策在主库 PR）",
        path_rel, gitlink_sha[:12], url, branch, remote_head[:12],
    )


def _handle_synced(
    config: dict[str, Any],
    logger: logging.Logger,
    direction: Direction,
    dry_run: bool,
    subprocess_timeout: int | None,
) -> dict[str, Any]:
    """SYNCED path: steady-state materialization + optional gitlink forward."""
    result: dict[str, Any] = {
        "action": "none",
        "state": SyncState.SYNCED.value,
        "success": True,
        "message": "",
        "dry_run": dry_run,
    }
    # SEE-1268 follow-up: pointer equality does NOT imply content equality —
    # a working copy can sit exactly at the remote head yet have uninitialized
    # submodules (arrived via an older daemon build or a manual pull).
    # Materialize on the steady-state path too, or that gap never self-heals.
    if dry_run:
        result["message"] = "already synced (dry-run: would verify submodules)"
        logger.info("already synced")
        return result

    sub_ok, sub_msg = do_submodule_update(config["repo_path"], timeout=subprocess_timeout)
    if not sub_ok:
        result["success"] = False
        result["message"] = (
            f"already synced but submodule materialization failed: {sub_msg}"
        )
        result["error_category"] = classify_error(sub_msg)
        logger.error(
            "submodule materialization failed on SYNCED path: %s (category=%s)",
            sub_msg,
            result["error_category"],
        )
        return result

    result["message"] = "already synced"
    logger.info("already synced")
    # (a) SYNCED 滞后告警（SEE-1268 §7.4(a)）：materialize 成功后检测子库
    # remote 超前——只暴露不动作（不拦截、不置失败）。
    lag_heads = _warn_submodule_lags(
        config["repo_path"], config, logger, subprocess_timeout
    )
    if lag_heads is not None:
        result["lag_warned_heads"] = lag_heads
    # SEE-1285: 落后子库在这里走三道 gate 自动 forward（全开才动，任何 gate
    # 未过 fail-close，决断只落 state.json + 日志）。严格 opt-in：未在 config
    # 显式开启本功能的环境零行为变化。
    if not config.get("gitlink_forward_enabled", False):
        return result
    fwd = do_forward_gitlinks(
        config, logger, direction=direction, dry_run=dry_run
    )
    result["forward"] = fwd.get("forward", {})
    result["action"] = fwd["action"]
    if not fwd["success"]:
        result["success"] = False
        result["message"] = fwd["message"]
        result["error_category"] = fwd.get("error_category", "persistent")
        # gate1 'unknown'（CI 结论未知）为瞬态——退避重试，不计入 daemon 熔断；
        # CI 红（conclusion=fail）为持久态照常计数（M-1 语义收口）。
        blocked = [
            e for e in result["forward"].values() if e.get("status") == "blocked"
        ]
        if (
            all(e.get("gate") == "gate1" for e in blocked)
            and any(e.get("gate1_conclusion") == "unknown" for e in blocked)
            and not any(e.get("gate1_conclusion") == "fail" for e in blocked)
        ):
            logger.info(
                "forward blocked only at gate1 (CI unknown) — transient, "
                "will retry next cycle without circuit-breaker credit",
            )
    elif fwd["message"] != "no lagging submodule":
        logger.info("gitlink forward: %s", fwd["message"])
    return result


def _handle_local_ahead(
    config: dict[str, Any],
    logger: logging.Logger,
    direction: Direction,
    dry_run: bool,
    subprocess_timeout: int | None,
) -> dict[str, Any]:
    """LOCAL_AHEAD path: push (unless direction restricts)."""
    repo_path = config["repo_path"]
    remote = config["remote"]
    branch = config["branch"]
    result: dict[str, Any] = {
        "action": "push",
        "state": SyncState.LOCAL_AHEAD.value,
        "success": True,
        "message": "",
        "dry_run": dry_run,
    }
    if direction not in (Direction.BIDIRECTIONAL, Direction.PUSH_ONLY):
        result["action"] = "none"
        result["message"] = (
            "local ahead, but direction restricts to pull-only (not applicable)"
        )
        logger.info("local ahead, skipped per direction")
        return result
    if dry_run:
        result["message"] = "dry-run: would push"
        logger.info("dry-run: would push")
        return result
    ok, msg = do_push(repo_path, remote, branch, timeout=subprocess_timeout)
    result["success"] = ok
    result["message"] = msg
    if ok:
        logger.info("push success: %s", msg)
        # Pointer push done — materialize the child content the new pointers
        # point at (SEE-1268).
        sub_ok, sub_msg = do_submodule_update(repo_path, timeout=subprocess_timeout)
        if not sub_ok:
            result["success"] = False
            result["message"] = f"{msg}; {sub_msg}"
            result["error_category"] = classify_error(sub_msg)
            logger.error(
                "submodule update after push failed: %s (category=%s)",
                sub_msg,
                result["error_category"],
            )
    else:
        result["error_category"] = classify_error(msg)
        logger.error("push failed: %s (category=%s)", msg, result["error_category"])
    return result


def _handle_remote_ahead(
    config: dict[str, Any],
    logger: logging.Logger,
    direction: Direction,
    dry_run: bool,
    stash_on_dirty: bool,
    subprocess_timeout: int | None,
) -> dict[str, Any]:
    """REMOTE_AHEAD path: pull --ff-only (unless direction restricts)."""
    repo_path = config["repo_path"]
    remote = config["remote"]
    branch = config["branch"]
    result: dict[str, Any] = {
        "action": "pull",
        "state": SyncState.REMOTE_AHEAD.value,
        "success": True,
        "message": "",
        "dry_run": dry_run,
    }
    if direction != Direction.BIDIRECTIONAL:
        result["action"] = "none"
        result["message"] = "remote ahead, but direction restricts to push-only"
        logger.info("remote ahead, skipped per direction")
        return result
    if dry_run:
        result["message"] = "dry-run: would pull"
        logger.info("dry-run: would pull")
        return result
    ok, msg = do_pull_ff_only(
        repo_path, remote, branch, stash_on_dirty, timeout=subprocess_timeout
    )
    result["success"] = ok
    result["message"] = msg
    if ok:
        logger.info("pull success: %s", msg)
    else:
        result["error_category"] = classify_error(msg)
        logger.error("pull failed: %s (category=%s)", msg, result["error_category"])
    return result


def _handle_diverged(
    config: dict[str, Any],
    logger: logging.Logger,
    dry_run: bool,
    stash_on_dirty: bool,
    conflict_strategy: ConflictResolution,
    subprocess_timeout: int | None,
) -> dict[str, Any]:
    """DIVERGED path: automatic conflict resolution + alert on failure."""
    repo_path = config["repo_path"]
    remote = config["remote"]
    branch = config["branch"]
    result: dict[str, Any] = {
        "action": "none",
        "state": SyncState.DIVERGED.value,
        "success": True,
        "message": "",
        "dry_run": dry_run,
    }
    logger.warning(
        "DIVERGED detected: local=%s remote=%s",
        get_sha(repo_path, branch, timeout=subprocess_timeout),
        get_sha(repo_path, f"{remote}/{branch}", timeout=subprocess_timeout),
    )

    resolved = resolve_diverged(
        repo_path,
        remote,
        branch,
        stash_on_dirty,
        conflict_strategy,
        logger,
        dry_run,
        timeout=subprocess_timeout,
    )
    result.update(
        {
            "action": resolved["action"],
            "success": resolved["success"],
            "message": resolved["message"],
            "resolution_steps": resolved.get("resolution_steps", []),
        }
    )

    if resolved["success"]:
        logger.info("DIVERGED resolved: %s", resolved["message"])
        return result

    logger.error("DIVERGED resolution failed: %s", resolved["message"])
    if not dry_run:
        alert_msg = (
            "🚨 **Repo Sync Alert — DIVERGED resolution failed**\n\n"
            f"- Local `{branch}`: `{get_sha(repo_path, branch, timeout=subprocess_timeout) or 'unknown'}`\n"
            f"- Remote `{remote}/{branch}`: `{get_sha(repo_path, f'{remote}/{branch}', timeout=subprocess_timeout) or 'unknown'}`\n"
            f"- Strategy: `{conflict_strategy.value}`\n"
            f"- Steps: `{resolved.get('resolution_steps', [])}`\n"
            f"- Repo: `{repo_path}`\n\n"
            f"Resolution message: {resolved['message']}\n\n"
            "Manual intervention required."
        )
        ok, err = send_alert(config, alert_msg)
        if ok:
            logger.info("alert sent")
        else:
            logger.error("alert failed: %s", err)
    return result


def run_sync(
    config: dict[str, Any],
    logger: logging.Logger,
    direction: Direction = Direction.BIDIRECTIONAL,
    dry_run: bool = False,
) -> dict[str, Any]:
    repo_path = os.path.expanduser(config["repo_path"])
    remote = config["remote"]
    branch = config["branch"]
    stash_on_dirty = config.get("stash_on_dirty", True)
    conflict_strategy = ConflictResolution(
        config.get(
            "conflict_resolution_strategy", ConflictResolution.REBASE_FIRST.value
        )
    )
    subprocess_timeout = config.get("subprocess_timeout_seconds")

    result = {
        "action": "none",
        "state": None,
        "success": True,
        "message": "",
        "dry_run": dry_run,
    }

    # Branch guard (SEE-869): ensure main repo is on master before sync.
    # If a stash was taken here, it is restored once after the sync body
    # completes (single stash + single pop per cycle — never nested).
    guard_ok, guard_msg, had_stash = ensure_on_master_branch(
        repo_path,
        branch,
        stash_on_dirty,
        dry_run,
        logger,
        timeout=subprocess_timeout,
    )
    if not guard_ok:
        result["success"] = False
        result["action"] = "branch_guard"
        result["message"] = guard_msg
        result["error_category"] = classify_error(guard_msg)
        logger.error(
            "branch guard failed: %s (category=%s)",
            guard_msg,
            result["error_category"],
        )
        write_state(config, result)
        return result

    try:
        # Fetch origin (with retry for transient network errors)
        fetch_retries = config.get("fetch_retry_count", 3)
        fetch_retry_delay = config.get("fetch_retry_delay_seconds", 2.0)
        ok, err = git_fetch_with_retry(
            repo_path, remote, timeout=subprocess_timeout,
            retries=fetch_retries, retry_delay=fetch_retry_delay,
        )
        if not ok:
            result["success"] = False
            result["message"] = f"fetch failed: {err}"
            result["error_category"] = classify_error(err)
            logger.warning("fetch failed: %s (category=%s)", err, result["error_category"])
            write_state(config, result)
            return result

        state = detect_sync_state(repo_path, remote, branch, timeout=subprocess_timeout)
        result["state"] = state.value
        logger.info("sync state: %s", state.value)

        if state == SyncState.SYNCED:
            result = _handle_synced(
                config, logger, direction, dry_run, subprocess_timeout
            )
        elif state == SyncState.LOCAL_AHEAD:
            result = _handle_local_ahead(
                config, logger, direction, dry_run, subprocess_timeout
            )
        elif state == SyncState.REMOTE_AHEAD:
            result = _handle_remote_ahead(
                config, logger, direction, dry_run, stash_on_dirty,
                subprocess_timeout,
            )
        else:
            result = _handle_diverged(
                config, logger, dry_run, stash_on_dirty, conflict_strategy,
                subprocess_timeout,
            )

        write_state(config, result)
        return result
    finally:
        # Restore the stash taken by ensure_on_master_branch — one pop max
        # per sync cycle. Failure here overrides the previously written
        # result so the state file reflects the latest failure.
        if had_stash and not dry_run:
            pop_ok, pop_msg = do_stash_pop(repo_path, timeout=subprocess_timeout)
            if not pop_ok:
                logger.error("stash pop failed after sync: %s", pop_msg)
                result["success"] = False
                prior = result.get("message", "").strip()
                result["message"] = (
                    f"{prior}; stash pop failed: {pop_msg}".lstrip("; ").strip()
                )
                result["error_category"] = classify_error(pop_msg)
                write_state(config, result)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Repo sync script")
    parser.add_argument("--config", default=None, help="Path to config JSON")
    parser.add_argument(
        "--direction",
        choices=[Direction.BIDIRECTIONAL.value, Direction.PUSH_ONLY.value],
        default=Direction.BIDIRECTIONAL.value,
        help="Sync direction",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Detect state only, no write ops"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress console output")
    parser.add_argument(
        "--lock-timeout",
        type=float,
        default=0.0,
        help="Lock acquisition timeout (seconds)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logger = setup_logging(
        config, quiet=args.quiet, file_log=not args.dry_run
    )

    lock_path = config.get("lock_file", "/tmp/godot-qa-toolkit-sync.lock")
    lock_fd = acquire_lock(lock_path, timeout=args.lock_timeout)
    if lock_fd is None:
        logger.warning(
            "Could not acquire lock (%s) — another sync is running", lock_path
        )
        return 1

    # Write PID to lock file for stale lock detection
    try:
        os.lseek(lock_fd, 0, os.SEEK_SET)
        os.truncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode("utf-8"))
    except OSError:
        pass

    try:
        result = run_sync(
            config,
            logger,
            direction=Direction(args.direction),
            dry_run=args.dry_run,
        )
        return 0 if result["success"] else 1
    finally:
        release_lock(lock_fd, lock_path)


if __name__ == "__main__":
    sys.exit(main())
