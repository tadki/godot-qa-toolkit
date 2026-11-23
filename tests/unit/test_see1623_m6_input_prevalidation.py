"""SEE-1623 M6 — 输入前置校验行为钉。

根因（SEE-1622 Harti 障碍4/5）：不存在的 `--tests` glob 不在 plan/prepare
拒绝（烧完全部 run_error 腿才结束，~0.5s×N 全浪费）；显式损坏映射的
错误提示建议 `--auto-prepare`——与「显式映射只读、绝不重建」契约相反。

行为契约：显式 tests glob 不存在 → plan error 级（plan_blocked，先于
任何 GUT）；显式映射形态的 prepare_required 提示只指修复/替换映射，
零 --auto-prepare 字样。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation.plancheck import static_plan


class TestTestsGlobPrevalidation:
    def test_nonexistent_glob_is_error_level(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m6"\n')
        plan = static_plan(str(root), None, "res://tests/does-not-exist/")
        codes = {p["code"]: p for p in plan["problems"]}
        assert "tests_glob_target_missing" in codes
        assert codes["tests_glob_target_missing"]["severity"] == "error"
        assert codes["tests_glob_target_missing"]["evidence"][
            "anchored"].endswith("does-not-exist")

    def test_existing_glob_no_problem(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m6"\n')
        (root / "tests" / "unit").mkdir(parents=True)
        plan = static_plan(str(root), None, "res://tests/unit")
        assert "tests_glob_target_missing" not in {
            p["code"] for p in plan["problems"]}

    def test_no_explicit_glob_no_problem(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m6"\n')
        plan = static_plan(str(root), None, None)
        assert "tests_glob_target_missing" not in {
            p["code"] for p in plan["problems"]}

    def test_plan_gate_blocks_before_any_gut(self, tmp_path, monkeypatch,
                                             capsys):
        """plan 门对 error 级 glob 问题阻断（plan_blocked），零 GUT 腿。"""
        import io
        import json as _json
        from contextlib import redirect_stdout

        import godot_qa_toolkit.cli as cli
        from godot_qa_toolkit.mutation import runner as runner_mod

        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m6"\n')
        (root / "calc.gd").write_text(
            "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")
        spawned: list = []
        monkeypatch.setattr(runner_mod, "_run_gut_on_project",
                            lambda *a, **k: spawned.append(1))
        args = cli.build_parser().parse_args([
            "mutation", str(root / "calc.gd"), "--project-root", str(root),
            "--tests", "res://tests/does-not-exist/", "--auto-prepare"])
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = args.func(args)
        except SystemExit:
            code = None
        assert code == 1
        payload = _json.loads(buf.getvalue())
        assert payload["ok"] is False
        assert payload["summary"]["error"] == "plan_blocked"
        assert any(p["code"] == "tests_glob_target_missing"
                   for p in payload["summary"]["plan"]["problems"])
        assert spawned == [], "plan_blocked 必须先于任何 GUT 腿"


class TestExplicitMapNotice:
    def test_explicit_map_notice_never_suggests_auto_prepare(
            self, tmp_path, monkeypatch):
        import godot_qa_toolkit.cli as cli

        captured: dict = {}

        def fake_emit(result):
            captured.update(result)
            return 1

        monkeypatch.setattr(cli, "_emit", fake_emit)
        target = tmp_path / "calc.gd"
        target.write_text("extends RefCounted\n")
        args = type("A", (), {
            "files": [str(target)], "project_root": str(tmp_path),
            "dry_run": False, "auto_prepare": False,
            "select_mode": "line", "tests": None,
            "attribution_map": str(tmp_path / "broken-map.json")})()
        plan = {"problems": [
            {"severity": "warning", "code": "attribution_map_missing_or_stale",
             "level": "P1", "evidence": {}, "fix": "x",
             "auto_prepare_covered": True}]}
        cli._p1_prepare_gate(args, plan)
        notice = captured["summary"]["notice"]
        assert "--auto-prepare" not in notice
        assert "read-only" in notice

    def test_auto_map_notice_keeps_auto_prepare_suggestion(
            self, tmp_path, monkeypatch):
        import godot_qa_toolkit.cli as cli

        captured: dict = {}

        def fake_emit(result):
            captured.update(result)
            return 1

        monkeypatch.setattr(cli, "_emit", fake_emit)
        target = tmp_path / "calc.gd"
        target.write_text("extends RefCounted\n")
        args = type("A", (), {
            "files": [str(target)], "project_root": str(tmp_path),
            "dry_run": False, "auto_prepare": False,
            "select_mode": "line", "tests": None,
            "attribution_map": None})()
        plan = {"problems": [
            {"severity": "warning", "code": "attribution_map_missing_or_stale",
             "level": "P1", "evidence": {}, "fix": "x",
             "auto_prepare_covered": True}]}
        cli._p1_prepare_gate(args, plan)
        assert "--auto-prepare" in captured["summary"]["notice"]
