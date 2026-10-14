#!/usr/bin/env bash
# Single launcher for the godot-qa-toolkit dev tool chain (SEE-1406).
#
# Starts the repo-sync daemon in the background, then execs the multica daemon
# in the foreground so systemd's MainPID is the daemon process itself.
#
# Invoked as ExecStart by .dev/systemd/godot-qa-toolkit-start.service, or run
# manually:   bash .dev/launch/godot-qa-toolkit-start.sh
#
# Minimal 1-unit-1-script shape (mirrors the KOL implementation, SEE-894).
# The repo checkout path and user are HARDCODED below — edit them here if the
# WSL checkout moves. Known accepted tradeoffs:
#   * hardcoded WSL path / user (no per-machine override)
#   * SIGTERM coarse stop (no graceful drain of running agent tasks)
#   * no PID-file idempotency for repo-sync (rely on systemd single-instance)

set -euo pipefail

# Hardcoded main-repo checkout (tradeoff: no machine env override).
REPO_ROOT="${GQT_REPO_ROOT:-$HOME/tools/godot-qa-toolkit}"
DEV_DIR="$REPO_ROOT/.dev"
DAEMON_LOG="${GQT_DAEMON_LOG:-$HOME/.multica/daemon.log}"

# repo_sync_config.json uses the __GQT_REPO_ROOT__ sentinel; the daemon
# resolves it from this env var at config-load time.
export GQT_REPO_ROOT="$REPO_ROOT"

MULTICA="${MULTICA:-multica}"
if ! command -v "$MULTICA" &>/dev/null; then
    echo "[start] ERROR: multica not found in PATH" >&2
    exit 1
fi

mkdir -p "$DEV_DIR/logs" "$(dirname "$DAEMON_LOG")"

# repo-sync in the background. Detached subshell so it survives this script's
# exec; the daemon writes its own pid file from repo_sync_config.json.
echo "[start] Starting repo-sync daemon..."
( cd "$DEV_DIR" && \
  nohup python3 repo-sync/repo_sync_daemon.py --config repo-sync/repo_sync_config.json \
    >> "$DEV_DIR/logs/repo-sync-daemon.log" 2>&1 & )

# multica per-task routing affinity (same as 105 deployment): make the daemon
# launch the wrapper instead of plain claude, so each task run stamps
# x-multica-task-session-id (litellm session affinity key).
export MULTICA_CLAUDE_PATH="$HOME/.local/bin/multica-claude"

# multica daemon in the foreground: systemd supervises this PID directly.
echo "[start] Starting multica daemon in foreground (log: $DAEMON_LOG)..."
exec "$MULTICA" daemon start --foreground >> "$DAEMON_LOG" 2>&1
