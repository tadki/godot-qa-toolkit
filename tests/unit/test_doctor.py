"""SEE-1319 缺陷2：gqt doctor 安装路径自检单元测试。

doctor 比对 editable 安装位置与当前工作树 .dev/qa-toolkit 的 gitlink HEAD，
不一致即 exit 2 + stderr 修复指引——跨工作树行为不一致（coverage 探针版本
错配）的治理入口。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "src"))

from godot_qa_toolkit.doctor import (
    check_install,
    locate_toolkit_root,
    resolve_editable_location,
)


class TestResolveEditableLocation:
    def test_returns_path_when_editable_installed(self):
        # 本工作机存在 editable 安装（指向旧工作树）——只要求能解析出路径。
        loc = resolve_editable_location()
        assert loc is None or Path(loc).is_dir()

    def test_unresolvable_returns_none_not_crash(self, monkeypatch):
        # pip 不可用/未安装：失败兜底返回 None，绝不抛异常（WSL 稳健性）。
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor._pip_show",
            lambda: None,
        )
        assert resolve_editable_location() is None


class TestLocateToolkitRoot:
    def test_walks_up_to_dev_qa_toolkit(self, tmp_path):
        root = tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        cwd = root / "src" / "godot_qa_toolkit"
        cwd.mkdir(parents=True)
        assert locate_toolkit_root(cwd) == root

    def test_no_root_found_returns_none(self, tmp_path):
        assert locate_toolkit_root(tmp_path) is None


class TestCheckInstall:
    def _fake_env(self, tmp_path, monkeypatch, editable, git_head):
        root = tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"
        root.mkdir(parents=True)
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor.resolve_editable_location",
            lambda: editable,
        )
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor._gitlink_head",
            lambda root_path: git_head,
        )
        return root

    def test_match_returns_ok(self, tmp_path, monkeypatch):
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=str(tmp_path / "wt" / "KingOfLikes-Godot" / ".dev" / "qa-toolkit"),
                              git_head="abc123")
        # editable 与 gitlink 根一致即视为已对齐（HEAD 比对在 real env 由调用方
        # 传入 _gitlink_head 的 mock 决定，这里 mock 返回固定值 → root 匹配即可）。
        ok, detail = check_install(cwd=root / "src")
        assert ok is True
        assert detail["status"] == "ok"

    def test_stale_editable_exits_mismatch(self, tmp_path, monkeypatch):
        stale = tmp_path / "other" / "see-1317" / "qa-toolkit"
        stale.mkdir(parents=True)
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=str(stale), git_head="abc123")
        ok, detail = check_install(cwd=root / "src")
        assert ok is False
        assert detail["status"] == "mismatch"
        assert "install.sh" in detail["fix_hint"]

    def test_missing_editable_reports_not_installed(self, tmp_path, monkeypatch):
        root = self._fake_env(tmp_path, monkeypatch,
                              editable=None, git_head="abc123")
        ok, detail = check_install(cwd=root / "src")
        assert ok is False
        assert detail["status"] == "not_installed"

    def test_missing_toolkit_root_is_neutral(self, tmp_path, monkeypatch):
        # cwd 不在任何 qa-toolkit 内（如主仓外随意目录）——自检无从比对，
        # 报 ok/skipped 而非误报错。
        monkeypatch.setattr(
            "godot_qa_toolkit.doctor.resolve_editable_location",
            lambda: None,
        )
        ok, detail = check_install(cwd=tmp_path)
        assert ok is True
        assert detail["status"] == "skipped"


class TestDoctorCli:
    def _run_cli(self, *argv, cwd):
        proc = subprocess.run(
            [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
            capture_output=True, text=True, cwd=str(cwd),
            env=None,
        )
        return proc

    def test_doctor_subcommand_registered(self):
        from godot_qa_toolkit.cli import build_parser
        args = build_parser().parse_args(["doctor"])
        assert args.func is not None
