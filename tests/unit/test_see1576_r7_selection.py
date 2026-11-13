"""SEE-1576 D3（§SPEC-613 R-7）：选测判据 = 直连引用强度。

根因回归：autoload 身份目标的 gdself 传递闭包（depth≥2）消费者按
`len(sources)` 冒充目标证据，弱耦合目录以文件计数压过专用套件。
新判据：仅 depth==1（直接命中）consumer 计权，权重 = 文件内目标身份
token 命中次数；闭包成员保留候选但零权重；纯闭包（全零）回退目录
文件计数并弱选测双披露。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import executor
from godot_qa_toolkit.mutation.testscan import (
    derive_tests_glob,
    derive_tests_glob_meta,
)
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

def test_direct_strength_beats_closure_file_count(tmp_path):
    """§SPEC-613 oracle 形态：专用套件（1 文件强引用）必须胜过 12 文件弱闭包。"""
    root = _dilution_project(tmp_path)
    glob = derive_tests_glob("res://svc/svc_clock.gd", root / "tests",
                             project_root=str(root))
    assert glob is not None
    assert glob.startswith("res://tests/unit/svc/"), (
        f"弱闭包目录仍胜出：{glob}")


def test_weak_selection_falls_back_with_flag(tmp_path):
    """纯闭包（无任何直接引用）→ 回退目录文件计数 + weak 标记（不短路）。"""
    root = _dilution_project(tmp_path)
    glob, meta = derive_tests_glob_meta("res://svc/aux.gd", root / "tests",
                                        project_root=str(root))
    assert glob is not None, "弱选测不得短路为 None（E2 语义不得误触）"
    assert meta["weak"] is True
    assert meta["strength_score"] == 0
    assert meta["direct_files"] == 0
    assert meta["fallback"] == "dir_file_count"
    assert glob.startswith("res://tests/regression/")


def test_zero_ref_still_none(tmp_path):
    """零消费者 → None（E2 零引用短路判据不变）。"""
    root = _dilution_project(tmp_path)
    assert derive_tests_glob("res://svc/orphan.gd", root / "tests",
                             project_root=str(root)) is None


def test_direct_selection_regression_unchanged(tmp_path):
    """既有直连面（非 autoload）选测结果不变：同目录 2 文件 → 目录级 glob。"""
    root = _plain_project(tmp_path)
    glob = derive_tests_glob("res://calc.gd", root / "tests",
                             project_root=str(root))
    assert glob == "res://tests/unit/alpha/"


def test_meta_shape_direct(tmp_path):
    """直连面 meta 形态：weak=false、score>0、derived_glob 与 glob 一致。"""
    root = _plain_project(tmp_path)
    glob, meta = derive_tests_glob_meta("res://calc.gd", root / "tests",
                                        project_root=str(root))
    assert meta["derived_glob"] == glob
    assert meta["weak"] is False
    assert meta["strength_score"] > 0
    assert meta["direct_files"] >= 1
    assert "fallback" not in meta


# ------------------------------------------------- 披露接线（runner/报告面）

def test_runner_weak_disclosure_line(tmp_path, capsys):
    """弱选测：runner 扫描入口向 stderr 输出披露行（建议 --tests 覆写）。"""
    root = _dilution_project(tmp_path)
    target = root / "svc" / "aux.gd"
    glob, meta = runner_mod._auto_scan_tests_glob_meta(str(target), str(root))
    assert glob is not None and meta["weak"] is True
    err = capsys.readouterr().err
    assert "weak test selection" in err
    assert "--tests" in err


def test_runner_direct_no_weak_line(tmp_path, capsys):
    """直连面：不得输出弱选测噪声行。"""
    root = _plain_project(tmp_path)
    glob, meta = runner_mod._auto_scan_tests_glob_meta(
        str(root / "calc.gd"), str(root))
    assert meta["weak"] is False
    assert "weak test selection" not in capsys.readouterr().err


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

    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    monkeypatch.setattr(
        runner_mod, "_run_gut_on_project",
        lambda root_, timeout_s=60, tests_glob=None:
            (1, "* test_killed_by_probe\n\t[Failed]\n"))

    result = run_mutation_files([str(target)], str(root), budget=10,
                                timeout_s=60, jobs=4)  # 不传 --tests：走自动选测
    s = result["results"][0]["summary"]
    sel = s.get("test_selection")
    assert sel is not None, "summary 缺 test_selection 披露键"
    assert sel["weak"] is False
    assert sel["derived_glob"] == "res://tests/unit/save/test_save.gd"
