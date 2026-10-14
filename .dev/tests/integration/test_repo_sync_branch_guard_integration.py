"""Integration tests for SEE-869 branch guard.

All tests run against temporary git repositories created at runtime.
They NEVER touch the real local main repo $HOME/tools/godot-qa-toolkit (the canonical checkout)/.

Coverage targets from Phase 4:
  - dirty tree on non-master branch auto-switches back
  - merge in-progress fails (persistent)
  - detached HEAD switches back to master
  - submodule intermediate state fails (persistent)
  - worktree occupation is transient skip
  - config branch != master fails (persistent)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

_REPO_SYNC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "repo-sync")
)
_REPO_SYNC_SCRIPT = os.path.join(_REPO_SYNC_DIR, "repo_sync.py")

sys.path.insert(0, _REPO_SYNC_DIR)

from repo_sync import (
    Direction,
    classify_error,
    get_current_branch,
    run_sync,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _run_git(cwd: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd] + args,
        capture_output=True,
        text=True,
        check=check,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def temp_sync_repo(tmp_path: Path):
    """Create a temporary git repo with a bare remote and a local clone."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()

    _run_git(str(remote), ["init", "--bare", "-b", "master"])
    _run_git(str(local), ["init", "-b", "master"])
    _run_git(str(local), ["config", "user.email", "test@test.com"])
    _run_git(str(local), ["config", "user.name", "Test"])

    (local / "a.txt").write_text("a")
    _run_git(str(local), ["add", "a.txt"])
    _run_git(str(local), ["commit", "-m", "init"])
    _run_git(str(local), ["remote", "add", "origin", str(remote)])
    _run_git(str(local), ["push", "-u", "origin", "master"])

    return {"local": str(local), "remote": str(remote), "root": str(tmp_path)}


def _config(repo: str, tmp_path: Path, branch: str = "master") -> dict[str, Any]:
    return {
        "repo_path": repo,
        "remote": "origin",
        "branch": branch,
        "log_dir": str(tmp_path / "logs"),
        "state_file": str(tmp_path / "state.json"),
        "lock_file": str(tmp_path / "sync.lock"),
        "pid_file": str(tmp_path / "daemon.pid"),
        "stash_on_dirty": True,
        "conflict_resolution_strategy": "rebase_first",
        "daemon_poll_interval_seconds": 1,
        "daemon_max_consecutive_failures": 3,
    }


class _QuietLogger:
    """Minimal logger usable by run_sync in tests."""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# 1. Non-master branch dirty tree auto-switches back and syncs
# ---------------------------------------------------------------------------


class TestDirtyTreeAutoSwitch:
    def test_dirty_tree_on_feature_branch_switches_back_and_pulls(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]

        # Push an update to origin/master so there is something to pull
        _run_git(repo, ["checkout", "-b", "feature/dirty"])
        (Path(repo) / "remote.txt").write_text("remote change")
        _run_git(repo, ["add", "remote.txt"])
        _run_git(repo, ["commit", "-m", "remote change"])
        _run_git(repo, ["push", "origin", "master"])

        # Rewind local master behind origin
        init_sha = _run_git(repo, ["rev-parse", "HEAD~1"], check=True).stdout.strip()
        _run_git(repo, ["checkout", "master"])
        _run_git(repo, ["reset", "--hard", init_sha])

        # Switch to feature branch and dirty the tree
        _run_git(repo, ["checkout", "-b", "feature/dirty2"])
        dirty_file = Path(repo) / "dirty.txt"
        dirty_content = "uncommitted local change"
        dirty_file.write_text(dirty_content)

        config = _config(repo, tmp_path)
        logger = _QuietLogger()
        result = run_sync(config, logger, direction=Direction.BIDIRECTIONAL)

        assert result["success"] is True, f"sync failed: {result}"
        assert get_current_branch(repo) == "master"
        assert dirty_file.exists(), "dirty file should be restored after stash pop"
        assert dirty_file.read_text() == dirty_content

        # Verify pulled the remote commit
        _run_git(repo, ["fetch", "origin"])
        assert _run_git(repo, ["rev-parse", "master"]).stdout.strip() == \
               _run_git(repo, ["rev-parse", "origin/master"]).stdout.strip()


# ---------------------------------------------------------------------------
# 2. Merge in-progress fails (persistent)
# ---------------------------------------------------------------------------


class TestMergeInProgressBlocks:
    def test_merge_in_progress_blocks_sync(self, temp_sync_repo, tmp_path: Path) -> None:
        repo = temp_sync_repo["local"]

        # Put a commit on master so merge is not fast-forward
        _run_git(repo, ["checkout", "-b", "feature/merge"])
        (Path(repo) / "master.txt").write_text("m")
        _run_git(repo, ["checkout", "master"])
        (Path(repo) / "master.txt").write_text("m")
        _run_git(repo, ["add", "master.txt"])
        _run_git(repo, ["commit", "-m", "master commit"])
        _run_git(repo, ["push", "origin", "master"])

        # Switch to feature branch and start a non-fast-forward merge
        _run_git(repo, ["checkout", "feature/merge"])
        (Path(repo) / "branch.txt").write_text("b")
        _run_git(repo, ["add", "branch.txt"])
        _run_git(repo, ["commit", "-m", "branch commit"])
        _run_git(repo, ["merge", "--no-ff", "--no-commit", "master"])

        config = _config(repo, tmp_path)
        logger = _QuietLogger()
        result = run_sync(config, logger)

        assert result["success"] is False
        assert result["action"] == "branch_guard"
        assert "in progress" in result["message"].lower()
        assert result["error_category"] == "persistent"


# ---------------------------------------------------------------------------
# 3. Detached HEAD switches back to master
# ---------------------------------------------------------------------------


class TestDetachedHeadSwitchesBack:
    def test_detached_head_switches_back(self, temp_sync_repo, tmp_path: Path) -> None:
        repo = temp_sync_repo["local"]
        sha = _run_git(repo, ["rev-parse", "HEAD"], check=True).stdout.strip()
        _run_git(repo, ["checkout", "--detach", sha])
        assert get_current_branch(repo) == "HEAD"

        config = _config(repo, tmp_path)
        logger = _QuietLogger()
        result = run_sync(config, logger)

        assert result["success"] is True, f"sync failed: {result}"
        assert get_current_branch(repo) == "master"


# ---------------------------------------------------------------------------
# 4. Submodule intermediate state fails (persistent)
# ---------------------------------------------------------------------------


class TestSubmoduleIntermediateStateBlocks:
    def test_submodule_with_uninitialized_state_blocks_sync(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]
        sub = Path(temp_sync_repo["root"]) / "subproject"
        sub.mkdir()

        # Create a tiny repo to use as submodule
        _run_git(str(sub), ["init", "-b", "master"])
        _run_git(str(sub), ["config", "user.email", "sub@test.com"])
        _run_git(str(sub), ["config", "user.name", "Sub"])
        (sub / "s.txt").write_text("s")
        _run_git(str(sub), ["add", "s.txt"])
        commit_result = _run_git(str(sub), ["commit", "-m", "sub init"], check=True)
        sub_sha = _run_git(str(sub), ["rev-parse", "HEAD"], check=True).stdout.strip()

        # Manually register the submodule on master (avoiding git submodule add
        # because some test runners disable file:// protocol transport).
        _run_git(repo, ["config", "protocol.file.allow", "always"])
        (Path(repo) / ".gitmodules").write_text(
            '[submodule "subproject"]\n\tpath = subproject\n\turl = {}\n'.format(str(sub))
        )
        _run_git(repo, ["add", ".gitmodules"])
        _run_git(
            repo,
            [
                "update-index",
                "--add",
                "--cacheinfo",
                "160000",
                sub_sha,
                "subproject",
            ],
        )
        _run_git(repo, ["commit", "-m", "add submodule"])
        _run_git(
            repo,
            ["push", "origin", "HEAD:refs/heads/master"],
            check=True,
        )

        # Drift to another branch and put submodule in uninitialized state.
        # Keep the submodule entry in the index but make the directory empty
        # (no nested .git, no checked-out content) so `git submodule status`
        # reports '-' (uninitialized).
        _run_git(repo, ["checkout", "-b", "feature/sub"])
        submodule_path = Path(repo) / "subproject"
        if submodule_path.exists():
            shutil.rmtree(str(submodule_path))
        submodule_path.mkdir(parents=True, exist_ok=True)

        config = _config(repo, tmp_path)
        logger = _QuietLogger()
        result = run_sync(config, logger)

        assert result["success"] is False
        assert result["action"] == "branch_guard"
        assert "submodule" in result["message"].lower()
        assert result["error_category"] == "persistent"


# ---------------------------------------------------------------------------
# 5. Worktree occupation is transient skip
# ---------------------------------------------------------------------------


class TestWorktreeOccupationTransient:
    def test_master_checked_out_in_other_worktree_is_transient_skip(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]
        other_wt = str(tmp_path / "other-wt")

        # Drift repo off master
        _run_git(repo, ["checkout", "-b", "feature/wt"])

        # Add a second worktree checked out on master
        _run_git(repo, ["worktree", "add", other_wt, "master"])
        try:
            config = _config(repo, tmp_path)
            logger = _QuietLogger()
            result = run_sync(config, logger)

            assert result["success"] is False
            assert result["action"] == "branch_guard"
            assert "another worktree" in result["message"].lower()
            assert result["error_category"] == "transient"
        finally:
            _run_git(repo, ["worktree", "remove", "--force", other_wt], check=False)


# ---------------------------------------------------------------------------
# 6. Config branch != master fails (persistent)
# ---------------------------------------------------------------------------


class TestConfigBranchNotMaster:
    def test_config_branch_not_master_fails_persistent(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]
        _run_git(repo, ["checkout", "-b", "develop"])

        config = _config(repo, tmp_path, branch="develop")
        logger = _QuietLogger()
        result = run_sync(config, logger)

        assert result["success"] is False
        assert result["action"] == "branch_guard"
        assert "config branch is 'develop'" in result["message"]
        assert result["error_category"] == "persistent"


# ---------------------------------------------------------------------------
# 7. End-to-end via repo_sync.py CLI against temporary repo
# ---------------------------------------------------------------------------


class TestBranchGuardCLI:
    def test_cli_auto_recover_from_non_master_branch(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]
        config_path = tmp_path / "repo_sync_config.json"
        config = _config(repo, tmp_path)
        config_path.write_text(json.dumps(config))

        _run_git(repo, ["checkout", "-b", "feature/cli"])
        (Path(repo) / "cli.txt").write_text("dirty")

        result = subprocess.run(
            [sys.executable, _REPO_SYNC_SCRIPT,
             "--config", str(config_path), "--lock-timeout", "10"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

        assert result.returncode == 0, f"CLI failed: {result.stdout}\n{result.stderr}"
        assert get_current_branch(repo) == "master"

        state = json.loads(Path(config["state_file"]).read_text())
        assert state["success"] is True

    def test_cli_reports_persistent_when_merge_in_progress(
        self, temp_sync_repo, tmp_path: Path
    ) -> None:
        repo = temp_sync_repo["local"]
        config_path = tmp_path / "repo_sync_config.json"
        config = _config(repo, tmp_path)
        config_path.write_text(json.dumps(config))

        _run_git(repo, ["checkout", "-b", "feature/cli-merge"])
        (Path(repo) / "master.txt").write_text("m")
        _run_git(repo, ["checkout", "master"])
        (Path(repo) / "master.txt").write_text("m")
        _run_git(repo, ["add", "master.txt"])
        _run_git(repo, ["commit", "-m", "master commit"])
        _run_git(repo, ["push", "origin", "master"])
        _run_git(repo, ["checkout", "feature/cli-merge"])
        (Path(repo) / "branch.txt").write_text("b")
        _run_git(repo, ["add", "branch.txt"])
        _run_git(repo, ["commit", "-m", "branch commit"])
        _run_git(repo, ["merge", "--no-ff", "--no-commit", "master"])

        result = subprocess.run(
            [sys.executable, _REPO_SYNC_SCRIPT,
             "--config", str(config_path), "--lock-timeout", "10"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )

        assert result.returncode == 1
        state = json.loads(Path(config["state_file"]).read_text())
        assert state["success"] is False
        assert state["error_category"] == "persistent"
