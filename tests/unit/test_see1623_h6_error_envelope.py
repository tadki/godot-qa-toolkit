"""SEE-1623 H6 — 畸形输入错误信封统一行为钉。

根因（SEE-1622 Refacty BUG-5，3/3 复现）：非 `.gd` 文件、畸形 `.gd`、坏
performance JSON 三路径均为原始 traceback + 空 stdout + exit 1——
gdtoolkit/lark 解析异常与 json 解码异常未进错误信封。契约（§5）：exit 1
= 判定失败且 stdout 有 JSON；错误路径单一 JSON 信封
（error/reason/reason_detail/suggested_action），禁止 traceback 泄漏。

行为契约：三条最小复现的 stdout 均为结构化 `ok=false` JSON（具名
reason + suggested_action），stdout+stderr 零 `Traceback`。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from godot_qa_toolkit.cli import build_parser


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="h6-fixture"\n')
    return root


def _run(args) -> tuple[int, dict, str]:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = args.func(args)
    except SystemExit as e:  # argparse 防御（本组用例不触发）
        code = int(e.code or 0)
    try:
        payload = json.loads(buf.getvalue())
    except json.JSONDecodeError:
        payload = {}
    return code, payload, buf.getvalue()


def test_coverage_non_gd_target_structured_envelope(tmp_path, capsys):
    root = _project(tmp_path)
    args = build_parser().parse_args([
        "measure", "coverage", str(root / "project.godot"),
        "--project-root", str(root), "--min-percent", "0"])
    code, payload, out = _run(args)
    err = capsys.readouterr().err
    assert "Traceback" not in out and "Traceback" not in err
    assert code == 1
    assert payload["tool"] == "measure.coverage"
    assert payload["ok"] is False
    s = payload["summary"]
    assert s["reason"] == "target_not_parseable_gdscript"
    assert s["reason_detail"]
    assert s["suggested_action"]
    assert payload["failures"]


def test_coverage_malformed_gd_structured_envelope(tmp_path, capsys):
    root = _project(tmp_path)
    (root / "broken.gd").write_text(
        "extends RefCounted\n\nfunc broken( {\n\treturn 1\n")
    args = build_parser().parse_args([
        "measure", "coverage", str(root / "broken.gd"),
        "--project-root", str(root), "--min-percent", "0"])
    code, payload, out = _run(args)
    err = capsys.readouterr().err
    assert "Traceback" not in out and "Traceback" not in err
    assert code == 1
    assert payload["ok"] is False
    s = payload["summary"]
    assert s["reason"] == "target_not_parseable_gdscript"
    assert s["suggested_action"]
    assert payload["failures"]


def test_risk_bad_performance_json_structured_envelope(tmp_path, capsys):
    root = _project(tmp_path)
    (root / "calc.gd").write_text(
        "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")
    (root / "bad.json").write_text("not json")
    args = build_parser().parse_args([
        "measure", "risk", "calc.gd", "--project-root", str(root),
        "--skip-measure", "--performance-file", "bad.json"])
    code, payload, out = _run(args)
    err = capsys.readouterr().err
    assert "Traceback" not in out and "Traceback" not in err
    assert code == 1
    assert payload["tool"] == "measure.risk"
    assert payload["ok"] is False
    s = payload["summary"]
    assert s["reason"] == "performance_file_unparseable"
    assert "bad.json" in s["reason_detail"] or "bad.json" in s["error"]
    assert s["suggested_action"]
    assert payload["failures"]
