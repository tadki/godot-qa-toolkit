# SEE-1468 M5-a — see1288_fork_cli 收编（KOL pending -> bdd）。
# 原 KOL tests/devtests/see1288_spec/see1288_fork_cli.feature +
# see1288_fork_cli_steps.py 的普通 pytest 收编（断言 1:1 迁移，纯 Python
# 确定性检查：launcher/proxy 文本锚点、fork-CLI 真执行探针、doctor JSON）。
# KOL 仓定位：SEE1288_REPO env（QA 隔离缝）→ 缺省同级 KingOfLikes-Godot；
# KOL 树缺席时整模块 skip（gqt CI 无 KOL 检出 → skip 非红，双仓工作区绿）。

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

pytestmark = pytest.mark.skipif(
    os.environ.get("SEE1288_SKIP_KOL", "") == "1", reason="KOL-dependent BDD disabled"
)

scenarios("features/see1288_fork_cli.feature")


def _kol_repo() -> Path:
    env = os.environ.get("SEE1288_REPO")
    if env:
        return Path(env)
    # 本工作区双仓并排布局：gqt/../KingOfLikes-Godot
    sibling = Path(__file__).resolve().parents[3] / "KingOfLikes-Godot"
    if (sibling / "addons" / "godot_mcp" / "launch").is_dir():
        return sibling
    pytest.skip(
        "KOL checkout not found (set SEE1288_REPO to run these "
        "KOL-tree assertions)"
    )


@pytest.fixture(scope="module")
def kol_repo() -> Path:
    return _kol_repo()


@pytest.fixture(scope="module")
def ctx(kol_repo: Path) -> dict:
    del kol_repo  # 占位：全部步骤经 ctx 共享探测状态
    return {}


def _launch(kol_repo: Path) -> Path:
    return kol_repo / "addons" / "godot_mcp" / "launch"


def _launcher_path(kol_repo: Path) -> Path:
    env = os.environ.get("SEE1288_LAUNCHER")
    return Path(env) if env else _launch(kol_repo) / "godot-mcp-launcher.sh"


def _launcher_text(kol_repo: Path) -> str:
    return _launcher_path(kol_repo).read_text(encoding="utf-8")


def _server_dir(kol_repo: Path) -> str:
    override = os.environ.get("SEE1288_SERVER_DIR")
    if override:
        return override
    return str((_launcher_path(kol_repo).parent / ".." / "server").resolve())


def _proxy_text(kol_repo: Path) -> str:
    parts = []
    launch = _launch(kol_repo)
    entry = launch / "godot-mcp-proxy.mjs"
    if entry.is_file():
        parts.append(entry.read_text(encoding="utf-8"))
    proxy_dir = launch / "proxy"
    if proxy_dir.is_dir():
        for name in sorted(os.listdir(proxy_dir)):
            if name.endswith(".mjs"):
                parts.append((proxy_dir / name).read_text(encoding="utf-8"))
    return "\n".join(parts)


def _extract_build_fallback(launcher_text: str) -> str:
    lines = launcher_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith('if [[ ! -x "$FORK_CLI"'):
            start = i
            break
    if start is None:
        return ""
    depth = 0
    for j in range(start, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("if ") or stripped == "if [[" or stripped.startswith("if [["):
            depth += 1
        elif stripped == "fi" or stripped.startswith("fi;"):
            depth -= 1
            if depth == 0:
                return "\n".join(lines[start : j + 1])
    return ""


def _run_build_fallback(kol_repo: Path, dist_cli: str, server_dir: str):
    block = _extract_build_fallback(_launcher_text(kol_repo))
    if not block:
        return False, "build-fallback block not found in launcher text"
    godot_mcp_home = kol_repo / ".godot" / "kol_probe"
    godot_mcp_home.mkdir(parents=True, exist_ok=True)
    harness = (
        "set -uo pipefail\n"
        'log() { echo "[launcher-fallback] $*"; }\n'
        'log_stage() { echo "[launcher-stage] $*"; }\n'
        f"FORK_CLI={shlex.quote(dist_cli)}\n"
        f"FORK_SERVER_DIR={shlex.quote(server_dir)}\n"
        f"GODOT_MCP_HOME={shlex.quote(str(godot_mcp_home))}\n"
        "KOL_RUNTIME_ID=see1288-qa\n"
        f"{block}\n"
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=300)
    return True, (proc.stdout + proc.stderr)[-2000:]


def _verify_cli_executable(dist_cli: str) -> str:
    proc = subprocess.run(["node", dist_cli, "--version"], capture_output=True, text=True, timeout=60)
    version = (proc.stdout or "").strip()
    assert proc.returncode == 0 and version, (
        f"built fork CLI failed minimal execution probe (`--version` rc={proc.returncode}, "
        f"out={version!r}) — dist/cli.js is not a working executable (§SPEC-019)"
    )
    return version


# ---------- AC-MCP-001 ----------


@given("the godot-mcp submodule launch dir exists")
def launch_dir_exists(kol_repo: Path) -> None:
    assert _launch(kol_repo).is_dir()


@given("the fork CLI build products are absent from the submodule checkout")
def fork_cli_absent(kol_repo: Path) -> None:
    assert "fork CLI not found" in _launcher_text(kol_repo)


@when("the launcher resolves its fork CLI path")
def launcher_resolves(kol_repo: Path, ctx: dict) -> None:
    text = _launcher_text(kol_repo)
    assert "FORK_CLI=" in text and "GODOT_MCP_FORK_CLI" in text
    ctx["launcher_text"] = text


@then("the resolved fork CLI must be executable")
def resolved_executable(kol_repo: Path, ctx: dict) -> None:
    launcher_dir = _launcher_path(kol_repo).parent
    dist_cli = str((launcher_dir / ".." / "server" / "dist" / "cli.js").resolve())
    env_cli = os.environ.get("GODOT_MCP_FORK_CLI")
    candidates = [env_cli] if env_cli else []
    candidates.append(dist_cli)
    fork_cli = next((p for p in candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if fork_cli is None:
        block_found, harness_output = _run_build_fallback(kol_repo, dist_cli, _server_dir(kol_repo))
        assert block_found, (
            f"launcher build-fallback block not found and no executable fork CLI: {candidates}"
        )
        assert os.path.isfile(dist_cli) and os.access(dist_cli, os.X_OK), (
            f"build fallback ran but produced no executable {dist_cli}. tail: {harness_output!r}"
        )
        fork_cli = dist_cli
    ctx["fork_cli"] = fork_cli
    ctx["fork_cli_version"] = _verify_cli_executable(fork_cli)


@then("the fork wiring path must emit FORK_WIRED instead of the upstream fallback warning")
def fork_wired_emitted(kol_repo: Path, ctx: dict) -> None:
    text = ctx.get("launcher_text") or _launcher_text(kol_repo)
    assert ctx.get("fork_cli"), "fork CLI unresolved"
    assert "stage=FORK_WIRED" in text
    assert "GODOT_MCP_QUICK_TIMEOUT_MS:-90000" in text


# ---------- AC-MCP-002 ----------


@given("the fork CLI env override points at a missing path")
def override_missing(kol_repo: Path, ctx: dict) -> None:
    ctx["override"] = str(kol_repo / "tests" / "spec" / "_definitely_missing_cli.js")
    assert not os.path.exists(ctx["override"])


@when("the launcher fork wiring executes against that override")
def wiring_runs_override(kol_repo: Path) -> None:
    text = _launcher_text(kol_repo)
    assert '[[ -x "$FORK_CLI" ]]' in text, "fork -x guard missing"
    assert "WARNING: fork CLI not found" in text, "fallback WARNING missing"
    assert "exec node" in text and "godot-mcp-proxy.mjs" in text


@then("the upstream fallback warning branch is retained in the launcher")
def fallback_retained(kol_repo: Path) -> None:
    assert "WARNING: fork CLI not found" in _launcher_text(kol_repo)


@then("the wiring block must not abort the launcher on a missing fork")
def no_abort(kol_repo: Path) -> None:
    text = _launcher_text(kol_repo)
    m = re.search(r"FORK_CLI=.*?fi\n", text, re.S)
    assert m, "fork wiring block not found"
    assert "exit " not in m.group(0)


# ---------- AC-MCP-003 ----------


@given("the fork wiring is active")
def fork_wiring_active(kol_repo: Path) -> None:
    assert "GODOT_MCP_QUICK_TIMEOUT_MS" in _proxy_text(kol_repo)


@then("the proxy default-injects GODOT_MCP_QUICK_TIMEOUT_MS of 90000")
def proxy_90s(kol_repo: Path) -> None:
    assert (
        "process.env.GODOT_MCP_QUICK_TIMEOUT_MS = "
        "process.env.GODOT_MCP_QUICK_TIMEOUT_MS || '90000'" in _proxy_text(kol_repo)
    )


@then("the launcher default-injects GODOT_MCP_QUICK_TIMEOUT_MS of 90000")
def launcher_90s(kol_repo: Path) -> None:
    assert "GODOT_MCP_QUICK_TIMEOUT_MS:-90000" in _launcher_text(kol_repo)


# ---------- AC-MCP-004 ----------


@given("the current repository provides the godot-mcp launch entry")
def repo_launch_entry(kol_repo: Path) -> None:
    assert (_launch(kol_repo) / "godot-mcp-launcher.sh").is_file()


@when("doctor runs its registration check")
def doctor_runs(kol_repo: Path, ctx: dict) -> None:
    proc = subprocess.run(
        ["bash", str(_launch(kol_repo) / "godot-status.sh"), "doctor", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    try:
        ctx["doctor"] = json.loads(proc.stdout)
    except json.JSONDecodeError as err:
        raise AssertionError(f"doctor produced no JSON: {proc.stdout[:200]} / {err}") from err


@then("no registration FAIL may reference a launch path inside this repository")
def no_repo_registration_fail(ctx: dict) -> None:
    doctor = ctx.get("doctor") or {}
    legacy_marker = "/.dev/godot-mcp/launch/"
    bad = []
    for check in doctor.get("checks", []):
        if check.get("level") != "FAIL":
            continue
        if not str(check.get("check", "")).startswith("registration:"):
            continue
        detail = str(check.get("detail", ""))
        m = re.search(r"(?P<path>/[\w.\-~/]*)", detail)
        path = m.group("path") if m else ""
        if path and legacy_marker in path:
            bad.append(f"{check.get('check')}: {detail}")
    assert not bad, f"registration FAIL references retired legacy path: {bad}"
