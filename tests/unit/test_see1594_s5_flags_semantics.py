"""SEE-1594 S5（A13/A14/A19/A20/A22）：旧 flag/缓存退役与语义统一反例。

- A13：--resume / --reuse-coverage 物理退役；--no-reuse-coverage 真实关断；
  默认差分仍开（summary.resumed 只在真实复用时在场）。
- A14：v1/旧 /1 启动提示与 identity.disclose_superseded 退役——不再有
  一次性失效披露输出；store 语义 = 不读不写不删（静默忽略）。
- A19：多文件自动选面 baseline 共享——select_mode 穿线后两文件解析出同一
  materialized glob → 单份共享 baseline（不退串行）。
- A20：显式 --select full 不因无关映射失败阻断（不预建映射）。
- A22：attribution_usable 中央校验——event_counts_stale 增量产物不可用、
  recipe 漂移不可用。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

REPO = Path(__file__).resolve().parents[2]


class TestA13FlagRetirement:
    def test_resume_flag_not_registered(self):
        from godot_qa_toolkit.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--resume"])

    def test_reuse_coverage_flag_not_registered(self):
        from godot_qa_toolkit.cli import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(
                ["mutation", "a.gd", "--project-root", ".", "--reuse-coverage"])

    def test_no_reuse_coverage_is_real_off_switch(self):
        from godot_qa_toolkit.cli import build_parser, _reuse_coverage_flag
        args = build_parser().parse_args(
            ["mutation", "a.gd", "--project-root", "."])
        assert _reuse_coverage_flag(args) is True
        args2 = build_parser().parse_args(
            ["mutation", "a.gd", "--project-root", ".", "--no-reuse-coverage"])
        assert _reuse_coverage_flag(args2) is False

    def test_default_diff_still_on(self, tmp_path, monkeypatch):
        """默认差分仍开：二轮 killed 复用（resumed 披露）。"""
        from godot_qa_toolkit.mutation import runner as mrunner
        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        tests = proj / "tests"
        tests.mkdir()
        (tests / "test_x.gd").write_text(
            "extends GutTest\n\nfunc test_x():\n\tpass\n")
        target = proj / "calc.gd"
        target.write_text(
            "func add(a):\n\treturn a + 1\n\nfunc sub(a):\n\treturn a - 2\n")

        # A09：killed 复用唯一依据 = 新鲜运行时映射证书——种子 killer 身份
        import attribution_fixtures as _fx
        from godot_qa_toolkit.mutation import truth_select as _ts
        _fx.install_recipe(proj)
        _fx.line_facts_map(proj, {"test_x.gd::test_x": {"calc.gd": [2, 5]}},
                           instrumentable={"calc.gd": [2, 5]})
        _ts._fresh_recipe_hash.cache_clear()
        _ts._test_scripts_by_name.cache_clear()
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")

        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            if state["n"] == 1:
                return (0, "* t0\n[passed]")
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r1 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        assert r1["summary"]["killed"] == 8
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        assert r2["summary"]["resumed"] == 8, "默认差分仍开（无 flag 参与）"


class TestA14StartupDisclosureRetired:
    def test_no_v1_disclosure_output(self, tmp_path, capsys):
        """A14：v1 目录在场 → 无一次性失效披露输出（静默忽略，不读不写不删）。"""
        from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2
        proj = tmp_path / "proj"
        proj.mkdir()
        v1 = proj / ".qa-cache" / "gqt-resume-v1"
        v1.mkdir(parents=True)
        (v1 / "0.json").write_text("{}", encoding="utf-8")
        store = ResumeStoreV2(str(proj), str(proj / "calc.gd"),
                              "res://tests/", binary_kind=None,
                              identity={"ops_version": "x"})
        store.disclose_v1_superseded if hasattr(
            store, "disclose_v1_superseded") else None
        assert not hasattr(store, "disclose_v1_superseded"), (
            "A14：v1 披露方法应已退役")
        err = capsys.readouterr().err
        assert "resume v1 store found" not in err

    def test_identity_disclose_superseded_removed(self):
        from godot_qa_toolkit.mutation import identity
        assert not hasattr(identity, "disclose_superseded"), (
            "A14：identity.disclose_superseded 应已物理删除")


class TestA19SharedBaseline:
    def test_multi_file_shared_baseline_same_glob(self, tmp_path, monkeypatch):
        """A19：select_mode 穿线后，两文件解析出同一 materialized glob →
        单份共享 baseline（不再因缺省 full 全 None 而退串行）。"""
        from godot_qa_toolkit.mutation import executor

        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        (proj / "tests" / "unit").mkdir(parents=True)
        (proj / "tests" / "unit" / "test_a.gd").write_text(
            "extends GutTest\nfunc t1() -> void:\n\tpass\n")
        f1 = proj / "calc_a.gd"
        f1.write_text("func a(x):\n\treturn x\n")
        f2 = proj / "calc_b.gd"
        f2.write_text("func b(x):\n\treturn x\n")
        from godot_qa_toolkit.mutation import truth_select as _ts
        import attribution_fixtures as fx
        fx.install_recipe(proj)
        fx.line_facts_map(proj, {"test_a.gd::t1": {"calc_a.gd": [2],
                                                   "calc_b.gd": [2]}},
                          instrumentable={"calc_a.gd": [2],
                                          "calc_b.gd": [2]})
        _ts._fresh_recipe_hash.cache_clear()
        _ts._test_scripts_by_name.cache_clear()
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        monkeypatch.setattr(executor, "_run_baseline_snapshot",
                            lambda root, g, timeout_s: {
                                "rc": 0, "tail": "* t0\n[passed]",
                                "elapsed": 5.0})

        seen_select_modes = []
        real_scan = executor._auto_scan_tests_glob

        def spy_scan(file_path, project_root, select_mode="full"):
            seen_select_modes.append(select_mode)
            return real_scan(file_path, project_root,
                             select_mode=select_mode)

        monkeypatch.setattr(executor, "_auto_scan_tests_glob", spy_scan)
        baselines, failures = executor._precompute_baselines(
            [str(f1), str(f2)], str(proj), 60, None,
            select_mode="line")
        assert failures == {}, failures
        assert len(baselines) == 1, "同 glob 收敛单份共享 baseline"
        assert all(sm == "line" for sm in seen_select_modes)
        # 两文件 glob 一致（共享同一条 baseline）
        assert len(set(baselines)) == 1



class TestA20FullFaceNoPrebuild:
    def test_full_face_skips_map_bootstrap(self, tmp_path, monkeypatch):
        """A20：--select full 无映射也不阻断——ensure_map 不被调用。"""
        from godot_qa_toolkit import cli
        from godot_qa_toolkit.mutation import truth_select as _truth
        calls = {"ensure": 0}

        def boom(*a, **k):
            calls["ensure"] += 1
            raise _truth.MutationSelectionError("unrelated_map_failure")

        proj = tmp_path / "proj"
        (proj / "src_game").mkdir(parents=True)
        (proj / "src_game" / "cell.gd").write_text(
            "extends Node\nfunc f(a):\n\treturn a\n")
        (proj / "project.godot").write_text(
            'config_version=5\n\n[application]\n')
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")

        import godot_qa_toolkit.cli as cli_mod

        monkeypatch.setattr(cli, "_bootstrap_import_or_contract",
                            lambda *a, **k: None)
        monkeypatch.setattr(_truth, "ensure_map", boom)
        monkeypatch.setattr(
            cli, "_dispatch_mutation",
            lambda args, kind: {"tool": "mutation", "ok": True,
                                "summary": {}, "failures": []})

        args = cli.build_parser().parse_args([
            "mutation", str(proj / "src_game" / "cell.gd"),
            "--project-root", str(proj), "--select", "full",
        ])
        rc = cli.cmd_mutation(args)
        assert calls["ensure"] == 0, (
            "A20：显式 full 不得预建/校验映射")
        assert rc == 0


class TestA22CentralFreshness:
    def _write_map(self, root: Path, **overrides) -> Path:
        from godot_qa_toolkit.measurement.recipe import (
            build_recipe, recipe_hash)
        map_file = root / ".qa-cache" / "attribution-map.json"
        map_file.parent.mkdir(parents=True, exist_ok=True)
        recipe, error = build_recipe(root)
        raw = recipe_hash({k: v for k, v in recipe.items()
                           if k != "recipe_hash"}) if error is None else "rh"
        doc = {"schema_version": "test-attribution/6",
               "recipe_hash_pristine": raw,
               "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                           "pairs": 1, "probes_fired": 100,
                                           "vacuous": False}},
               "arms": {"gut_full": {}},
               "increment": {"event_counts_stale": False},
               "file_sha256": {}, "tests_face": {},
               "instrumentable_lines": {}}
        doc.update(overrides)
        map_file.write_text(json.dumps(doc, ensure_ascii=False),
                            encoding="utf-8")
        return map_file

    def test_event_counts_stale_not_usable(self, tmp_path):
        """A22：event_counts_stale 的增量产物 → attribution_usable=False。"""
        from godot_qa_toolkit.cli import _attribution_usable
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "project.godot").write_text('config_version=5\n')
        map_file = self._write_map(
            root, increment={"event_counts_stale": True})
        assert _attribution_usable(str(map_file), 0.0) is False

    def test_recipe_drift_not_usable(self, tmp_path):
        from godot_qa_toolkit.cli import _attribution_usable
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "project.godot").write_text('config_version=5\n')
        map_file = self._write_map(root)
        doc = json.loads(map_file.read_text(encoding="utf-8"))
        doc["recipe_hash_pristine"] = "drifted"
        map_file.write_text(json.dumps(doc), encoding="utf-8")
        assert _attribution_usable(str(map_file), 0.0) is False

    def test_fresh_and_valid_is_usable(self, tmp_path):
        from godot_qa_toolkit.cli import _attribution_usable
        root = tmp_path / "proj"
        (root / "tests").mkdir(parents=True)
        (root / "project.godot").write_text('config_version=5\n')
        import attribution_fixtures as _fx
        _fx.install_recipe(root)
        map_file = self._write_map(root)
        import time
        assert _attribution_usable(str(map_file), time.time() - 60) is True


class TestS6HarnessSampleSelection:
    """S6 harness 样本选择逻辑：找不到样本必须 fail-loud（不得当 PASS）。"""

    def test_no_samples_raises(self, tmp_path):
        import importlib.util
        empty = tmp_path / "kol"
        (empty / "utils").mkdir(parents=True)
        spec = importlib.util.spec_from_file_location(
            "mapping_perf", Path(__file__).resolve().parents[1]
            / "integration" / "run_see1594_mapping_perf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with pytest.raises(RuntimeError, match="sample selection failed"):
            mod._pick_samples(empty)

    def test_samples_sorted_by_lines_capped(self, tmp_path):
        import importlib.util
        kol = tmp_path / "kol"
        (kol / "utils").mkdir(parents=True)
        (kol / "utils" / "big.gd").write_text("\n" * 30 + "func a():\n\tpass\n")
        (kol / "utils" / "small.gd").write_text("func b():\n\tpass\n")
        (kol / "systems").mkdir(parents=True)
        (kol / "systems" / "mid.gd").write_text("\n" * 10)
        spec = importlib.util.spec_from_file_location(
            "mapping_perf2", Path(__file__).resolve().parents[1]
            / "integration" / "run_see1594_mapping_perf.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        samples = mod._pick_samples(kol)
        assert len(samples) == 2, "small.gd(<5 行) 被过滤"
        assert samples[0].name == "big.gd", "行数降序排序"
        assert samples[1].name == "mid.gd"
