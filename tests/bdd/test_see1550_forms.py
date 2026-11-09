"""SEE-1550 §SPEC-002 — 真实 Godot 加载形态矩阵（pytest-bdd，真实引擎跑）。

8 形态 fixture（tests/fixtures/see1550_forms/）：运行时 load、启动期/晚期
preload、autoload、class_name、内部类、PackedScene 场景脚本、启动组合根、
orphan 负对照。7 个目标形态 mutant 必须 killed；orphan survived 且披露
疑似孤儿（unreachable 不进 verdict——诊断披露面）。

环境守卫（CI 无引擎 → 模块级 skip，非红）：
- Godot 二进制：GQT_GODOT_BIN 或 PATH（``shutil.which``）；
- GUT addon 源：``GQT_FORMS_GUT_DIR`` 或本机 KOL 检出约定路径（只读拷贝源）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.runner import run_mutation

# 真实引擎矩阵：每 mutant 一次 Godot 启动（win64 wrapper ~5-8s/轮），单场景
# 30-90s+——BDD collection 默认 60s 场景超时不够（只在无显式 marker 时注入
# 默认），模块级放宽到 30min。
pytestmark = pytest.mark.timeout(1800, method="signal", func_only=True)

scenarios("features/see1550_forms.feature")

REPO_ROOT = Path(__file__).parent.parent.parent
FIXTURE_TEMPLATE = REPO_ROOT / "tests" / "fixtures" / "see1550_forms"

_GUT_SOURCES = [
    os.environ.get("GQT_FORMS_GUT_DIR", ""),
    "/mnt/d/GodotProjects/king-of-likes/addons/gut",
    str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons" / "gut"),
]


def _godot_available() -> bool:
    if os.environ.get("GQT_GODOT_BIN", "").strip():
        return Path(os.environ["GQT_GODOT_BIN"]).is_file()
    return shutil.which("godot") is not None


def _gut_addon_source() -> Path | None:
    for cand in _GUT_SOURCES:
        p = Path(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            return p
    return None


def _pytest_skip_reason() -> str | None:
    if not _godot_available():
        return "real Godot engine unavailable (CI) — forms matrix skipped"
    if _gut_addon_source() is None:
        return ("GUT addon source not found (set GQT_FORMS_GUT_DIR) — "
                "forms matrix skipped")
    return None


_skip = _pytest_skip_reason()
if _skip:
    pytest.skip(_skip, allow_module_level=True)


@pytest.fixture(scope="session")
def forms_project(tmp_path_factory):
    """组装 fixture 项目：模板 + GUT addon 拷贝 + headless --import 引导。"""
    proj = tmp_path_factory.mktemp("see1550_forms") / "proj"
    shutil.copytree(FIXTURE_TEMPLATE, proj)
    gut_src = _gut_addon_source()
    assert gut_src is not None
    shutil.copytree(gut_src, proj / "addons" / "gut")
    godot = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    # 新建 fixture 上直接跑 GUT 会扫到 0 个测试（.godot 缓存未热，已知坑）——
    # 先 import 一次建立 uid/class 缓存。
    subprocess.run(
        [godot, "--headless", "--import", "--path", str(proj)],
        capture_output=True, text=True, timeout=300, cwd=str(proj),
        check=False,
    )
    return proj


@given("a real 8-form fixture project with the GUT addon")
def given_forms_project(forms_project):
    return forms_project


@when(parsers.parse('the default mutation command runs on "{target_res}"'),
      target_fixture="mutation_result")
def run_default_mutation(forms_project, target_res):
    """默认 gqt mutation 路径（disk-farm/1 影子注入），真实引擎每 mutant
    一个 Godot 子进程。timeout 取宽（QA 实机门由 §SPEC-010 基准另行锚定）。"""
    target_fs = forms_project / target_res.removeprefix("res://")
    return run_mutation(str(target_fs), str(forms_project), budget=30,
                        timeout_s=180)


@then("every mutant is killed with zero survivors")
def every_mutant_killed(mutation_result):
    result = mutation_result
    s = result["summary"]
    assert s["mutants"] > 0, "fixture 形态必须有可变异位点"
    assert s["survived"] == 0, f"全形态可达红线被破坏: {s}"
    assert s["run_errors"] == 0, f"beacon/影子基建失败: {s}"
    assert s["killed"] == s["mutants"]
    assert result["ok"] is True


@then("mutants survive with the suspected-orphan disclosure")
def orphan_survives_with_disclosure(mutation_result):
    result = mutation_result
    s = result["summary"]
    assert s["survived"] > 0, "orphan 负对照必须有幸存 mutant"
    assert s.get("suspected_orphans"), \
        f"orphan 负对照必须披露疑似孤儿: {s}"
    assert any(f.get("suspected_orphan") is True for f in result["failures"])
