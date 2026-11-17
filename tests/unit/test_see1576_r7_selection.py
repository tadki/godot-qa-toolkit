"""SEE-1576 D3（§SPEC-613）→ SEE-1594 S4：静态强度选测退役后保留的
runtime 映射选面钉（mapping line / full-face fail-loud / summary 披露）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
import godot_qa_toolkit.mutation.runner as runner_mod
from godot_qa_toolkit.mutation.executor import run_mutation_files

WEAK_DIR_FILES = 12


def _dilution_project(tmp_path: Path) -> Path:
    """autoload 目标 + 专用套件（1 文件强引用） vs 弱目录（12 文件闭包冒充）。"""
    root = tmp_path / "proj"
    (root / "autoload").mkdir(parents=True)
    (root / "svc").mkdir()
    (root / "tests" / "unit" / "svc").mkdir(parents=True)
    (root / "tests" / "regression").mkdir(parents=True)
    (root / "project.godot").write_text(
        "[autoload]\n"
        'SvcClock="*res://svc/svc_clock.gd"\n'
        'HubState="*res://autoload/hub_state.gd"\n',
        encoding="utf-8")
    (root / "svc" / "svc_clock.gd").write_text(
        "extends Node\nfunc tick(d):\n\tpass\n", encoding="utf-8")
    (root / "svc" / "aux.gd").write_text(
        "extends Node\nfunc helper():\n\tpass\n", encoding="utf-8")
    (root / "svc" / "orphan.gd").write_text(
        "extends Node\n", encoding="utf-8")
    # 生产中间节点：引用 SvcClock（对它是 depth1）与 aux（对它是 depth1）
    (root / "autoload" / "hub_state.gd").write_text(
        "extends Node\nvar t = SvcClock\n"
        'const AUX = preload("res://svc/aux.gd")\n',
        encoding="utf-8")
    # 专用套件：直接 preload + 多次调用（高 occ）
    body = 'const SVC = preload("res://svc/svc_clock.gd")\n'
    body += "".join(f"func test_{i}() -> void:\n\tSvcClock.tick({i})\n"
                    for i in range(8))
    (root / "tests" / "unit" / "svc" / "test_svc_clock.gd").write_text(
        body, encoding="utf-8")
    # 弱目录：只引用中间 autoload HubState（对目标而言是 depth2 闭包）
    for i in range(WEAK_DIR_FILES):
        (root / "tests" / "regression" / f"test_r{i:02d}.gd").write_text(
            f"func test_{i}() -> void:\n\tHubState.x()\n", encoding="utf-8")
    return root


def _plain_project(tmp_path: Path) -> Path:
    """直连引用面（无 autoload）：2 个测试文件同目录 preload 目标。"""
    root = tmp_path / "proj"
    (root / "tests" / "unit" / "alpha").mkdir(parents=True)
    (root / "calc.gd").write_text(
        "func add(a, b):\n\treturn a + b\n", encoding="utf-8")
    for i in range(2):
        (root / "tests" / "unit" / "alpha" / f"test_c{i}.gd").write_text(
            'preload("res://calc.gd")\nfunc test_x() -> void:\n\tpass\n',
            encoding="utf-8")
    return root


# ------------------------------------------------- 判据公式（直连强度）

def test_runner_mapping_selection_line(tmp_path, capsys):
    """SEE-1581：新鲜映射 → 选择目录 + stderr 一行运行面披露。"""
    from attribution_fixtures import write_map
    root = _dilution_project(tmp_path)
    write_map(root, {"svc/aux.gd": ["test_svc_clock.gd::test_0"]})
    glob, meta = runner_mod._auto_scan_tests_glob_meta(
        str(root / "svc" / "aux.gd"), str(root), select_mode="file")
    assert glob is not None
    assert glob.startswith("res://.gqt-attr-select/")
    assert meta["source"] == "runtime_mapping"
    assert "runtime-mapping selection" in capsys.readouterr().err


def test_runner_full_face_without_map(tmp_path, capsys):
    """显式 full 审计面：tests_glob None + 零 stderr 噪声，来源进 meta。

    （映射不可用已改 fail-loud——全脸只在 --select full 显式审计时可达。）"""
    root = _plain_project(tmp_path)
    glob, meta = runner_mod._auto_scan_tests_glob_meta(
        str(root / "calc.gd"), str(root), select_mode="full")
    assert glob is None
    assert meta["source"] == "full_face"
    assert meta["reason"] == "explicit_full_face"
    assert capsys.readouterr().err == ""


class _SerialPool:
    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_summary_carries_test_selection(tmp_path, monkeypatch):
    """自动选测 run 的 summary 携带 test_selection 披露键（只增不改）。"""
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    (root / "tests" / "unit" / "save").mkdir(parents=True)
    (root / "tests" / "unit" / "save" / "test_save.gd").write_text(
        'preload("res://calc.gd")\n')
    target = root / "calc.gd"
    target.write_text("func add(a, b):\n\tif a > b:\n\t\treturn a - b\n"
                      "\treturn a + b\n", encoding="utf-8")
    from attribution_fixtures import write_map
    write_map(root, {"calc.gd": ["test_save.gd::test_ok"]})

    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root_, timeout_s=60, tests_glob=None:
            (1, "* test_killed_by_probe\n\t[Failed]\n"))

    result = run_mutation_files([str(target)], str(root), budget=10,
                                timeout_s=60, jobs=4,
                                select_mode="file")  # 不传 --tests：走自动选测
    s = result["results"][0]["summary"]
    sel = s.get("test_selection")
    assert sel is not None, "summary 缺 test_selection 披露键"
    assert sel["source"] == "runtime_mapping"
    assert sel["tests"] == ["res://tests/unit/save/test_save.gd"]


# ------------------------------- 函数名 token 判别力（D3-hardener 补钉）
