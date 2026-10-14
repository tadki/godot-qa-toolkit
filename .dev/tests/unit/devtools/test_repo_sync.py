#!/usr/bin/env python3
"""
Unit tests for .dev/repo-sync/ (repo_sync.py and repo_sync_daemon.py).
T4 QA round 2 — verifies all 7 defects from the first T4 QA report are fixed.

SEE-476: Git main repo bidirectional sync mechanism
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add .dev to path so imports resolve
# sys.path setup is centralized in ../conftest.py

from repo_sync import (
    ConflictResolution,
    Direction,
    SyncState,
    DEFAULT_CONFIG_PATH,
    acquire_lock,
    classify_error,
    detect_sync_state,
    do_pull_ff_only,
    do_pull_merge,
    do_push,
    do_rebase,
    do_rebase_abort,
    do_stash_if_dirty,
    do_stash_pop,
    ensure_on_master_branch,
    get_current_branch,
    get_sha,
    git_fetch,
    git_run,
    has_git_operation_in_progress,
    has_submodule_changes,
    is_master_worktree_occupied,
    load_config,
    release_lock,
    resolve_diverged,
    _handle_remote_ahead,
    run_sync,
    switch_to_master,
)
from repo_sync_daemon import load_config as daemon_load_config


# ---------------------------------------------------------------------------
# Helper: create a temporary git repo
# ---------------------------------------------------------------------------

def _run_git(cwd, args, check=True):
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repo with a remote."""
    with tempfile.TemporaryDirectory() as d:
        local = os.path.join(d, "local")
        remote = os.path.join(d, "remote.git")

        os.makedirs(local)
        os.makedirs(remote)

        # Init bare remote
        _run_git(remote, ["init", "--bare", "-b", "master"])

        # Init local and push to remote
        _run_git(local, ["init", "-b", "master"])
        _run_git(local, ["config", "user.email", "test@test.com"])
        _run_git(local, ["config", "user.name", "Test"])

        Path(os.path.join(local, "a.txt")).write_text("a")
        _run_git(local, ["add", "a.txt"])
        _run_git(local, ["commit", "-m", "init"])
        _run_git(local, ["remote", "add", "origin", remote])
        _run_git(local, ["push", "-u", "origin", "master"])

        yield {"local": local, "remote": remote}


@pytest.fixture
def temp_config(tmp_path):
    """Return a temporary config dict for repo_sync."""
    return {
        "repo_path": str(tmp_path / "repo"),
        "remote": "origin",
        "branch": "master",
        "log_dir": str(tmp_path / "logs"),
        "state_file": str(tmp_path / "state.json"),
        "lock_file": str(tmp_path / "sync.lock"),
        "stash_on_dirty": True,
    }


# ---------------------------------------------------------------------------
# Defect 1: DEFAULT_CONFIG_PATH with_suffix -> with_name
# ---------------------------------------------------------------------------

class TestConfigPath:
    def test_default_config_path_uses_with_name(self):
        """
        Defect 1: DEFAULT_CONFIG_PATH must use with_name(), not with_suffix().

        with_suffix("_config.json") is invalid in Python 3.14+ because
        with_suffix() requires the suffix to start with a dot.
        with_name("repo_sync_config.json") replaces the entire filename,
        which is correct regardless of Python version.
        """
        # Path must end with the correct filename
        assert str(DEFAULT_CONFIG_PATH).endswith("repo_sync_config.json")

    def test_default_config_path_is_sibling_to_script(self):
        """Ensure the config path resolves to a sibling of repo_sync.py."""
        assert DEFAULT_CONFIG_PATH.name == "repo_sync_config.json"


class TestLoadConfig:
    def test_load_config_reads_json(self, tmp_path):
        config_file = tmp_path / "test_config.json"
        config_file.write_text('{"repo_path": "/tmp/test", "remote": "origin"}')
        cfg = load_config(config_file)
        assert cfg["repo_path"] == "/tmp/test"
        assert cfg["remote"] == "origin"


# ---------------------------------------------------------------------------
# Defect 2 & 5: acquire_lock / release_lock on Windows
# ---------------------------------------------------------------------------

class TestAcquireLock:
    def test_acquire_lock_returns_fd_on_unix(self, tmp_path):
        """On non-Windows platforms, acquire_lock returns a valid fd."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        lock_file = str(tmp_path / "test.lock")
        fd = acquire_lock(lock_file)
        assert fd is not None
        assert isinstance(fd, int)
        release_lock(fd, lock_file)

    def test_acquire_lock_timeout_returns_none_when_locked(self, tmp_path):
        """If another process holds the lock, timeout should return None."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        lock_file = str(tmp_path / "test.lock")
        fd1 = acquire_lock(lock_file)
        assert fd1 is not None

        fd2 = acquire_lock(lock_file, timeout=0.2)
        assert fd2 is None

        release_lock(fd1, lock_file)

    def test_windows_acquire_lock_not_none(self):
        """
        Defect 2: Windows acquire_lock must NOT simply return None.

        It should implement msvcrt.locking-based file locking.
        We verify by checking the source code does not have 'return None'
        as the sole body of the win32 branch.
        """
        import inspect
        src = inspect.getsource(acquire_lock)
        # The old code had: if sys.platform == "win32":\n        return None
        # The new code imports msvcrt and does actual locking.
        assert "msvcrt" in src, "Windows lock should use msvcrt"
        assert "os.open" in src, "Windows lock should call os.open"
        assert "LK_NBLCK" in src, "Windows lock should use LK_NBLCK"

    def test_release_lock_handles_windows(self):
        """
        Defect 5: release_lock must handle Windows platform.

        The original code unconditionally called fcntl.flock(fd, LOCK_UN),
        which would fail on Windows. The fix adds a platform check.
        """
        import inspect
        src = inspect.getsource(release_lock)
        assert "win32" in src, "release_lock should branch on platform"
        assert "LK_UNLCK" in src, "release_lock should use msvcrt.LK_UNLCK on Windows"

    def test_release_lock_noop_on_none_fd(self, tmp_path):
        """release_lock should be safe when fd is None."""
        lock_file = str(tmp_path / "test.lock")
        release_lock(None, lock_file)


# ---------------------------------------------------------------------------
# Defect 3: merge-base detection via git_run instead of get_sha
# ---------------------------------------------------------------------------

class TestDetectSyncState:
    def test_synced(self, temp_git_repo):
        repo = temp_git_repo["local"]
        state = detect_sync_state(repo, "origin", "master")
        assert state == SyncState.SYNCED

    def test_local_ahead(self, temp_git_repo):
        repo = temp_git_repo["local"]
        Path(os.path.join(repo, "b.txt")).write_text("b")
        _run_git(repo, ["add", "b.txt"])
        _run_git(repo, ["commit", "-m", "local commit"])

        state = detect_sync_state(repo, "origin", "master")
        assert state == SyncState.LOCAL_AHEAD

    def test_remote_ahead(self, temp_git_repo):
        local = temp_git_repo["local"]
        remote = temp_git_repo["remote"]

        # Make a commit directly on the bare remote using another clone
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "c.txt")).write_text("c")
        _run_git(clone, ["add", "c.txt"])
        _run_git(clone, ["commit", "-m", "remote commit"])
        _run_git(clone, ["push"])

        # Fetch so local sees the new remote ref
        _run_git(local, ["fetch", "origin"])

        state = detect_sync_state(local, "origin", "master")
        assert state == SyncState.REMOTE_AHEAD

    def test_diverged(self, temp_git_repo):
        local = temp_git_repo["local"]
        remote = temp_git_repo["remote"]

        # Local commit
        Path(os.path.join(local, "local.txt")).write_text("local")
        _run_git(local, ["add", "local.txt"])
        _run_git(local, ["commit", "-m", "local"])

        # Remote commit via clone
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "remote.txt")).write_text("remote")
        _run_git(clone, ["add", "remote.txt"])
        _run_git(clone, ["commit", "-m", "remote"])
        _run_git(clone, ["push"])

        _run_git(local, ["fetch", "origin"])
        state = detect_sync_state(local, "origin", "master")
        assert state == SyncState.DIVERGED

    def test_merge_base_uses_git_run_not_get_sha(self):
        """
        Defect 3: detect_sync_state must call git merge-base via git_run,
        NOT by passing 'merge-base branch remote/branch' as a ref to get_sha().

        get_sha() internally calls 'git rev-parse <ref>' which does NOT
        understand 'merge-base' as a valid ref.
        """
        import inspect
        src = inspect.getsource(detect_sync_state)
        # Must call git_run with merge-base subcommand
        assert '"merge-base"' in src and "git_run" in src, (
            "detect_sync_state must call git_run with merge-base subcommand"
        )
        # Must NOT pass merge-base as a string to get_sha
        assert 'get_sha(repo_path, "merge-base' not in src, (
            "detect_sync_state must NOT pass merge-base string to get_sha"
        )
        assert "get_sha(repo_path, f\"merge-base" not in src, (
            "detect_sync_state must NOT pass merge-base f-string to get_sha"
        )


# ---------------------------------------------------------------------------
# Defect 4: finally block return -> restructured do_pull_ff_only
# ---------------------------------------------------------------------------

class TestDoPullFfOnly:
    def test_no_return_in_finally_block(self):
        """
        Defect 4: do_pull_ff_only must NOT have a return statement
        inside a finally block.

        'return' inside 'finally' produces a SyntaxWarning in Python
        and can suppress exceptions.
        """
        import inspect
        src = inspect.getsource(do_pull_ff_only)
        assert "finally" not in src, (
            "do_pull_ff_only should not use a finally block"
        )

    def test_pull_ff_only_success(self, temp_git_repo):
        remote = temp_git_repo["remote"]
        local = temp_git_repo["local"]

        # Push a new commit via clone to make remote ahead
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "new.txt")).write_text("new")
        _run_git(clone, ["add", "new.txt"])
        _run_git(clone, ["commit", "-m", "remote new"])
        _run_git(clone, ["push"])

        # Now local is behind; pull should succeed
        ok, msg = do_pull_ff_only(local, "origin", "master", stash_on_dirty=False)
        assert ok, f"pull failed: {msg}"
        assert "success" in msg

    def test_pull_with_dirty_working_tree_stashes(self, temp_git_repo):
        local = temp_git_repo["local"]
        # Make working tree dirty with a new untracked file
        Path(os.path.join(local, "dirty.txt")).write_text("dirty")

        ok, msg = do_pull_ff_only(local, "origin", "master", stash_on_dirty=True)
        # Already synced, so pull is a no-op but stash/restore should work
        assert ok, f"pull with dirty tree failed: {msg}"
        # After stash pop, dirty.txt should still exist
        assert os.path.exists(os.path.join(local, "dirty.txt"))

    def test_pull_with_dirty_and_stash_disabled(self, temp_git_repo):
        local = temp_git_repo["local"]
        Path(os.path.join(local, "dirty.txt")).write_text("dirty")

        ok, msg = do_pull_ff_only(local, "origin", "master", stash_on_dirty=False)
        assert not ok
        assert "dirty" in msg.lower()


class TestDoPush:
    def test_push_local_ahead(self, temp_git_repo):
        local = temp_git_repo["local"]
        Path(os.path.join(local, "push.txt")).write_text("push")
        _run_git(local, ["add", "push.txt"])
        _run_git(local, ["commit", "-m", "push test"])

        ok, msg = do_push(local, "origin", "master")
        assert ok, f"push failed: {msg}"
        assert "success" in msg


# ---------------------------------------------------------------------------
# Defect 6: REMOTE_AHEAD direction check must exclude PUSH_ONLY
# ---------------------------------------------------------------------------

class TestRemoteAheadDirection:
    def test_push_only_does_not_pull(self, temp_git_repo):
        """
        Defect 6: When direction is PUSH_ONLY and state is REMOTE_AHEAD,
        the sync must NOT attempt a pull. It should skip with a message.

        The buggy code had: if direction == BIDIRECTIONAL or direction == PUSH_ONLY:
        which incorrectly allowed pull in PUSH_ONLY mode.
        """
        import inspect
        # SEE-1406: the REMOTE_AHEAD branch lives in _handle_remote_ahead.
        src = inspect.getsource(_handle_remote_ahead)

        # The REMOTE_AHEAD branch should only allow BIDIRECTIONAL
        assert "Direction.BIDIRECTIONAL" in src, (
            "REMOTE_AHEAD branch should check BIDIRECTIONAL"
        )
        # And NOT allow PUSH_ONLY in the same condition
        assert "Direction.PUSH_ONLY" not in src, (
            "PUSH_ONLY should not appear in REMOTE_AHEAD condition"
        )


class TestRunSyncRemoteAhead:
    def test_remote_ahead_push_only_skips(self, temp_git_repo, tmp_path):
        """When remote is ahead and direction is PUSH_ONLY, do not pull."""
        remote = temp_git_repo["remote"]
        local = temp_git_repo["local"]

        # Make remote ahead
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "ahead.txt")).write_text("ahead")
        _run_git(clone, ["add", "ahead.txt"])
        _run_git(clone, ["commit", "-m", "ahead"])
        _run_git(clone, ["push"])

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.PUSH_ONLY, dry_run=False)
        assert result["state"] == "REMOTE_AHEAD"
        assert result["action"] == "none"  # Did not pull
        assert "push-only" in result["message"].lower()

    def test_remote_ahead_bidirectional_pulls(self, temp_git_repo, tmp_path):
        """When remote is ahead and direction is BIDIRECTIONAL, do pull."""
        remote = temp_git_repo["remote"]
        local = temp_git_repo["local"]

        # Make remote ahead
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "ahead.txt")).write_text("ahead")
        _run_git(clone, ["add", "ahead.txt"])
        _run_git(clone, ["commit", "-m", "ahead"])
        _run_git(clone, ["push"])

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL, dry_run=False)
        assert result["state"] == "REMOTE_AHEAD"
        assert result["action"] == "pull"
        assert result["success"] is True
        assert "success" in result["message"]


# ---------------------------------------------------------------------------
# Defect 7: daemon config fallback path
# ---------------------------------------------------------------------------

class TestDaemonConfigPath:
    def test_daemon_config_fallback_uses_parent(self):
        """
        Defect 7: daemon load_config fallback must use Path(__file__).parent,
        NOT with_suffix("").

        with_suffix("") would strip the .py extension but keep the
        directory component as the filename, resulting in an invalid path.
        """
        import inspect
        src = inspect.getsource(daemon_load_config)
        # Must use .parent
        assert "Path(__file__).parent" in src, (
            "daemon load_config must use Path(__file__).parent for config path"
        )
        # Must NOT use with_suffix
        assert ".with_suffix(\"\")" not in src, (
            "daemon load_config must NOT use with_suffix('') for config path"
        )


# ---------------------------------------------------------------------------
# Integration: end-to-end sync scenarios
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_local_ahead_push_only(self, temp_git_repo, tmp_path):
        """Local commit + push_only = push succeeds."""
        local = temp_git_repo["local"]
        Path(os.path.join(local, "push.txt")).write_text("push")
        _run_git(local, ["add", "push.txt"])
        _run_git(local, ["commit", "-m", "push"])

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.PUSH_ONLY, dry_run=False)
        assert result["state"] == "LOCAL_AHEAD"
        assert result["action"] == "push"
        assert result["success"] is True

    def test_diverged_auto_resolves_with_rebase(self, temp_git_repo, tmp_path):
        """Non-conflicting diverged state auto-resolves via rebase_first."""
        remote = temp_git_repo["remote"]
        local = temp_git_repo["local"]

        # Local commit
        Path(os.path.join(local, "local.txt")).write_text("local")
        _run_git(local, ["add", "local.txt"])
        _run_git(local, ["commit", "-m", "local"])

        # Remote commit
        clone = os.path.join(os.path.dirname(remote), "clone2")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "remote.txt")).write_text("remote")
        _run_git(clone, ["add", "remote.txt"])
        _run_git(clone, ["commit", "-m", "remote"])
        _run_git(clone, ["push"])

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL, dry_run=False)
        assert result["state"] == "DIVERGED"
        assert result["success"] is True
        assert result["action"] == "resolve_diverged"

    def test_already_synced(self, temp_git_repo, tmp_path):
        """Already synced state = no action."""
        local = temp_git_repo["local"]
        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL, dry_run=False)
        assert result["state"] == "SYNCED"
        assert result["action"] == "none"
        assert result["success"] is True

    def test_dry_run_does_not_modify(self, temp_git_repo, tmp_path):
        """Dry run should detect state but not perform any writes."""
        remote = temp_git_repo["remote"]
        local = temp_git_repo["local"]

        # Make local ahead
        Path(os.path.join(local, "dry.txt")).write_text("dry")
        _run_git(local, ["add", "dry.txt"])
        _run_git(local, ["commit", "-m", "dry"])

        # Get current remote HEAD before dry run
        r = subprocess.run(
            ["git", "-C", local, "rev-parse", "origin/master"],
            capture_output=True, text=True, check=True,
        )
        before_remote_sha = r.stdout.strip()

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL, dry_run=True)
        assert result["state"] == "LOCAL_AHEAD"
        assert result["action"] == "push"
        assert result["dry_run"] is True

        # Verify remote was NOT actually pushed to
        r = subprocess.run(
            ["git", "-C", local, "rev-parse", "origin/master"],
            capture_output=True, text=True, check=True,
        )
        after_remote_sha = r.stdout.strip()
        assert before_remote_sha == after_remote_sha


# ---------------------------------------------------------------------------
# ConflictResolution enum
# ---------------------------------------------------------------------------

class TestConflictResolutionEnum:
    def test_enum_values(self):
        assert ConflictResolution.REBASE_FIRST.value == "rebase_first"
        assert ConflictResolution.MERGE_FIRST.value == "merge_first"
        assert ConflictResolution.BLOCK_ONLY.value == "block_only"


# ---------------------------------------------------------------------------
# do_stash_if_dirty / do_stash_pop
# ---------------------------------------------------------------------------

class TestStashHelpers:
    def test_clean_tree_no_stash(self, temp_git_repo):
        local = temp_git_repo["local"]
        ok, msg, had_stash = do_stash_if_dirty(local, stash_on_dirty=True)
        assert ok is True
        assert had_stash is False

    def test_dirty_tree_stash_and_pop(self, temp_git_repo):
        local = temp_git_repo["local"]
        Path(os.path.join(local, "dirty.txt")).write_text("dirty")
        ok, msg, had_stash = do_stash_if_dirty(local, stash_on_dirty=True)
        assert ok is True
        assert had_stash is True
        pop_ok, pop_msg = do_stash_pop(local)
        assert pop_ok is True
        assert os.path.exists(os.path.join(local, "dirty.txt"))

    def test_dirty_tree_stash_disabled(self, temp_git_repo):
        local = temp_git_repo["local"]
        Path(os.path.join(local, "dirty.txt")).write_text("dirty")
        ok, msg, had_stash = do_stash_if_dirty(local, stash_on_dirty=False)
        assert ok is False
        assert "dirty" in msg.lower()


# ---------------------------------------------------------------------------
# resolve_diverged — real git scenarios
# ---------------------------------------------------------------------------

class TestResolveDivergedRealGit:
    def _make_diverged(self, temp_git_repo):
        """Create diverged state with non-conflicting commits on different files."""
        local = temp_git_repo["local"]
        remote = temp_git_repo["remote"]

        # Local commit on a unique file
        Path(os.path.join(local, "local_change.txt")).write_text("local")
        _run_git(local, ["add", "local_change.txt"])
        _run_git(local, ["commit", "-m", "local change"])

        # Remote commit via clone on a different file
        clone = os.path.join(os.path.dirname(remote), "clone_div")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "remote_change.txt")).write_text("remote")
        _run_git(clone, ["add", "remote_change.txt"])
        _run_git(clone, ["commit", "-m", "remote change"])
        _run_git(clone, ["push"])

        _run_git(local, ["fetch", "origin"])

    def test_rebase_first_resolves(self, temp_git_repo):
        """Non-conflicting diverged state: rebase_first should rebase + push."""
        local = temp_git_repo["local"]
        self._make_diverged(temp_git_repo)

        logger = MagicMock()
        result = resolve_diverged(
            local, "origin", "master",
            stash_on_dirty=True,
            strategy=ConflictResolution.REBASE_FIRST,
            logger=logger,
        )
        assert result["success"] is True
        assert "rebase" in result["message"]
        assert "rebase(ok)" in result["resolution_steps"]
        assert "push(ok)" in result["resolution_steps"]

        # Verify synced after resolution
        _run_git(local, ["fetch", "origin"])
        state = detect_sync_state(local, "origin", "master")
        assert state == SyncState.SYNCED

    def test_merge_first_resolves(self, temp_git_repo):
        """Non-conflicting diverged state: merge_first should merge + push."""
        local = temp_git_repo["local"]
        self._make_diverged(temp_git_repo)

        logger = MagicMock()
        result = resolve_diverged(
            local, "origin", "master",
            stash_on_dirty=True,
            strategy=ConflictResolution.MERGE_FIRST,
            logger=logger,
        )
        assert result["success"] is True
        assert "merge" in result["message"]
        assert "merge(ok)" in result["resolution_steps"]
        assert "push(ok)" in result["resolution_steps"]

    def test_block_only_blocks(self, temp_git_repo):
        """block_only strategy should not attempt any resolution."""
        local = temp_git_repo["local"]
        self._make_diverged(temp_git_repo)

        logger = MagicMock()
        result = resolve_diverged(
            local, "origin", "master",
            stash_on_dirty=True,
            strategy=ConflictResolution.BLOCK_ONLY,
            logger=logger,
        )
        assert result["success"] is False
        assert "block_only" in result["message"]
        assert result["resolution_steps"] == []

    def test_dry_run_does_not_modify(self, temp_git_repo):
        """Dry run should report but not modify anything."""
        local = temp_git_repo["local"]
        self._make_diverged(temp_git_repo)

        sha_before = get_sha(local, "master")

        logger = MagicMock()
        result = resolve_diverged(
            local, "origin", "master",
            stash_on_dirty=True,
            strategy=ConflictResolution.REBASE_FIRST,
            logger=logger,
            dry_run=True,
        )
        assert result["success"] is False  # dry_run reports initial False
        assert "dry-run" in result["message"]

        sha_after = get_sha(local, "master")
        assert sha_before == sha_after

    def test_resolves_with_dirty_working_tree(self, temp_git_repo):
        """Diverged + dirty working tree: should stash, resolve, pop."""
        local = temp_git_repo["local"]
        self._make_diverged(temp_git_repo)

        # Make working tree dirty
        Path(os.path.join(local, "dirty.txt")).write_text("dirty")

        logger = MagicMock()
        result = resolve_diverged(
            local, "origin", "master",
            stash_on_dirty=True,
            strategy=ConflictResolution.REBASE_FIRST,
            logger=logger,
        )
        assert result["success"] is True
        assert "stash" in result["resolution_steps"]
        assert "stash_pop(ok)" in result["resolution_steps"]

        # Dirty file should be restored
        assert os.path.exists(os.path.join(local, "dirty.txt"))


# ---------------------------------------------------------------------------
# resolve_diverged — mock-based fallback tests
# ---------------------------------------------------------------------------

class TestResolveDivergedMocked:
    def test_rebase_fails_merge_succeeds(self, temp_git_repo):
        """rebase_first: rebase fails → abort → merge succeeds → push."""
        local = temp_git_repo["local"]

        logger = MagicMock()
        with patch("repo_sync.do_rebase", return_value=(False, "rebase conflict")), \
             patch("repo_sync.do_rebase_abort", return_value=(True, "aborted")), \
             patch("repo_sync.do_pull_merge", return_value=(True, "merge ok")), \
             patch("repo_sync.do_push", return_value=(True, "push ok")):
            result = resolve_diverged(
                local, "origin", "master",
                stash_on_dirty=True,
                strategy=ConflictResolution.REBASE_FIRST,
                logger=logger,
            )
        assert result["success"] is True
        assert "merge fallback" in result["message"]
        assert "rebase(fail)" in result["resolution_steps"]
        assert "rebase_abort(ok)" in result["resolution_steps"]
        assert "merge(ok)" in result["resolution_steps"]
        assert "push(ok)" in result["resolution_steps"]

    def test_rebase_and_merge_both_fail(self, temp_git_repo):
        """rebase_first: both rebase and merge fail → block."""
        local = temp_git_repo["local"]

        logger = MagicMock()
        with patch("repo_sync.do_rebase", return_value=(False, "rebase conflict")), \
             patch("repo_sync.do_rebase_abort", return_value=(True, "aborted")), \
             patch("repo_sync.do_pull_merge", return_value=(False, "merge conflict")):
            result = resolve_diverged(
                local, "origin", "master",
                stash_on_dirty=True,
                strategy=ConflictResolution.REBASE_FIRST,
                logger=logger,
            )
        assert result["success"] is False
        assert "manual intervention" in result["message"]
        assert "rebase(fail)" in result["resolution_steps"]
        assert "merge(fail)" in result["resolution_steps"]

    def test_merge_first_fails(self, temp_git_repo):
        """merge_first: merge fails → block."""
        local = temp_git_repo["local"]

        logger = MagicMock()
        with patch("repo_sync.do_pull_merge", return_value=(False, "merge conflict")):
            result = resolve_diverged(
                local, "origin", "master",
                stash_on_dirty=True,
                strategy=ConflictResolution.MERGE_FIRST,
                logger=logger,
            )
        assert result["success"] is False
        assert "manual intervention" in result["message"]
        assert "merge(fail)" in result["resolution_steps"]

    def test_rebase_succeeds_push_fails(self, temp_git_repo):
        """rebase succeeds but push fails → report failure."""
        local = temp_git_repo["local"]

        logger = MagicMock()
        with patch("repo_sync.do_rebase", return_value=(True, "rebase ok")), \
             patch("repo_sync.do_push", return_value=(False, "push rejected")):
            result = resolve_diverged(
                local, "origin", "master",
                stash_on_dirty=True,
                strategy=ConflictResolution.REBASE_FIRST,
                logger=logger,
            )
        assert result["success"] is False
        assert "push failed" in result["message"]
        assert "rebase(ok)" in result["resolution_steps"]
        assert "push(fail)" in result["resolution_steps"]


# ---------------------------------------------------------------------------
# run_sync with DIVERGED + conflict_resolution_strategy
# ---------------------------------------------------------------------------

class TestRunSyncDivergedResolution:
    def _make_diverged_config(self, temp_git_repo, tmp_path, strategy="rebase_first"):
        """Create diverged state and return config dict."""
        local = temp_git_repo["local"]
        remote = temp_git_repo["remote"]

        # Local commit
        Path(os.path.join(local, "local_file.txt")).write_text("local")
        _run_git(local, ["add", "local_file.txt"])
        _run_git(local, ["commit", "-m", "local"])

        # Remote commit via clone
        clone = os.path.join(os.path.dirname(remote), "clone_div")
        os.makedirs(clone)
        _run_git(clone, ["clone", remote, "."])
        _run_git(clone, ["config", "user.email", "test@test.com"])
        _run_git(clone, ["config", "user.name", "Test"])
        Path(os.path.join(clone, "remote_file.txt")).write_text("remote")
        _run_git(clone, ["add", "remote_file.txt"])
        _run_git(clone, ["commit", "-m", "remote"])
        _run_git(clone, ["push"])

        config = {
            "repo_path": local,
            "remote": "origin",
            "branch": "master",
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
            "conflict_resolution_strategy": strategy,
        }
        return config

    def test_diverged_rebase_first_auto_resolves(self, temp_git_repo, tmp_path):
        """Full run_sync with rebase_first resolves non-conflicting divergence."""
        config = self._make_diverged_config(temp_git_repo, tmp_path, "rebase_first")
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["state"] == "DIVERGED"
        assert result["success"] is True
        assert result["action"] == "resolve_diverged"
        assert len(result.get("resolution_steps", [])) > 0

    def test_diverged_merge_first_auto_resolves(self, temp_git_repo, tmp_path):
        """Full run_sync with merge_first resolves non-conflicting divergence."""
        config = self._make_diverged_config(temp_git_repo, tmp_path, "merge_first")
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["state"] == "DIVERGED"
        assert result["success"] is True
        assert result["action"] == "resolve_diverged"

    def test_diverged_block_only_blocks(self, temp_git_repo, tmp_path):
        """Full run_sync with block_only does not resolve."""
        config = self._make_diverged_config(temp_git_repo, tmp_path, "block_only")
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["state"] == "DIVERGED"
        assert result["success"] is False
        assert result["action"] == "resolve_diverged"

    def test_diverged_dry_run(self, temp_git_repo, tmp_path):
        """Dry run on diverged state should not modify anything."""
        config = self._make_diverged_config(temp_git_repo, tmp_path)
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL, dry_run=True)
        assert result["state"] == "DIVERGED"
        assert result["success"] is False
        assert result["dry_run"] is True

    def test_default_strategy_is_rebase_first(self, temp_git_repo, tmp_path):
        """When config has no conflict_resolution_strategy, default to rebase_first."""
        config = self._make_diverged_config(temp_git_repo, tmp_path)
        del config["conflict_resolution_strategy"]
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["success"] is True
        assert "rebase" in result["message"]


# ---------------------------------------------------------------------------
# SEE-869 Phase 3: branch guard (ensure main repo on master before sync)
# ---------------------------------------------------------------------------


def _create_branch(repo_path: str, name: str) -> None:
    """Create and check out a new branch off the current HEAD."""
    _run_git(repo_path, ["checkout", "-b", name])


class TestGetCurrentBranch:
    def test_returns_master_when_on_master(self, temp_git_repo):
        repo = temp_git_repo["local"]
        assert get_current_branch(repo) == "master"

    def test_returns_other_branch_name(self, temp_git_repo):
        repo = temp_git_repo["local"]
        _create_branch(repo, "feature-x")
        assert get_current_branch(repo) == "feature-x"

    def test_returns_head_for_detached(self, temp_git_repo):
        repo = temp_git_repo["local"]
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _run_git(repo, ["checkout", "--detach", sha])
        assert get_current_branch(repo) == "HEAD"


class TestHasGitOperationInProgress:
    def test_clean_repo_has_no_operation(self, temp_git_repo):
        repo = temp_git_repo["local"]
        assert has_git_operation_in_progress(repo) is False

    def test_detects_merge_in_progress(self, temp_git_repo):
        repo = temp_git_repo["local"]
        # Put a commit on master first so the merge is not a fast-forward
        _create_branch(repo, "merge-branch")
        Path(os.path.join(repo, "branch.txt")).write_text("b")
        _run_git(repo, ["add", "branch.txt"])
        _run_git(repo, ["commit", "-m", "branch commit"])
        _run_git(repo, ["checkout", "master"])
        Path(os.path.join(repo, "master.txt")).write_text("m")
        _run_git(repo, ["add", "master.txt"])
        _run_git(repo, ["commit", "-m", "master commit"])
        # No-ff merge with --no-commit keeps MERGE_HEAD around
        _run_git(repo, ["merge", "--no-ff", "--no-commit", "merge-branch"])

        assert has_git_operation_in_progress(repo) is True
        _run_git(repo, ["merge", "--abort"])  # cleanup


class TestHasSubmoduleChanges:
    def test_no_gitmodules_returns_false(self, temp_git_repo):
        repo = temp_git_repo["local"]
        assert has_submodule_changes(repo) is False


class TestIsMasterWorktreeOccupied:
    def test_single_repo_not_occupied(self, temp_git_repo):
        repo = temp_git_repo["local"]
        occupied, path = is_master_worktree_occupied(repo)
        assert occupied is False
        assert path is None


class TestSwitchToMaster:
    def test_switch_when_already_on_master(self, temp_git_repo):
        repo = temp_git_repo["local"]
        ok, msg = switch_to_master(repo)
        assert ok, f"switch failed: {msg}"
        assert get_current_branch(repo) == "master"

    def test_switch_from_other_branch(self, temp_git_repo):
        repo = temp_git_repo["local"]
        _create_branch(repo, "feature-y")
        ok, msg = switch_to_master(repo)
        assert ok, f"switch failed: {msg}"
        assert get_current_branch(repo) == "master"

    def test_switch_from_detached_head(self, temp_git_repo):
        repo = temp_git_repo["local"]
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _run_git(repo, ["checkout", "--detach", sha])
        ok, msg = switch_to_master(repo)
        assert ok, f"switch failed: {msg}"
        assert get_current_branch(repo) == "master"


class TestEnsureOnMasterBranch:
    def test_config_branch_not_master_returns_persistent(self, temp_git_repo):
        repo = temp_git_repo["local"]
        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "develop", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        assert ok is False
        assert "config branch is 'develop'" in msg
        assert had_stash is False
        assert classify_error(msg) == "persistent"

    def test_already_on_master_is_noop(self, temp_git_repo):
        repo = temp_git_repo["local"]
        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        assert ok is True
        assert "already on master" in msg
        assert had_stash is False

    def test_dirty_tree_stashes_and_switches(self, temp_git_repo):
        repo = temp_git_repo["local"]
        _create_branch(repo, "dirty-branch")
        Path(os.path.join(repo, "uncommitted.txt")).write_text("local change")

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        assert ok is True, f"guard failed: {msg}"
        assert had_stash is True
        assert get_current_branch(repo) == "master"
        # Stash pop restores uncommitted.txt
        ok_pop, _ = do_stash_pop(repo)
        assert ok_pop
        assert os.path.exists(os.path.join(repo, "uncommitted.txt"))

    def test_dirty_tree_with_stash_disabled_returns_persistent(self, temp_git_repo):
        repo = temp_git_repo["local"]
        _create_branch(repo, "no-stash-branch")
        Path(os.path.join(repo, "uncommitted.txt")).write_text("local change")

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=False, dry_run=False, logger=logger,
        )
        assert ok is False
        assert "stash_on_dirty disabled" in msg
        assert had_stash is False
        assert classify_error(msg) == "persistent"
        assert get_current_branch(repo) == "no-stash-branch"

    def test_in_progress_merge_blocks(self, temp_git_repo):
        repo = temp_git_repo["local"]
        # Put a commit on master so a later merge is not a fast-forward
        _create_branch(repo, "merge-branch")
        _run_git(repo, ["checkout", "master"])
        Path(os.path.join(repo, "master.txt")).write_text("m")
        _run_git(repo, ["add", "master.txt"])
        _run_git(repo, ["commit", "-m", "master commit"])
        # Switch to merge-branch and start a no-ff --no-commit merge of master
        _run_git(repo, ["checkout", "merge-branch"])
        Path(os.path.join(repo, "branch.txt")).write_text("b")
        _run_git(repo, ["add", "branch.txt"])
        _run_git(repo, ["commit", "-m", "branch commit"])
        _run_git(repo, ["merge", "--no-ff", "--no-commit", "master"])

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        assert ok is False
        assert "in progress" in msg
        assert had_stash is False
        assert classify_error(msg) == "persistent"
        _run_git(repo, ["merge", "--abort"])

    def test_detached_head_switches_to_master(self, temp_git_repo):
        repo = temp_git_repo["local"]
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        _run_git(repo, ["checkout", "--detach", sha])

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        assert ok is True, f"guard failed: {msg}"
        assert had_stash is False
        assert get_current_branch(repo) == "master"

    def test_dry_run_does_not_mutate_repo(self, temp_git_repo):
        repo = temp_git_repo["local"]
        _create_branch(repo, "dry-branch")
        Path(os.path.join(repo, "uncommitted.txt")).write_text("dirty")

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=True, logger=logger,
        )
        assert ok is True
        assert "dry-run" in msg
        assert had_stash is False
        # Branch was not changed, dirty file still present
        assert get_current_branch(repo) == "dry-branch"
        assert os.path.exists(os.path.join(repo, "uncommitted.txt"))

    def test_worktree_occupation_is_transient_skip(self, temp_git_repo, tmp_path):
        repo = temp_git_repo["local"]
        # Move the main repo off master, then add a second worktree on master
        _create_branch(repo, "temp-feature")
        other_wt = str(tmp_path / "other-wt")
        _run_git(repo, ["worktree", "add", other_wt, "master"])

        logger = MagicMock()
        ok, msg, had_stash = ensure_on_master_branch(
            repo, "master", stash_on_dirty=True, dry_run=False, logger=logger,
        )
        # cleanup worktree before assertions so failure doesn't leak
        try:
            assert ok is False
            assert "another worktree" in msg
            assert had_stash is False
            assert classify_error(msg) == "transient"
        finally:
            _run_git(repo, ["worktree", "remove", "--force", other_wt], check=False)


class TestRunSyncBranchGuard:
    def _config(self, repo, tmp_path, branch="master"):
        return {
            "repo_path": repo,
            "remote": "origin",
            "branch": branch,
            "log_dir": str(tmp_path / "logs"),
            "state_file": str(tmp_path / "state.json"),
            "lock_file": str(tmp_path / "sync.lock"),
            "stash_on_dirty": True,
        }

    def test_run_sync_blocks_when_branch_not_master(self, temp_git_repo, tmp_path):
        repo = temp_git_repo["local"]
        config = self._config(repo, tmp_path, branch="develop")
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["success"] is False
        assert result["action"] == "branch_guard"
        assert "config branch is 'develop'" in result["message"]
        assert result["error_category"] == "persistent"

    def test_run_sync_switches_back_then_syncs(self, temp_git_repo, tmp_path):
        """If main repo drifted to another branch, guard switches back and proceeds."""
        repo = temp_git_repo["local"]
        _create_branch(repo, "drifted")
        config = self._config(repo, tmp_path)
        logger = MagicMock()

        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)
        assert result["success"] is True
        assert get_current_branch(repo) == "master"
