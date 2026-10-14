"""Integration tests for SEE-476 bidirectional repo sync.

All tests run against INDEPENDENT temporary git repositories created under
pytest's tmp_path. They NEVER touch the real local main repo
$HOME/tools/godot-qa-toolkit (the canonical checkout)/: no probe files, no force-push, no
reflog pollution. Each temp repo is a (local clone + bare remote) pair that
is discarded automatically when the test ends.

Strategy:
  - temp_sync_repo fixture builds a local clone with a bare origin remote
  - every sync cycle runs repo_sync.py against a per-test config that points
    at the temp clone and writes pid/lock/state/logs under tmp_path
  - daemon lifecycle tests copy repo_sync.py into the temp clone's
    .dev/repo-sync/ so the daemon's repo-root-relative sync_script
    resolution still works
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

# Source-tree root (this file is at <root>/.dev/tests/integration/...).
SOURCE_ROOT = Path(__file__).resolve().parents[3]
SYNC_SCRIPT = SOURCE_ROOT / ".dev" / "repo-sync" / "repo_sync.py"
DAEMON_SCRIPT = SOURCE_ROOT / ".dev" / "repo-sync" / "repo_sync_daemon.py"
CONFIG_FILE = SOURCE_ROOT / ".dev" / "repo-sync" / "repo_sync_config.json"
HOOK_FILE = SOURCE_ROOT / ".githooks" / "post-commit"

_REPO_ROOT_SENTINEL = "__GQT_REPO_ROOT__"


def _run_git(cwd: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", cwd] + args,
        capture_output=True, text=True, check=check,
        encoding="utf-8", errors="replace",
    )


def _read_json(path: Path) -> dict[str, Any]:
    for _ in range(30):
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            time.sleep(0.2)
    raise RuntimeError(f"Could not read {path}")


def _wait_for_lock(lock_file: str, timeout: float = 15.0) -> None:
    """Wait until lock file is absent or unlocked."""
    import fcntl
    end = time.time() + timeout
    while time.time() < end:
        if not os.path.exists(lock_file):
            return
        try:
            fd = os.open(lock_file, os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            return
        except (OSError, BlockingIOError, FileNotFoundError):
            time.sleep(0.5)
    raise TimeoutError(f"Lock {lock_file} still held after {timeout}s")


def _kill_daemon(pid_file: str) -> None:
    if os.path.exists(pid_file):
        try:
            pid = Path(pid_file).read_text().strip()
            if pid.isdigit():
                os.kill(int(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        try:
            os.remove(pid_file)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Fixtures / config helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_sync_repo(tmp_path: Path) -> dict[str, str]:
    """Local clone backed by a bare remote, fully isolated from the main repo."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()

    _run_git(str(remote), ["init", "--bare", "-b", "master"])
    _run_git(str(local), ["init", "-b", "master"])
    _run_git(str(local), ["config", "user.email", "test@test.com"])
    _run_git(str(local), ["config", "user.name", "Test"])
    (local / "README.md").write_text("init")
    _run_git(str(local), ["add", "README.md"])
    _run_git(str(local), ["commit", "-m", "init"])
    _run_git(str(local), ["remote", "add", "origin", str(remote)])
    _run_git(str(local), ["push", "-u", "origin", "master"])

    return {"local": str(local), "remote": str(remote), "root": str(tmp_path)}


def _make_config(local: str, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "repo_path": local,
        "remote": "origin",
        "branch": "master",
        "log_dir": str(tmp_path / "logs"),
        "state_file": str(tmp_path / "state.json"),
        "lock_file": str(tmp_path / "sync.lock"),
        "pid_file": str(tmp_path / "daemon.pid"),
        "stash_on_dirty": True,
        "conflict_resolution_strategy": "rebase_first",
        "daemon_poll_interval_seconds": 1,
        "daemon_max_consecutive_failures": 3,
        "daemon_backoff_max_seconds": 10,
        "subprocess_timeout_seconds": 30,
    }
    cfg.update(overrides)
    return cfg


def _write_config(cfg: dict[str, Any], tmp_path: Path) -> str:
    p = tmp_path / "repo_sync_config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def _sync(config_path: str, *extra_args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run repo_sync.py against a temp config (never the main repo)."""
    args = [sys.executable, str(SYNC_SCRIPT), "--config", config_path,
            "--lock-timeout", "10"] + list(extra_args)
    return subprocess.run(
        args, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _make_remote_commit(temp_sync_repo: dict[str, str], filename: str, content: str) -> None:
    """Push a commit via a second clone of the bare remote (simulate remote change)."""
    remote = temp_sync_repo["remote"]
    root = temp_sync_repo["root"]
    clone = os.path.join(root, "clone_remote")
    os.makedirs(clone)
    _run_git(clone, ["clone", remote, "."], check=True)
    _run_git(clone, ["config", "user.email", "test@test.com"])
    _run_git(clone, ["config", "user.name", "Test"])
    Path(clone, filename).write_text(content)
    _run_git(clone, ["add", filename])
    _run_git(clone, ["commit", "-m", f"remote: {filename}"])
    _run_git(clone, ["push"])


# ---------------------------------------------------------------------------
# 1. Deploy verification (source-tree relative, read-only)
# ---------------------------------------------------------------------------


class TestDeployVerification:
    """Verify sync components ship in the source tree (paths resolved from
    this test file, never the /mnt/d main repo)."""

    def test_sync_script_exists(self) -> None:
        assert SYNC_SCRIPT.exists()
        assert os.access(SYNC_SCRIPT, os.X_OK)

    def test_daemon_script_exists(self) -> None:
        assert DAEMON_SCRIPT.exists()
        assert os.access(DAEMON_SCRIPT, os.X_OK)

    def test_config_valid(self) -> None:
        assert CONFIG_FILE.exists()
        cfg = json.loads(CONFIG_FILE.read_text())
        for k in ["repo_path", "remote", "branch", "lock_file", "pid_file",
                   "log_dir", "state_file", "daemon_poll_interval_seconds"]:
            assert k in cfg, f"Missing key: {k}"

    def test_config_repo_path_uses_sentinel(self) -> None:
        """Shipped config is machine-neutral: repo_path is the sentinel
        resolved from GQT_REPO_ROOT at runtime, not a hardcoded /mnt/d path."""
        cfg = json.loads(CONFIG_FILE.read_text())
        assert cfg["repo_path"].rstrip("/").endswith(_REPO_ROOT_SENTINEL), (
            "repo_sync_config.json repo_path must use the "
            f"{_REPO_ROOT_SENTINEL} sentinel, got {cfg['repo_path']!r}"
        )

    def test_hook_installed(self) -> None:
        assert HOOK_FILE.exists()
        assert os.access(HOOK_FILE, os.X_OK)

    def test_imports_cleanly(self) -> None:
        r = subprocess.run(
            [sys.executable, "-W", "error", "-c",
             "import importlib.util; "
             f"spec=importlib.util.spec_from_file_location('m','{SYNC_SCRIPT}'); "
             "mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr


# ---------------------------------------------------------------------------
# 2. Sync state detection and direction control
# ---------------------------------------------------------------------------


class TestSyncStateDetection:
    """Test sync state detection against a temp clone."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_sync_repo, tmp_path: Path) -> None:
        self.repo = temp_sync_repo["local"]
        self.tmp = tmp_path
        self.config = _make_config(self.repo, tmp_path)
        self.config_path = _write_config(self.config, tmp_path)
        yield

    def test_synced_state_detected(self) -> None:
        r = _sync(self.config_path, "--dry-run")
        assert r.returncode == 0, r.stdout
        state = _read_json(Path(self.config["state_file"]))
        assert state["state"] == "SYNCED"

    def test_local_ahead_detected(self) -> None:
        probe = Path(self.repo) / "test_local_ahead.txt"
        probe.write_text(f"probe {time.time()}")
        _run_git(self.repo, ["add", str(probe)])
        _run_git(self.repo, ["commit", "-m", "test: local ahead"])

        r = _sync(self.config_path, "--dry-run")
        assert r.returncode == 0, r.stdout
        state = _read_json(Path(self.config["state_file"]))
        assert state["state"] == "LOCAL_AHEAD"

    def test_remote_ahead_detected(self, temp_sync_repo) -> None:
        _make_remote_commit(temp_sync_repo, "test_remote_ahead.txt", f"probe {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        r = _sync(self.config_path, "--dry-run")
        assert r.returncode == 0, r.stdout
        state = _read_json(Path(self.config["state_file"]))
        assert state["state"] == "REMOTE_AHEAD", f"Got {state['state']}"

    def test_diverged_detected(self, temp_sync_repo) -> None:
        _make_remote_commit(temp_sync_repo, "test_div_remote.txt", f"remote {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        probe_l = Path(self.repo) / "test_div_local.txt"
        probe_l.write_text(f"local {time.time()}")
        _run_git(self.repo, ["add", str(probe_l)])
        _run_git(self.repo, ["commit", "-m", "test: diverged local"])

        r = _sync(self.config_path, "--dry-run")
        assert r.returncode == 1, "DIVERGED should return exit code 1"
        state = _read_json(Path(self.config["state_file"]))
        assert state["state"] == "DIVERGED", f"Got {state['state']}"
        assert state["success"] is False


# ---------------------------------------------------------------------------
# 3. Bidirectional sync E2E
# ---------------------------------------------------------------------------


class TestBidirectionalSyncE2E:
    """Test actual push/pull operations on a temp clone."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_sync_repo, tmp_path: Path) -> None:
        self.repo = temp_sync_repo["local"]
        self.remote = temp_sync_repo["remote"]
        self.tmp = tmp_path
        self.config = _make_config(self.repo, tmp_path)
        self.config_path = _write_config(self.config, tmp_path)
        yield

    def test_push_only_sync(self) -> None:
        probe = Path(self.repo) / "test_push.txt"
        probe.write_text(f"push {time.time()}")
        _run_git(self.repo, ["add", str(probe)])
        _run_git(self.repo, ["commit", "-m", "test: push sync"])

        r = _sync(self.config_path, "--direction", "push-only")
        assert r.returncode == 0, r.stdout

        _run_git(self.repo, ["fetch", "origin"])
        local = _run_git(self.repo, ["rev-parse", "master"]).stdout.strip()
        remote = _run_git(self.repo, ["rev-parse", "origin/master"]).stdout.strip()
        assert local == remote

    def test_bidirectional_pushes_local_ahead(self) -> None:
        probe = Path(self.repo) / "test_bidir_push.txt"
        probe.write_text(f"bidir {time.time()}")
        _run_git(self.repo, ["add", str(probe)])
        _run_git(self.repo, ["commit", "-m", "test: bidir push"])

        r = _sync(self.config_path)
        assert r.returncode == 0, r.stdout

        _run_git(self.repo, ["fetch", "origin"])
        assert _run_git(self.repo, ["rev-parse", "master"]).stdout.strip() == \
               _run_git(self.repo, ["rev-parse", "origin/master"]).stdout.strip()

    def test_pull_ff_only(self, temp_sync_repo) -> None:
        _make_remote_commit(temp_sync_repo, "test_pull.txt", f"pull {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        r = _sync(self.config_path)
        assert r.returncode == 0, r.stdout

        local = _run_git(self.repo, ["rev-parse", "master"]).stdout.strip()
        remote = _run_git(self.repo, ["rev-parse", "origin/master"]).stdout.strip()
        assert local == remote

    def test_pull_success_is_logged(self, temp_sync_repo) -> None:
        """SEE-894: REMOTE_AHEAD pull success must be visible in stdout
        (so the daemon's captured stdout reflects it), not only in state."""
        _make_remote_commit(temp_sync_repo, "test_pull_log.txt", f"pull {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        r = _sync(self.config_path)
        assert r.returncode == 0, r.stdout
        assert "pull success" in r.stdout.lower(), (
            "pull success must be logged to stdout for daemon visibility"
        )

    def test_push_only_skips_remote_ahead(self, temp_sync_repo) -> None:
        before = _run_git(self.repo, ["rev-parse", "master"]).stdout.strip()
        _make_remote_commit(temp_sync_repo, "test_skip_pull.txt", f"skip {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        r = _sync(self.config_path, "--direction", "push-only")
        assert r.returncode == 0

        # Local unchanged (push-only does not pull)
        assert _run_git(self.repo, ["rev-parse", "master"]).stdout.strip() == before

    def test_diverged_blocks_all_operations(self, temp_sync_repo) -> None:
        # block_only strategy: DIVERGED must not push, pull, or rebase — exit 1.
        cfg = _make_config(self.repo, self.tmp, conflict_resolution_strategy="block_only")
        cfg_path = _write_config(cfg, self.tmp)

        _make_remote_commit(temp_sync_repo, "test_bl_remote.txt", f"r {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        pl = Path(self.repo) / "test_bl_local.txt"
        pl.write_text(f"l {time.time()}")
        _run_git(self.repo, ["add", str(pl)])
        _run_git(self.repo, ["commit", "-m", "test: bl local"])

        local_before = _run_git(self.repo, ["rev-parse", "master"]).stdout.strip()
        r = _sync(cfg_path)
        assert r.returncode == 1
        assert _run_git(self.repo, ["rev-parse", "master"]).stdout.strip() == local_before


# ---------------------------------------------------------------------------
# 4. Concurrent flock safety
# ---------------------------------------------------------------------------


class TestConcurrentFlock:
    """Verify flock prevents concurrent sync operations (temp lock file)."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_sync_repo, tmp_path: Path) -> None:
        self.repo = temp_sync_repo["local"]
        self.tmp = tmp_path
        self.config = _make_config(self.repo, tmp_path)
        self.config_path = _write_config(self.config, tmp_path)
        self.lock_file = self.config["lock_file"]
        yield

    def test_locked_sync_is_rejected(self) -> None:
        import fcntl
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = _sync(self.config_path, "--lock-timeout", "2")
            assert r.returncode == 1
            assert "Could not acquire lock" in r.stdout
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            try:
                os.remove(self.lock_file)
            except OSError:
                pass

    def test_parallel_processes_no_corruption(self) -> None:
        procs = [
            subprocess.Popen(
                [sys.executable, str(SYNC_SCRIPT), "--config", self.config_path],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for _ in range(5)
        ]
        codes = [p.wait() for p in procs]
        assert 0 in codes, "At least one sync should succeed"
        state = _read_json(Path(self.config["state_file"]))
        assert "state" in state


# ---------------------------------------------------------------------------
# 5. Daemon lifecycle (temp clone + temp config + temp pid/lock)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_daemon_repo(tmp_path: Path) -> dict[str, str]:
    """temp_sync_repo plus a copy of repo_sync.py under <local>/.dev/ so the
    daemon's repo-root-relative sync_script resolution points at a real file."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    remote.mkdir()
    local.mkdir()
    _run_git(str(remote), ["init", "--bare", "-b", "master"])
    _run_git(str(local), ["init", "-b", "master"])
    _run_git(str(local), ["config", "user.email", "test@test.com"])
    _run_git(str(local), ["config", "user.name", "Test"])
    (local / "README.md").write_text("init")
    _run_git(str(local), ["add", "README.md"])
    _run_git(str(local), ["commit", "-m", "init"])
    _run_git(str(local), ["remote", "add", "origin", str(remote)])
    _run_git(str(local), ["push", "-u", "origin", "master"])

    dev_dir = local / ".dev" / "repo-sync"
    dev_dir.mkdir(parents=True)
    shutil.copy(SYNC_SCRIPT, dev_dir / "repo_sync.py")
    return {"local": str(local), "remote": str(remote), "root": str(tmp_path)}


class TestDaemonLifecycle:
    """Test daemon start, stop, PID lock, crash recovery — all against a
    temp clone with a temp config. Never the main repo."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_daemon_repo, tmp_path: Path) -> None:
        self.repo = temp_daemon_repo["local"]
        self.tmp = tmp_path
        self.config = _make_config(self.repo, tmp_path)
        self.config_path = _write_config(self.config, tmp_path)
        self.pid_file = self.config["pid_file"]
        _kill_daemon(self.pid_file)
        yield
        _kill_daemon(self.pid_file)

    def _start_daemon(self) -> subprocess.Popen:
        return subprocess.Popen(
            [sys.executable, str(DAEMON_SCRIPT), "--config", self.config_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_daemon_starts_writes_pid_runs_cycles(self) -> None:
        proc = self._start_daemon()
        try:
            time.sleep(4)
            assert os.path.exists(self.pid_file)
            pid = int(Path(self.pid_file).read_text().strip())
            os.kill(pid, 0)  # running?
            state_path = Path(self.config["state_file"])
            assert state_path.exists()
            assert "state" in _read_json(state_path)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def test_duplicate_daemon_rejected(self) -> None:
        p1 = self._start_daemon()
        try:
            time.sleep(3)
            r = subprocess.run(
                [sys.executable, str(DAEMON_SCRIPT), "--config", self.config_path],
                capture_output=True, text=True, timeout=10,
            )
            assert r.returncode != 0
        finally:
            p1.terminate()
            p1.wait(timeout=10)

    def test_graceful_sigterm(self) -> None:
        proc = self._start_daemon()
        time.sleep(3)
        assert proc.poll() is None
        proc.terminate()
        proc.wait(timeout=10)
        time.sleep(1)
        assert not os.path.exists(self.pid_file)

    def test_crash_recovery(self) -> None:
        p1 = self._start_daemon()
        p2 = None
        try:
            time.sleep(3)
            p1.kill()
            p1.wait()

            p2 = self._start_daemon()
            time.sleep(3)
            assert p2.poll() is None, "New daemon should run after crash recovery"
            p2.terminate()
            p2.wait(timeout=10)
        except Exception:
            for p in [p1, p2]:
                if p:
                    try:
                        p.kill()
                    except Exception:
                        pass
            raise


# ---------------------------------------------------------------------------
# 6. Stash protection
# ---------------------------------------------------------------------------


class TestStashProtection:
    """Verify dirty working tree is protected during pull (temp clone)."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_sync_repo, tmp_path: Path) -> None:
        self.repo = temp_sync_repo["local"]
        self.tmp = tmp_path
        self.config = _make_config(self.repo, tmp_path)
        self.config_path = _write_config(self.config, tmp_path)
        yield

    def test_stash_pull_pop_preserves_dirty_files(self, temp_sync_repo) -> None:
        _make_remote_commit(temp_sync_repo, "test_stash_remote.txt", f"remote {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        dirty = Path(self.repo) / "test_dirty_untracked.txt"
        dirty_content = f"dirty {time.time()}"
        dirty.write_text(dirty_content)

        _wait_for_lock(self.config["lock_file"])
        r = _sync(self.config_path)
        assert r.returncode == 0, f"Pull failed: {r.stdout}"

        assert dirty.exists(), "Dirty file lost!"
        assert dirty.read_text() == dirty_content, "Dirty content changed!"

        _run_git(self.repo, ["fetch", "origin"])
        assert _run_git(self.repo, ["rev-parse", "master"]).stdout.strip() == \
               _run_git(self.repo, ["rev-parse", "origin/master"]).stdout.strip()

    def test_stash_disabled_rejects_dirty_pull(self, temp_sync_repo) -> None:
        _make_remote_commit(temp_sync_repo, "test_nostash_remote.txt", f"nostash {time.time()}")
        _run_git(self.repo, ["fetch", "origin"])

        dirty = Path(self.repo) / "test_nostash_dirty.txt"
        dirty.write_text("dirty")

        cfg = _make_config(self.repo, self.tmp, stash_on_dirty=False)
        cfg_path = _write_config(cfg, self.tmp)

        _wait_for_lock(cfg["lock_file"])
        r = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--config", cfg_path, "--lock-timeout", "10"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
        assert r.returncode != 0, "Should reject dirty pull when stash disabled"
        assert "dirty" in r.stdout.lower() or r.returncode != 0
