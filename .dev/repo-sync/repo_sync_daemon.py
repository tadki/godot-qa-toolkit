#!/usr/bin/env python3
"""Daemon process for repo bidirectional sync.

Runs repo_sync.py in a loop with configurable poll interval.
Supports nohup / systemd launch. Manages PID file and handles SIGTERM.

Usage:
  nohup python3 repo_sync_daemon.py --config repo_sync_config.json \\
      >> .dev/logs/repo-sync-daemon.log 2>&1 &

  # Or with systemd: see docs for unit file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

if sys.platform != "win32":
    import fcntl

from repo_sync import send_alert

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

# Sentinel replaced at config load time with the machine-specific main repo
# path (GQT_REPO_ROOT env var). Keeps repo_sync_config.json machine-neutral.
_REPO_ROOT_SENTINEL = "__GQT_REPO_ROOT__"


def _resolve_repo_path(raw: str) -> str:
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
    return raw


# Module-level flag for signal handler
_running = True


def _handle_signal(signum: int, _frame: object) -> None:
    global _running
    _running = False


def load_config(config_path: Path | str | None = None) -> dict:
    if config_path is None:
        config_path = Path(__file__).parent / "repo_sync_config.json"
    path = Path(config_path)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Resolve repo_path (may use the __GQT_REPO_ROOT__ sentinel) before deriving
    # relative paths against it.
    repo_root = Path(os.path.expanduser(_resolve_repo_path(cfg["repo_path"])))
    cfg["repo_path"] = str(repo_root)
    for key in ("log_dir", "state_file"):
        val = cfg.get(key, "")
        if val and not Path(val).is_absolute():
            cfg[key] = str(repo_root / val)
    return cfg


def acquire_pid_lock(pid_file: str) -> int | None:
    """Write current PID to pid_file with exclusive lock. Returns fd or None."""
    fd = os.open(pid_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        # Check if the existing process is still alive
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            existing = os.read(fd, 32).decode("utf-8").strip()
            if existing.isdigit():
                try:
                    os.kill(int(existing), 0)
                    os.close(fd)
                    return None
                except OSError:
                    pass
        except (OSError, ValueError):
            pass
        # Stale lock — we'll overwrite
        fcntl.flock(fd, fcntl.LOCK_EX)

    os.lseek(fd, 0, os.SEEK_SET)
    os.truncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release_pid_lock(fd: int | None, pid_file: str) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.remove(pid_file)
    except OSError:
        pass


def run_one_cycle(
    sync_script: str, config: dict, config_path: str, logger: logging.Logger, timeout: int = 120
) -> tuple[bool, str]:
    """Run repo_sync.py once. Returns (success, error_category).

    error_category is 'transient' or 'persistent' on failure, '' on success.
    """
    cmd = [sys.executable, sync_script, "--config", config_path]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if r.stdout:
            for line in r.stdout.strip().splitlines():
                logger.info("sync: %s", line)
        if r.returncode != 0:
            logger.warning(
                "sync cycle exited with code %d: %s",
                r.returncode,
                r.stderr.strip() if r.stderr else "",
            )
            category = _read_error_category(config, logger)
            return False, category
        return True, ""
    except subprocess.TimeoutExpired:
        logger.error("sync cycle timed out")
        return False, "transient"
    except Exception as e:
        logger.error("sync cycle exception: %s", e)
        return False, "persistent"


def _read_error_category(config: dict, logger: logging.Logger) -> str:
    """Read error_category from the state file written by repo_sync.py."""
    state_path = config.get("state_file", "")
    if not state_path:
        return "persistent"
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get("error_category", "persistent")
    except (OSError, json.JSONDecodeError, ValueError):
        return "persistent"


def main() -> int:
    parser = argparse.ArgumentParser(description="Repo sync daemon")
    parser.add_argument("--config", required=True, help="Path to repo_sync_config.json")
    args = parser.parse_args()

    config = load_config(args.config)

    pid_file = config.get("pid_file", "/tmp/godot-qa-toolkit-sync-daemon.pid")
    poll_interval = config.get("daemon_poll_interval_seconds", 5)
    max_failures = config.get("daemon_max_consecutive_failures", 10)
    backoff_max = config.get("daemon_backoff_max_seconds", 300)
    subprocess_timeout = config.get("subprocess_timeout_seconds", 120)
    log_dir = Path(config.get("log_dir", "logs"))

    # Setup logging
    logger = logging.getLogger("repo_sync_daemon")
    logger.setLevel(logging.DEBUG)
    logger.handlers = []

    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_dir / "repo-sync-daemon.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(ch)

    # PID lock
    fd = acquire_pid_lock(pid_file)
    if fd is None:
        logger.error(
            "Another daemon instance is already running (PID file: %s)", pid_file
        )
        return 1

    # Signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Resolve from config repo_path so the daemon survives worktree cleanup.
    # Using __file__ breaks when launched from a transient worktree copy.
    repo_root = Path(os.path.expanduser(config["repo_path"]))
    sync_script = str(repo_root / ".dev" / "repo-sync" / "repo_sync.py")
    logger.info(
        "Daemon started (PID %d, poll interval %ds, max_consecutive_failures %d)",
        os.getpid(),
        poll_interval,
        max_failures,
    )

    consecutive_failures = 0
    current_interval = poll_interval

    try:
        while _running:
            ok, error_category = run_one_cycle(
                sync_script, config, args.config, logger, timeout=subprocess_timeout
            )
            if ok:
                consecutive_failures = 0
                current_interval = poll_interval
            else:
                skip_count = error_category == "transient"
                if skip_count:
                    logger.warning(
                        "Sync cycle failed with transient error (category=%s). "
                        "Not counting toward circuit breaker (%d/%d).",
                        error_category,
                        consecutive_failures,
                        max_failures,
                    )
                else:
                    consecutive_failures += 1
                if consecutive_failures >= max_failures:
                    alert_msg = (
                        f"🚨 **Repo Sync Daemon Alert — Circuit breaker triggered**\n\n"
                        f"- PID: `{os.getpid()}`\n"
                        f"- Consecutive failures: `{consecutive_failures}`\n"
                        f"- Max allowed: `{max_failures}`\n"
                        f"- Daemon stopped to prevent infinite crash loop."
                    )
                    send_alert(config, alert_msg)
                    logger.error(
                        "Circuit breaker: %d consecutive failures >= threshold %d. Stopping daemon.",
                        consecutive_failures,
                        max_failures,
                    )
                    break
                # Exponential backoff: double interval, capped at backoff_max
                if not skip_count:
                    current_interval = min(current_interval * 2, backoff_max)
                logger.warning(
                    "Sync cycle failed (category=%s, counted=%s, %d/%d). Backing off to %ds.",
                    error_category,
                    "no" if skip_count else "yes",
                    consecutive_failures,
                    max_failures,
                    current_interval,
                )

            # Sleep in small increments to allow prompt signal response
            end = time.time() + current_interval
            while _running and time.time() < end:
                time.sleep(max(0.0, min(1.0, end - time.time())))
    except Exception as e:
        logger.error("Daemon crashed: %s", e)
        return 1
    finally:
        release_pid_lock(fd, pid_file)
        logger.info("Daemon stopped (PID %d)", os.getpid())

    return 0


if __name__ == "__main__":
    sys.exit(main())
