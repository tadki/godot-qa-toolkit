"""SEE-1594 §SPEC-021：CLI-native 机器可读输出契约 + 文件报告写入失败非零。"""

from __future__ import annotations

import json
import pathlib

from godot_qa_toolkit import cli


def test_no_arg_mutation_non_project_fail_loud_single_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no project.godot upward → discovery fails loud
    rc = cli.main(["mutation"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["tool"] == "mutation"
    assert doc["ok"] is False
    assert {"tool", "ok", "summary", "failures"} <= set(doc)


def test_measure_suite_single_json_envelope(tmp_path, capsys):
    proj = tmp_path / "game"
    proj.mkdir()
    (proj / "project.godot").write_text("x", encoding="utf-8")
    out = tmp_path / "recipe.json"
    cli.main(["measure", "suite", "--project-root", str(proj),
              "--out", str(out)])
    doc = json.loads(capsys.readouterr().out)
    assert {"tool", "ok", "summary", "failures"} <= set(doc)
    assert doc["tool"] == "measure.suite"


def test_risk_report_write_failure_is_nonzero(tmp_path, monkeypatch, capsys):
    proj = tmp_path / "game"
    proj.mkdir()
    (proj / "project.godot").write_text("x", encoding="utf-8")
    (proj / "a.gd").write_text("extends Node\n", encoding="utf-8")

    import godot_qa_toolkit.risk.shadow as shadow
    fake = {"ranking": {"top": []}, "records": [],
            "unknown": {"share": 0.0},
            "confidence": {"sample_files": 0, "negative_files": 0}}
    monkeypatch.setattr(shadow, "run_shadow_from_paths", lambda **k: fake)
    monkeypatch.setattr(cli, "_apply_godot_bin_or_contract",
                        lambda *a, **k: None)

    orig = pathlib.Path.write_text

    def boom(self, *a, **k):
        if str(self).endswith("bad.json"):
            raise OSError("no space left")
        return orig(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", boom)
    rc = cli.main(["measure", "risk", "a.gd", "--project-root", str(proj),
                   "--skip-measure", "--report", str(tmp_path / "bad.json")])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["ok"] is False
    assert "report write failed" in doc["summary"]["error"]
