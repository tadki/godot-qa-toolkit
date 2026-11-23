"""SEE-1623 M1 — 显式面映射前置解耦行为钉。

根因（SEE-1622 Bachi BUG-5 + Harti 障碍3）：显式 `--tests`/fn/file 被
无/旧映射的 prepare_required 阻断——文档承诺「显式 --tests 用户接管
测试面」与 `--help` 的「缺映射是 blocker」互相矛盾，实测行为按后者，
显式面接管语义名存实亡。

行为契约：显式 `--tests` 在场时——P1 prepare 门不拦、不触发
ensure_map（映射构建对显式面非必需）；映射前置只约束默认 line/fn/file
面（既有契约不动）。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.cli import _p1_prepare_gate


def _plan_with_map_warning() -> dict:
    return {"problems": [
        {"severity": "warning", "code": "attribution_map_missing_or_stale",
         "level": "P1", "source": "conventional", "evidence": {},
         "fix": "build the map", "auto_prepare_covered": True}],
        "estimated_costs": [], "budget_s": 10.0, "elapsed_s": 0.01,
        "exhaustive": True}


class _Args:
    def __init__(self, **kw):
        self.files = ["/proj/src/calc.gd"]
        self.project_root = "/proj"
        self.dry_run = False
        self.auto_prepare = False
        self.select_mode = "line"
        self.tests = None
        for k, v in kw.items():
            setattr(self, k, v)

    def get(self, k, default=None):
        return getattr(self, k, default)


class TestExplicitFaceGate:
    def test_explicit_tests_bypasses_prepare_gate(self):
        assert _p1_prepare_gate(_Args(tests="res://tests/gut"),
                                _plan_with_map_warning()) is None

    def test_default_face_still_gated(self, tmp_path, monkeypatch):
        """默认面（无 --tests）映射缺失仍停（既有契约不放松）。"""
        import godot_qa_toolkit.cli as cli
        target = tmp_path / "calc.gd"
        target.write_text("extends RefCounted\n")
        monkeypatch.setattr(cli, "_emit", lambda result: result)
        args = _Args(project_root=str(tmp_path), files=[str(target)])
        rc = _p1_prepare_gate(args, _plan_with_map_warning())
        assert rc is not None
        assert rc["summary"]["error"] == "prepare_required"

    def test_full_face_still_bypasses(self):
        assert _p1_prepare_gate(_Args(select_mode="full"),
                                _plan_with_map_warning()) is None

    def test_missing_target_defers_to_downstream(self):
        """目标不存在时门不拦（交由下游 target-not-found 原错误契约）。"""
        missing = _Args(files=["/proj/src/nope.gd"])
        assert _p1_prepare_gate(missing, _plan_with_map_warning()) is None


class TestExplicitTestsSkipEnsureMap:
    def test_ensure_map_not_called_for_explicit_tests(self, tmp_path,
                                                      monkeypatch, capsys):
        """显式 --tests 真实运行不触发 ensure_map（零映射构建成本）；
        dispatch 打桩保证用例 hermetic（不触达真实 GUT 腿）。"""
        import io
        from contextlib import redirect_stdout

        import godot_qa_toolkit.cli as cli

        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        (root / "tests" / "gut").mkdir(parents=True)
        (root / "project.godot").write_text(
            'config_version=5\n[application]\nconfig/name="m1"\n')
        (root / "src" / "calc.gd").write_text(
            "extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")
        (root / "tests" / "gut" / "test_calc.gd").write_text(
            "extends GutTest\nfunc test_a() -> void:\n\tassert_true(true)\n")
        (root / "addons" / "gut").mkdir(parents=True)
        (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")

        called: list = []
        import godot_qa_toolkit.mutation.truth_select as ts
        monkeypatch.setattr(ts, "ensure_map",
                            lambda *a, **k: called.append(1))
        monkeypatch.setattr(cli, "_dispatch_mutation",
                            lambda *a, **k: {"tool": "mutation", "ok": True,
                                             "summary": {}, "failures": []})
        args = cli.build_parser().parse_args([
            "mutation", str(root / "src" / "calc.gd"),
            "--project-root", str(root), "--tests", "res://tests/gut",
            "--budget", "1"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_mutation(args)
        assert called == [], "显式 --tests 不得触发映射构建"
