"""SEE-1405 LOW 修复循环单测。

LOW-1：`GQT_GODOT_BIN` 显式指向不存在路径 / 非 ELF 不可执行文件两类边界
失败，统一收敛到既有 JSON 错误契约形态（ok:false + 明确 error 信息），
不再裸抛 PermissionError/OSError traceback。
LOW-2：`testscan._target_identities` 死代码删除——全仓零引用（红→绿：
删除后 import 层面零引用残留由本文件的引用断言守卫）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from godot_qa_toolkit.mutation import inject
from godot_qa_toolkit.mutation import testscan


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


@pytest.fixture()
def toy_project(tmp_path):
    (tmp_path / "addons" / "gut").mkdir(parents=True)
    (tmp_path / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    target = tmp_path / "calc.gd"
    target.write_text("func add(a, b):\n\treturn a + b\n", encoding="utf-8")
    return tmp_path, target


class TestLow1ErrorContract:
    def _run_cli(self, monkeypatch, toy_project, env_value):
        """经 cli main 全链跑 mutation（dispatch 处触发 detect）。
        返回 (stdout_json_or_None, stderr_text, exc_or_None)。"""
        root, target = toy_project
        monkeypatch.setenv("GQT_GODOT_BIN", env_value)
        from godot_qa_toolkit import cli
        import io
        from contextlib import redirect_stderr
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                rc = cli.main(["mutation", str(target),
                               "--project-root", str(root)])
            return rc, err.getvalue(), None
        except SystemExit as e:
            return e.code, err.getvalue(), None
        except Exception as e:  # noqa: BLE001 — 契约测试显式断言形态
            return None, err.getvalue(), e

    def test_non_elf_unreadable_file_yields_contract_not_traceback(
            self, monkeypatch, toy_project):
        """对抗主断言（红→绿）：非 ELF 不可执行文件 → ok:false JSON 契约，
        不再裸抛 PermissionError。"""
        rc, err, exc = self._run_cli(monkeypatch, toy_project, "/etc/hostname")
        assert exc is None, f"traceback leaked: {exc!r}"
        assert "PermissionError" not in err
        assert "Traceback" not in err

    def test_nonexistent_explicit_path_yields_contract(
            self, monkeypatch, toy_project):
        rc, err, exc = self._run_cli(monkeypatch, toy_project,
                                     "/nonexistent/godot-path")
        assert exc is None, f"traceback leaked: {exc!r}"

    def test_error_carries_binary_context(self, monkeypatch, toy_project,
                                          capsys):
        """契约 error 信息含操作与输入上下文（哪个探测、哪个路径）。"""
        from godot_qa_toolkit.cli import main
        root, target = toy_project
        monkeypatch.setenv("GQT_GODOT_BIN", "/etc/hostname")
        rc = main(["mutation", str(target), "--project-root", str(root)])
        assert rc == 1  # ok:false → exit 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "GQT_GODOT_BIN" in payload["summary"]["error"]
        assert "/etc/hostname" in payload["summary"]["error"]

    def test_detect_raises_typed_error_directly(self, monkeypatch):
        """库层契约：detect_binary_kind(refresh=True) 对坏路径抛
        BinaryConfigError（带路径上下文），不是 PermissionError/OSError。"""
        monkeypatch.setenv("GQT_GODOT_BIN", "/etc/hostname")
        with pytest.raises(inject.BinaryConfigError) as ei:
            inject.detect_binary_kind(refresh=True)
        assert "/etc/hostname" in str(ei.value)


class TestLow2DeadCode:
    def test_target_identities_removed(self):
        assert not hasattr(testscan, "_target_identities")

    def test_no_reference_residue_in_source(self):
        """src 树零引用（docs/hardener evidence 的历史披露记录不算引用）。"""
        src_root = Path(__file__).resolve().parents[2] / "src"
        for py in src_root.rglob("*.py"):
            assert "_target_identities" not in py.read_text(
                encoding="utf-8"), py

    def test_testscan_public_api_intact(self, tmp_path):
        """删除死代码后公开 API 语义零漂移（反查/derive 仍工作）。"""
        tests = tmp_path / "tests" / "unit" / "save"
        tests.mkdir(parents=True)
        (tests / "t.gd").write_text(
            'const S = preload("res://scripts/save_manager.gd")\n')
        assert testscan.derive_tests_glob(
            "res://scripts/save_manager.gd", tmp_path / "tests") == \
            "res://tests/unit/save/t.gd"
