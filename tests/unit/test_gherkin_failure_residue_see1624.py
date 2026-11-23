"""SEE-1624 C1-R2（Revy QA DEF-1/DEF-1b/1c/DEF-2）— 失败路径统一 JSON
契约与零私套件残留。

五条 QA 亲手复现路径都必须：stdout 恰一个 JSON、具名错误、exit 1、
项目内零 `.gqt-run-*` 目录/文件残留（失败前后私有 suite 暂存区不变）：

1. `gherkin compile --keys <未知 key>`（DEF-1；AddonError 曾逃逸 JSON 契约）
2. `gherkin compile --keys demo` + schema 缺失（DEF-1b）
3. `gherkin compile --keys demo` + schema 损坏（DEF-1c）
4. `gherkin run --gherkin-probe-keys <未知 key>`（DEF-2）
5. `gherkin run --gherkin-probe-keys demo` + validator 红（DEF-2）
"""

from __future__ import annotations

import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from gherkin_fixtures import (
    make_addon_project,
    write_feature,
)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = make_addon_project(tmp_path / "proj")
    write_feature(root, "demo")
    return root


def _run_cli(argv: list[str]) -> tuple[int, str]:
    """in-process CLI：返回 (exit code, stdout 原始文本)。

    AddonError 等未收口异常会在此直接抛出（即 JSON 契约破裂的 RED 面）。
    """
    from godot_qa_toolkit.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(argv)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    return code, buf.getvalue()


def _assert_single_json(code: int, out: str) -> dict:
    payload = json.loads(out)  # 整段 stdout 必须恰为一个 JSON 对象
    assert isinstance(payload, dict)
    assert payload.get("ok") is False
    assert payload.get("tool") == "gherkin"
    # 具名错误：summary.error 或 failures 至少一立面在场（probe 错误记录
    # 以 summary.error 承载，failures 保持既有空数组契约）。
    assert payload.get("summary", {}).get("error") or payload.get("failures")
    assert isinstance(payload.get("failures"), list)
    assert code == 1
    return payload


def _assert_zero_private_suite_residue(project: Path) -> None:
    leftovers = sorted(str(p.relative_to(project))
                       for p in project.rglob(".gqt-run-*"))
    assert leftovers == [], f"private suite residue: {leftovers}"


# ---------- 1. compile --keys 未知 key（DEF-1） ----------

def test_compile_keys_unknown_key_json_contract_zero_residue(
        project: Path) -> None:
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "nope", "--write"])
    payload = _assert_single_json(code, out)
    assert "unknown key" in payload["summary"]["error"]
    _assert_zero_private_suite_residue(project)


# ---------- 2/3. compile --keys + schema 缺失/损坏（DEF-1b/1c） ----------

def test_compile_keys_missing_schema_json_contract_zero_residue(
        project: Path) -> None:
    (project / "addons" / "gqt_gherkin" / "schema.json").unlink()
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "demo", "--write"])
    payload = _assert_single_json(code, out)
    assert "schema" in payload["summary"]["error"]
    _assert_zero_private_suite_residue(project)


def test_compile_keys_corrupt_schema_json_contract_zero_residue(
        project: Path) -> None:
    (project / "addons" / "gqt_gherkin" /
     "schema.json").write_text("{not json", encoding="utf-8")
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "demo", "--write"])
    payload = _assert_single_json(code, out)
    assert "schema" in payload["summary"]["error"]
    _assert_zero_private_suite_residue(project)


# ---------- 4. probe 未知 key（DEF-2） ----------

def test_probe_unknown_key_json_contract_zero_residue(
        project: Path) -> None:
    code, out = _run_cli(["gherkin", "run",
                          "--project-root", str(project),
                          "--gherkin-probe-keys", "ghost"])
    payload = _assert_single_json(code, out)
    assert "unknown key" in payload["summary"]["error"]
    _assert_zero_private_suite_residue(project)


# ---------- 5. probe + validator 红（DEF-2） ----------

def test_probe_validator_red_json_contract_zero_residue(
        project: Path, monkeypatch) -> None:
    import subprocess as sp

    from gherkin_fixtures import fake_validator_process

    red = fake_validator_process(ok=False, reason="V2 harness.gd missing")
    calls: list = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        assert "gut_cmdln.gd" not in joined, "GUT must not run on preflight red"
        if "validator.gd" in joined:
            return red
        raise AssertionError(f"unexpected spawn: {joined}")

    monkeypatch.setattr(sp, "run", fake_run)
    import godot_qa_toolkit.mutation.inject as inject
    monkeypatch.setattr(inject.subprocess, "run", fake_run)
    code, out = _run_cli(["gherkin", "run",
                          "--project-root", str(project),
                          "--gherkin-probe-keys", "demo"])
    payload = _assert_single_json(code, out)
    assert "preflight" in payload["summary"]["error"]
    assert "V2 harness.gd missing" in payload["summary"]["error"]
    _assert_zero_private_suite_residue(project)
    assert len(calls) == 1, "validator exactly once, no business GUT"


# ---------- 收口后的成功路径语义不回退（对照） ----------

def test_compile_keys_happy_path_still_writes_private_suite(
        project: Path) -> None:
    code, out = _run_cli(["gherkin", "compile",
                          "--project-root", str(project),
                          "--keys", "demo", "--write"])
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True and payload["summary"]["keys"] == ["demo"]
    suite_dir = Path(payload["summary"]["suite_dir"])
    assert suite_dir.is_dir()
    assert (suite_dir / "generated" / "demo.manifest.json").is_file()
    shutil.rmtree(suite_dir, ignore_errors=True)
