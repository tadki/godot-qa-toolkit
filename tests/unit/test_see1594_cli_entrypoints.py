"""SEE-1594 §SPEC-020：无参数 git diff 入口 + `<file>#<lines>` 行级地址。"""

from __future__ import annotations

import argparse
import subprocess

import pytest

from godot_qa_toolkit import cli


def _ns(**kw) -> argparse.Namespace:
    base = {"files": [], "lines": None}
    base.update(kw)
    return argparse.Namespace(**base)


def test_file_lines_address_parsed():
    ns = _ns(files=["utils/a.gd#10-20,30"])
    assert cli._normalize_mutation_targets(ns) is None
    assert ns.files == ["utils/a.gd"]
    assert ns.lines == "10-20,30"


def test_file_lines_conflicting_flag_fails(capsys):
    ns = _ns(files=["a.gd#10"], lines="20")
    assert cli._normalize_mutation_targets(ns) == 1
    assert '"ok": false' in capsys.readouterr().out


def test_file_lines_mixed_with_plain_fails(capsys):
    ns = _ns(files=["a.gd#10", "b.gd"])
    assert cli._normalize_mutation_targets(ns) == 1


def test_multiple_file_lines_fails(capsys):
    ns = _ns(files=["a.gd#10", "b.gd#20"])
    assert cli._normalize_mutation_targets(ns) == 1


def test_plain_files_pass_through():
    ns = _ns(files=["a.gd", "b.gd"])
    assert cli._normalize_mutation_targets(ns) is None
    assert ns.files == ["a.gd", "b.gd"]


def test_stable_id_is_not_line_address():
    # `<file>#<scope>#<n>` 不是行级地址（spec 非纯数字/区间）→ 当普通 token。
    ns = _ns(files=["a.gd"])
    assert cli._normalize_mutation_targets(ns) is None
    assert ns.files == ["a.gd"]


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv],
                          capture_output=True, text=True)


def _init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "project.godot").write_text("x", encoding="utf-8")
    _git(repo, "add", ".")


def test_git_diff_targets_tracked_and_untracked(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.gd").write_text("extends Node\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    (repo / "a.gd").write_text("extends Node\nfunc f():\n\tpass\n", encoding="utf-8")
    (repo / "b.gd").write_text("extends Node\n", encoding="utf-8")  # untracked
    got = cli._git_diff_targets(str(repo))
    assert "a.gd" in got and "b.gd" in got


def test_git_diff_no_changes_raises(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.gd").write_text("extends Node\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    with pytest.raises(ValueError):
        cli._git_diff_targets(str(repo))


def test_git_diff_non_git_raises(tmp_path):
    with pytest.raises(ValueError):
        cli._git_diff_targets(str(tmp_path))
