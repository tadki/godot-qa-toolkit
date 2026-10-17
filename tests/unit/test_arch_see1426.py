"""SEE-1426 P1 — `gqt arch` Pure Core 架构守卫单测（§SPEC-1426-01/02/03）。

正反例 fixture 矩阵：继承 Node、引用 get_tree、类型注解 Node、裸 randi 各
必打回；合法 Pure Core（显式 RNG 注入 + 纯逻辑）全过；统一 JSON 契约 +
exit 0/1。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from godot_qa_toolkit.arch.collector import collect_file, collect_paths
from godot_qa_toolkit.arch.gate import run_arch_gate

REPO = Path(__file__).resolve().parents[2]


GOOD_CORE = "extends RefCounted\nvar hp: int = 10\nfunc damage(n: int) -> int:\n\treturn hp - n\n"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------- 正例 ----------

def test_pure_core_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/hero.gd", GOOD_CORE)
    violations, error = collect_file(p)
    assert error is None
    assert violations == []


def test_explicit_rng_passes(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/dice.gd",
               "extends RefCounted\nvar rng := RandomNumberGenerator.new()\n"
               "func roll() -> int:\n\treturn rng.randi_range(1, 6)\n")
    violations, error = collect_file(p)
    assert error is None
    assert violations == []


# ---------- 反例矩阵（各必打回） ----------

def test_extends_node_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd", "extends Node\n")
    violations, _ = collect_file(p)
    rules = {v.rule for v in violations}
    assert "core/extends-whitelist" in rules


def test_get_tree_reference_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd",
               "extends RefCounted\nfunc f() -> void:\n\tvar t = get_tree()\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-engine-api" and "get_tree" in v.message
               for v in violations)


def test_engine_type_annotation_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd",
               "extends RefCounted\nfunc f() -> Node:\n\treturn null\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-engine-api" for v in violations)


def test_bare_global_random_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd",
               "extends RefCounted\nfunc roll() -> int:\n\treturn randi() % 6\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-bare-global-random" for v in violations)


def test_input_singleton_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd",
               "extends RefCounted\nfunc pressed() -> bool:\n\treturn Input.is_pressed(\"x\")\n")
    violations, _ = collect_file(p)
    assert any(v.rule == "no-engine-api" and "Input" in v.message for v in violations)


def test_unparseable_is_failure(tmp_path: Path) -> None:
    p = _write(tmp_path, "core/bad.gd", "extends ???not gdscript(((\n")
    violations, error = collect_file(p)
    assert error is not None
    result = run_arch_gate([str(p)])
    assert result["ok"] is False
    assert result["summary"]["unparseable"] == 1


# ---------- gate / 契约 ----------

def test_gate_contract_shape(tmp_path: Path) -> None:
    good = _write(tmp_path, "a/good.gd", GOOD_CORE)
    bad = _write(tmp_path, "b/bad.gd", "extends Node\nfunc f() -> void:\n\trandomize()\n")
    result = run_arch_gate([str(good), str(bad)])
    assert result["tool"] == "arch"
    assert result["ok"] is False
    assert result["summary"]["violations"] >= 2
    assert set(result["summary"]["by_rule"]) >= {"core/extends-whitelist", "no-bare-global-random"}
    for f in result["failures"]:
        assert {"file", "line", "rule", "message"} <= set(f) or "reason" in f


def test_gate_all_green_contract(tmp_path: Path) -> None:
    p = _write(tmp_path, "good.gd", GOOD_CORE)
    result = run_arch_gate([str(p)])
    assert result["ok"] is True
    assert result["failures"] == []


def test_collect_paths_directory(tmp_path: Path) -> None:
    _write(tmp_path, "sub/ok.gd", GOOD_CORE)
    _write(tmp_path, "sub/bad.gd", "extends SceneTree\n")
    violations, unparseable = collect_paths([tmp_path])
    assert unparseable == []
    assert any(v.rule == "core/extends-whitelist" for v in violations)


# ---------- CLI 判据（§SPEC-1426-01 形态） ----------

def _run_cli(*argv: str) -> tuple[int, dict]:
    env = {"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    r = subprocess.run(
        [sys.executable, "-m", "godot_qa_toolkit.cli", *argv],
        capture_output=True, text=True, env=env, cwd=REPO,
    )
    return r.returncode, json.loads(r.stdout)


def test_cli_arch_exit_codes(tmp_path: Path) -> None:
    good = _write(tmp_path, "good.gd", GOOD_CORE)
    bad = _write(tmp_path, "bad.gd", "extends Node\n")
    rc, out = _run_cli("arch", str(good))
    assert rc == 0 and out["ok"] is True and out["tool"] == "arch"
    rc, out = _run_cli("arch", str(bad))
    assert rc == 1 and out["ok"] is False
    assert out["failures"][0]["rule"] == "core/extends-whitelist"


@pytest.mark.parametrize("snippet,rule", [
    ("extends Node\n", "core/extends-whitelist"),
    ("extends RefCounted\nfunc f() -> void:\n\tget_tree()\n", "no-engine-api"),
    ("extends RefCounted\nfunc f() -> int:\n\treturn randf()\n", "no-bare-global-random"),
])
def test_fixture_matrix(tmp_path: Path, snippet: str, rule: str) -> None:
    p = _write(tmp_path, "x.gd", snippet)
    rc, out = _run_cli("arch", str(p))
    assert rc == 1
    assert any(f.get("rule") == rule for f in out["failures"])
