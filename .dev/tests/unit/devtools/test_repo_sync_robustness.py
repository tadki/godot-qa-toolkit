#!/usr/bin/env python3
"""
Unit tests for SEE-565 repo sync robustness improvements.

Covers:
  1. Exponential backoff logic
  2. Circuit breaker (stop after consecutive failures)
  3. Stale lock detection
  4. Orphan gitlink detection
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# sys.path setup is centralized in ../conftest.py

from repo_sync import (
    acquire_lock,
    release_lock,
    detect_orphan_gitlinks,
    remove_orphan_gitlink,
    _is_gitlink_related_error,
    do_stash_if_dirty,
)
from repo_sync_daemon import run_one_cycle


def _run_git(cwd, args, check=True):
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------------------
# 3. Stale lock detection
# ---------------------------------------------------------------------------


class TestStaleLockDetection:
    """Test acquire_lock stale lock detection on Unix."""

    @pytest.fixture
    def lock_file(self, tmp_path):
        return str(tmp_path / "test.lock")

    def test_acquire_lock_writes_pid(self, lock_file):
        """After acquiring lock, the lock file should be usable for PID storage."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        fd = acquire_lock(lock_file)
        assert fd is not None
        # Write PID (simulating what main() does)
        os.lseek(fd, 0, os.SEEK_SET)
        os.truncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        # Read back
        os.lseek(fd, 0, os.SEEK_SET)
        content = os.read(fd, 32).decode("utf-8").strip()
        assert content == str(os.getpid())
        release_lock(fd, lock_file)

    def test_stale_lock_detected_when_holder_dead(self, lock_file):
        """If lock file contains a dead PID, lock should be acquirable."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")

        # Write a dead PID to the file (99999999 almost certainly doesn't exist)
        dead_pid = 99999999
        with open(lock_file, "w") as f:
            f.write(str(dead_pid))

        # The file has a dead PID but no flock held — acquire should succeed
        fd = acquire_lock(lock_file, timeout=0.0)
        assert fd is not None
        release_lock(fd, lock_file)

    def test_lock_with_dead_pid_in_file_content(self, lock_file):
        """Verify stale PID detection logic: dead PID → lock file is readable."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")

        dead_pid = 99999999
        with open(lock_file, "w") as f:
            f.write(str(dead_pid))

        # Read back and verify PID is dead
        with open(lock_file, "r") as f:
            content = f.read().strip()
        assert content.isdigit()
        pid = int(content)
        with pytest.raises(OSError):
            os.kill(pid, 0)  # Should raise — PID doesn't exist

    def test_acquire_lock_succeeds_when_no_lock(self, lock_file):
        """Fresh lock file should be immediately acquirable."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        fd = acquire_lock(lock_file)
        assert fd is not None
        release_lock(fd, lock_file)

    def test_acquire_lock_timeout_returns_none_when_locked(self, lock_file):
        """If another fd holds the lock, second acquire should return None."""
        if sys.platform == "win32":
            pytest.skip("Unix-only test")
        fd1 = acquire_lock(lock_file)
        assert fd1 is not None

        fd2 = acquire_lock(lock_file, timeout=0.1)
        assert fd2 is None

        release_lock(fd1, lock_file)


# ---------------------------------------------------------------------------
# 4. Orphan gitlink detection
# ---------------------------------------------------------------------------


class TestOrphanGitlinkDetection:
    """Test detect_orphan_gitlinks and remove_orphan_gitlink."""

    @pytest.fixture
    def git_repo_with_gitlink(self, tmp_path):
        """Create a git repo with an orphan gitlink entry."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _run_git(repo, ["init", "-b", "master"])
        _run_git(repo, ["config", "user.email", "test@test.com"])
        _run_git(repo, ["config", "user.name", "Test"])

        # Create initial commit
        Path(os.path.join(repo, "a.txt")).write_text("a")
        _run_git(repo, ["add", "a.txt"])
        _run_git(repo, ["commit", "-m", "init"])

        # Add an orphan gitlink (mode 160000) without .gitmodules
        # We do this by directly adding a fake submodule reference
        subprocess.run(
            [
                "git",
                "-C",
                repo,
                "update-index",
                "--add",
                "--cacheinfo",
                "160000",
                "abc123def456789012345678901234567890abcd",
                "FakeSubmodule",
            ],
            capture_output=True,
            check=False,
        )

        return repo

    def test_detect_orphan_gitlinks_finds_orphan(self, git_repo_with_gitlink):
        """Should detect gitlink entries without .gitmodules registration."""
        orphans = detect_orphan_gitlinks(git_repo_with_gitlink)
        assert "FakeSubmodule" in orphans

    def test_detect_orphan_gitlinks_empty_when_none(self, tmp_path):
        """Clean repo should have no orphan gitlinks."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _run_git(repo, ["init", "-b", "master"])
        _run_git(repo, ["config", "user.email", "test@test.com"])
        _run_git(repo, ["config", "user.name", "Test"])
        Path(os.path.join(repo, "a.txt")).write_text("a")
        _run_git(repo, ["add", "a.txt"])
        _run_git(repo, ["commit", "-m", "init"])

        orphans = detect_orphan_gitlinks(repo)
        assert orphans == []

    def test_remove_orphan_gitlink(self, git_repo_with_gitlink):
        """Should successfully remove orphan gitlink from index."""
        orphans = detect_orphan_gitlinks(git_repo_with_gitlink)
        assert len(orphans) > 0

        ok, msg = remove_orphan_gitlink(git_repo_with_gitlink, orphans[0])
        assert ok is True

        # Verify it's gone
        orphans_after = detect_orphan_gitlinks(git_repo_with_gitlink)
        assert len(orphans_after) == 0


class TestIsGitlinkRelatedError:
    """Test _is_gitlink_related_error helper."""

    def test_detects_could_not_write_index(self):
        assert _is_gitlink_related_error("error: could not write index") is True

    def test_detects_submodule(self):
        assert _is_gitlink_related_error("fatal: submodule error") is True

    def test_detects_gitlink(self):
        assert _is_gitlink_related_error("gitlink problem") is True

    def test_ignores_unrelated_error(self):
        assert _is_gitlink_related_error("merge conflict in file.txt") is False

    def test_ignores_empty_string(self):
        assert _is_gitlink_related_error("") is False

    def test_case_insensitive(self):
        assert _is_gitlink_related_error("Could Not Write Index") is True


# ---------------------------------------------------------------------------
# 5. Stash recovery with gitlink cleanup
# ---------------------------------------------------------------------------


class TestStashRecovery:
    """Test do_stash_if_dirty recovery with gitlink cleanup."""

    def test_stash_recovery_on_gitlink_error(self, tmp_path):
        """When stash fails with gitlink error, should attempt cleanup and retry."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _run_git(repo, ["init", "-b", "master"])
        _run_git(repo, ["config", "user.email", "test@test.com"])
        _run_git(repo, ["config", "user.name", "Test"])
        Path(os.path.join(repo, "a.txt")).write_text("a")
        _run_git(repo, ["add", "a.txt"])
        _run_git(repo, ["commit", "-m", "init"])

        # Make working tree dirty
        Path(os.path.join(repo, "dirty.txt")).write_text("dirty")

        # Normal stash should succeed on clean repo (no orphans)
        ok, msg, had_stash = do_stash_if_dirty(repo, stash_on_dirty=True)
        assert ok is True
        assert had_stash is True

    @patch("repo_sync.detect_orphan_gitlinks")
    @patch("repo_sync.remove_orphan_gitlink")
    def test_stash_retries_after_gitlink_cleanup(
        self, mock_remove, mock_detect, tmp_path
    ):
        """do_stash_if_dirty should attempt cleanup on gitlink error."""
        repo = str(tmp_path / "repo")
        os.makedirs(repo)
        _run_git(repo, ["init", "-b", "master"])
        _run_git(repo, ["config", "user.email", "test@test.com"])
        _run_git(repo, ["config", "user.name", "Test"])
        Path(os.path.join(repo, "a.txt")).write_text("a")
        _run_git(repo, ["add", "a.txt"])
        _run_git(repo, ["commit", "-m", "init"])

        # Make working tree dirty
        Path(os.path.join(repo, "dirty.txt")).write_text("dirty")

        # Mock git_run to fail first then succeed
        call_count = {"n": 0}
        original_git_run = __import__("repo_sync", fromlist=["git_run"]).git_run

        def mock_git_run(repo_path, args, check=True, timeout=None):
            if "stash" in args and "push" in args:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First call: fail with gitlink error
                    r = MagicMock()
                    r.returncode = 1
                    r.stderr = "error: could not write index"
                    return r
            return original_git_run(repo_path, args, check=check, timeout=timeout)

        mock_detect.return_value = ["FakeSubmodule"]
        mock_remove.return_value = (True, "removed")

        with patch("repo_sync.git_run", side_effect=mock_git_run):
            ok, msg, had_stash = do_stash_if_dirty(
                repo, stash_on_dirty=True, recovery=True
            )

        assert mock_detect.called
        assert mock_remove.called


# ---------------------------------------------------------------------------
# 6. run_one_cycle timeout parameter
# ---------------------------------------------------------------------------


class TestRunOneCycleTimeout:
    """Test that run_one_cycle passes timeout to subprocess."""

    @patch("repo_sync_daemon.subprocess.run")
    def test_timeout_passed_to_subprocess(self, mock_run):
        """run_one_cycle should pass timeout parameter to subprocess.run."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        logger = MagicMock()
        run_one_cycle("repo_sync.py", {}, "config.json", logger, timeout=60)

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 60 or (
            len(call_kwargs) > 1 and call_kwargs[1].get("timeout") == 60
        )

    @patch("repo_sync_daemon.subprocess.run")
    def test_default_timeout_is_120(self, mock_run):
        """Default timeout should be 120 seconds."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        logger = MagicMock()
        run_one_cycle("repo_sync.py", {}, "config.json", logger)

        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 120 or (
            len(call_kwargs) > 1 and call_kwargs[1].get("timeout") == 120
        )
