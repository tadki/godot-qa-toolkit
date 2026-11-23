"""SEE-1623 M2 — 前置一次性枚举 + 目标校验前置行为钉。

根因（SEE-1622 Bachi BUG-4/BUG-6、Harti 障碍2）：`.gutconfig.json` →
`acceptance/features/` 目录 → `.feature` 逐个试错（每轮 0 秒失败一次只
披露一个），plan 自称静态穷查却 (0 error) 从未预见；不存在的目标先报
无关 acceptance 错误（prepare 链先行于目标锚定）。

行为契约：plan 一次列全 prepare 前置缺失（附修复动作）；目标不存在时
target-not-found 先于任何 prepare/映射错误（与 core 原错误契约同文）。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.mutation.plancheck import static_plan


class TestPreparePrereqEnumeration:
    def test_all_missing_prereqs_listed_at_once(self, tmp_path):
        """三前置全缺 → plan 一次列全（gutconfig + features 目录 +
        feature 文件），不再逐个试错。"""
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m2"\n')
        plan = static_plan(str(root), None, None)
        codes = {p["code"] for p in plan["problems"]}
        assert {"gutconfig_missing", "acceptance_features_dir_missing"} \
            <= codes

    def test_features_dir_without_feature_files_named(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m2"\n')
        (root / ".gutconfig.json").write_text("{}")
        (root / "acceptance" / "features").mkdir(parents=True)
        plan = static_plan(str(root), None, None)
        codes = {p["code"] for p in plan["problems"]}
        assert "acceptance_features_dir_missing" not in codes
        assert "acceptance_features_empty" in codes

    def test_complete_prereqs_no_noise(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m2"\n')
        (root / ".gutconfig.json").write_text(
            '{"dirs": ["res://tests/"], "include_subdirs": true}')
        (root / "acceptance" / "features").mkdir(parents=True)
        (root / "acceptance" / "features" / "a.feature").write_text(
            "Feature: a\nScenario: s\nGiven x\n")
        plan = static_plan(str(root), None, None)
        codes = {p["code"] for p in plan["problems"]}
        assert not (codes & {"gutconfig_missing",
                             "acceptance_features_dir_missing",
                             "acceptance_features_empty"})

    def test_prereq_problems_carry_fix_actions(self, tmp_path):
        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m2"\n')
        plan = static_plan(str(root), None, None)
        for p in plan["problems"]:
            if p["code"] in ("gutconfig_missing",
                             "acceptance_features_dir_missing"):
                assert p["fix"]
                assert p["auto_prepare_covered"] is False


class TestTargetBeforePrepare:
    def test_missing_target_named_before_prepare_chain(self, tmp_path,
                                                       monkeypatch, capsys):
        """不存在的目标 → target-not-found 具名（先于 prepare/映射链）。"""
        import io
        import json as _json
        from contextlib import redirect_stdout

        import godot_qa_toolkit.cli as cli
        import godot_qa_toolkit.mutation.truth_select as ts

        root = tmp_path / "proj"
        root.mkdir()
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m2"\n')
        ensure_calls: list = []
        monkeypatch.setattr(ts, "ensure_map",
                            lambda *a, **k: ensure_calls.append(1))
        args = cli.build_parser().parse_args([
            "mutation", str(root / "nope.gd"), "--project-root",
            str(root), "--auto-prepare"])
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = args.func(args)
        except SystemExit:
            code = None
        assert code == 1
        payload = _json.loads(buf.getvalue())
        assert payload["ok"] is False
        assert "mutation target not found" in payload["summary"]["error"]
        assert "anchored" in payload["summary"]["error"]
        assert ensure_calls == [], "目标缺失不得先跑映射构建"
        assert "acceptance" not in payload["summary"]["error"]
