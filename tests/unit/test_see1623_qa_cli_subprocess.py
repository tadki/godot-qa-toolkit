"""SEE-1623 QA FAIL 修复轮 —— 真实 CLI 子进程级行为钉。

为什么必须子进程：进程内 `build_parser().parse_args` 会先加载
truth_select，绕过真实 CLI 启动期的导入顺序——`--lines` 循环导入回归
（truth_lines ↔ truth_select）因此全绿而语义已崩。本文件全部经
**子进程**调用 CLI，覆盖真实启动导入顺序。

覆盖：--dry-run --lines 预览、--lines abc → exit 2 可读提示、
全无位点 --lines 999999 具名失败、正常卡尺执行路径，以及
mutation/measurement 包逐模块「首个加载入口」独立进程导入审计
（循环依赖不得在任何导入顺序下爆）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="subproc"\n')
    (root / "src" / "calc.gd").write_text(
        "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n\n"
        "func sub(a, b):\n\treturn a - b\n")
    return root


def _cli_env(tmp_path: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env["HOME"] = str(tmp_path / "home")
    env["XDG_DATA_HOME"] = str(tmp_path / "xdg")
    env["TMPDIR"] = str(tmp_path / "tmp")
    for key in ("HOME", "XDG_DATA_HOME", "TMPDIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    return env


def _run_cli(tmp_path: Path, args: list[str]) -> subprocess.CompletedProcess:
    root = _project(tmp_path)
    return subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *args],
        cwd=str(root), env=_cli_env(tmp_path), capture_output=True,
        text=True, timeout=180)


class TestCliSubprocessLines:
    def test_dry_run_lines_preview_ok(self, tmp_path):
        """--dry-run --select full --lines 4 → exit 0 + JSON 预览
        （行过滤生效：预览计数 = 选中行位点数）。"""
        r = _run_cli(tmp_path, [
            "mutation", "src/calc.gd", "--project-root", ".",
            "--dry-run", "--select", "full", "--lines", "4"])
        assert r.returncode == 0, r.stderr
        assert "Traceback" not in r.stderr
        payload = json.loads(r.stdout)
        assert payload["ok"] is True
        s = payload["summary"]
        assert s["mutants"] == 3
        assert s["lines_selection"]["hit"] == [4]

    def test_invalid_lines_exit_two_readable(self, tmp_path):
        """--lines abc → exit 2 + 可读提示（stdout 无 JSON、零 traceback）。"""
        r = _run_cli(tmp_path, [
            "mutation", "src/calc.gd", "--project-root", ".",
            "--lines", "abc"])
        assert r.returncode == 2, r.stdout
        assert r.stdout == ""
        assert "Traceback" not in r.stderr
        assert "--lines" in r.stderr

    def test_lines_without_site_named_failure(self, tmp_path):
        """全无位点 --lines 999999 → exit 1 + 具名失败（不静默成功）。"""
        r = _run_cli(tmp_path, [
            "mutation", "src/calc.gd", "--project-root", ".",
            "--dry-run", "--select", "full", "--lines", "999999"])
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        payload = json.loads(r.stdout)
        assert payload["ok"] is False
        assert "selection matched no mutation sites" in \
            str(payload["summary"].get("error"))

    def test_caliper_execution_path_no_traceback(self, tmp_path):
        """卡尺执行路径（真实启动导入顺序）→ 结构化 JSON，零 traceback。"""
        r = _run_cli(tmp_path, [
            "mutation", "src/calc.gd", "--project-root", ".",
            "--select", "full", "--lines", "4"])
        assert "Traceback" not in r.stderr, r.stderr
        assert r.stdout.strip(), "CLI 必须给出 JSON（成功或具名失败）"
        payload = json.loads(r.stdout)
        assert "ok" in payload


class TestImportOrderSafety:
    """逐模块「首个加载入口」独立进程导入审计——循环依赖不得在任何
    导入顺序下爆（QA 审计发现 truth_lines↔truth_select 与
    worker_session↔worker_channel 两处）。"""

    def _package_modules(self, package: str) -> list[str]:
        out = subprocess.run(
            [sys.executable, "-c",
             "import pkgutil, importlib;import {0} as p;"
             "print('\\n'.join(sorted(m.name for m in "
             "pkgutil.walk_packages(p.__path__, '{0}.'))))".format(package)],
            env=_cli_env(Path("/tmp")), capture_output=True, text=True,
            timeout=120)
        assert out.returncode == 0, out.stderr
        return [m for m in out.stdout.split() if m]

    @pytest.mark.parametrize("package", ["godot_qa_toolkit.mutation",
                                         "godot_qa_toolkit.measurement"])
    def test_each_module_importable_first(self, package):
        for module in self._package_modules(package):
            r = subprocess.run(
                [sys.executable, "-c", f"import {module}"],
                env=_cli_env(Path("/tmp")), capture_output=True, text=True,
                timeout=120)
            assert r.returncode == 0, (
                f"{module} 作为首个加载入口失败：\n{r.stderr[-400:]}")
