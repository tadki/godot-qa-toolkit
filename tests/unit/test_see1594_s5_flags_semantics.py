"""SEE-1594 S5（A19/A20/A22）语义统一反例。

- A13/A14 的退役防复发钉已集中迁移至
  test_see1594_semantic_retirement.py（S7 起退役钉唯一位置）。
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

import attribution_fixtures

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

REPO = Path(__file__).resolve().parents[2]


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
        """A20（SEE-1631 §SPEC-021/022 映射）：cold 源下 --select full
        仍不预建/校验映射（原契约）；map 源（默认）下判定基线 = map，
        full 面同样走 prepare 自动续新（ensure_map 被调用一次）。"""
        from godot_qa_toolkit import cli
        from godot_qa_toolkit.mutation import map_contract as _mc
        from godot_qa_toolkit.mutation.verdict_source import (
            ENV_BASELINE_SOURCE,
        )
        calls = {"ensure": 0}

        def boom(*a, **k):
            calls["ensure"] += 1
            # C3-R13 退回1：注入 BaselineSourceError 族（refresh 内部步
            # 建图/重建/增量失败的真实类型）——不得靠换异常类型绕过
            from godot_qa_toolkit.mutation.verdict_source import (
                BaselineSourceError,
            )
            raise BaselineSourceError("unrelated_map_failure")

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
        # C3-R12：prepare 续新收口单点 _prepare_map_fresh——判据不变：
        # prepare 自动续新必须发生。
        monkeypatch.setattr(cli, "_prepare_map_fresh", boom)
        monkeypatch.setattr(
            cli, "_dispatch_mutation",
            lambda args, kind: {"tool": "mutation", "ok": True,
                                "summary": {}, "failures": []})

        args = cli.build_parser().parse_args([
            "mutation", str(proj / "src_game" / "cell.gd"),
            "--project-root", str(proj), "--select", "full",
        ])
        # cold 源（legacy）：A20 原契约——full 不预建不校验
        monkeypatch.setenv(ENV_BASELINE_SOURCE, "cold")
        rc = cli.cmd_mutation(args)
        assert calls["ensure"] == 0, (
            "cold 源：显式 full 不得预建/校验映射")
        assert rc == 0
        # map 源（§SPEC-020/021 默认）：full 面同样走 prepare 自动续新
        monkeypatch.setenv(ENV_BASELINE_SOURCE, "map")
        from godot_qa_toolkit.mutation.truth_select import map_path as _mp

        _mp_file = _mp(Path(proj))
        if _mp_file and Path(_mp_file).is_file():
            Path(_mp_file).unlink()
        calls["ensure"] = 0
        rc = cli.cmd_mutation(args)
        assert calls["ensure"] == 1, (
            "map 源：full 面判定基线 = map，prepare 自动续新必须发生")




class TestA22CentralFreshness:
    def _write_map(self, root: Path, **overrides) -> Path:
        from godot_qa_toolkit.measurement.recipe import (
            build_recipe, recipe_hash)
        map_file = root / ".qa-cache" / "attribution-map.json"
        map_file.parent.mkdir(parents=True, exist_ok=True)
        recipe, error = build_recipe(root)
        raw = (recipe_hash({k: v for k, v in recipe.items()
                            if k != "recipe_hash"})
               if error is None else "rh")
        pristine = (attribution_fixtures.pristine_recipe_hash(root)
                    if error is None else "rh")
        doc = {"schema_version": "test-attribution/6",
               "recipe_hash_pristine": pristine,
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
