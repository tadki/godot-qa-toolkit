"""§SPEC-012（SEE-1583 Final Review）— 串行/卡尺选测派发与旧 schema map 恢复判据。

HIGH-1 回归（派发级）：serial（--jobs 1）/ caliper（--mutation/--lines）路径
必须把 CLI `--select`（默认 fn）贯通到 runner 自动选面——不可用映射 fail-loud，
不得静默退化为 full_face 或伪报 `explicit_full_face`。双守护形态：
- 接线 spy：`_auto_scan_tests_glob_meta` 收到的 select_mode 被硬编码回
  `full` 时立即红；
- 行为反例：fn/file 面无映射 → MutationSelectionError / selection_error
  契约，绝不 exit 0 全脸。

HIGH-2 回归：自动管理的约定默认 attribution map 遇旧 schema / 无效状态必须
原子重建（`<map>.lock` 串行化、唯一 tmp + os.replace）；显式
`--attribution-map` 只读透传——绝不重建/删除，schema 不符仍 fail-loud。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit import cli
from godot_qa_toolkit.mutation import runner, truth_select

from attribution_fixtures import install_recipe

GD_TARGET = "func add(a, b):\n\treturn a + b\n"
OLD_SCHEMA = "canonical-test-attribution/4"
NEW_SCHEMA = "test-attribution/7"


def _project(tmp_path: Path) -> tuple[Path, Path]:
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    target = proj / "calc.gd"
    target.write_text(GD_TARGET, encoding="utf-8")
    return proj, target


def _serial_args(proj: Path, target: Path, *, select_mode: str = "fn",
                 caliper: str | None = None,
                 dry_run: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        files=[str(target)], project_root=str(proj), budget=50, timeout=60,
        dry_run=dry_run, tests=None, jobs=1, resume=False,
        mutation_warning=50, timeout_factor=2.0, mutate_all=False,
        mutation=caliper, lines=None, worker_channel=False,
        select_mode=select_mode,
    )


class TestHigh1SerialCaliperSelectDispatch:
    """串行/卡尺路径遵守 select_mode：不可用映射 fail-loud，无静默全脸。"""

    def _assert_fails_loud(self, tmp_path, select_mode, caliper=None):
        proj, target = _project(tmp_path)
        args = _serial_args(proj, target, select_mode=select_mode,
                            caliper=caliper)
        with pytest.raises(truth_select.MutationSelectionError):
            cli._dispatch_mutation(args, "linux")

    def test_serial_default_fn_fails_loud_without_map(self, tmp_path):
        self._assert_fails_loud(tmp_path, "fn")

    def test_serial_explicit_file_fails_loud_without_map(self, tmp_path):
        self._assert_fails_loud(tmp_path, "file")

    def test_caliper_default_fn_fails_loud_without_map(self, tmp_path):
        self._assert_fails_loud(tmp_path, "fn", caliper="calc.gd#add#1")

    def test_serial_explicit_full_face_still_audits(self, tmp_path):
        # 反向钉：--select full 显式审计面不得被修复误伤（无映射也放行）。
        proj, target = _project(tmp_path)
        args = _serial_args(proj, target, select_mode="full")
        result = cli._dispatch_mutation(args, "linux")
        assert result["ok"] is True
        assert result["summary"]["test_selection"]["reason"] == \
            "explicit_full_face"

    @pytest.mark.parametrize("select_mode", ["fn", "file", "full"])
    def test_serial_dispatch_passes_select_mode_to_selection(
            self, tmp_path, monkeypatch, select_mode):
        # 接线 spy：select_mode 被硬编码回 "full" 时本测试立即红。
        proj, target = _project(tmp_path)
        observed = []
        real = runner._auto_scan_tests_glob_meta

        def spy(file_path, project_root, **kwargs):
            observed.append(kwargs.get("select_mode"))
            return real(file_path, project_root, **kwargs)

        monkeypatch.setattr(runner, "_auto_scan_tests_glob_meta", spy)
        args = _serial_args(proj, target, select_mode=select_mode)
        try:
            cli._dispatch_mutation(args, "linux")
        except truth_select.MutationSelectionError:
            pass                     # fn/file 无映射 fail-loud 属预期
        assert observed == [select_mode]

    def test_cli_exit_contract_serial_fail_loud(self, tmp_path, monkeypatch,
                                                capsys):
        proj, target = _project(tmp_path)
        rc = cli.main(["mutation", str(target),
                       "--project-root", str(proj),
                       "--jobs", "1", "--dry-run"])
        out = capsys.readouterr().out
        payload = json.loads(out[out.index("{"):])
        # §SPEC-PLANFIRST：dry-run 无可用映射 → plan-only 成功（列出静态
        # 问题），不再是 fail-loud；有 map 的 case 仍走原选面解析
        assert rc == 0
        assert payload["summary"].get("selection_mode") == "plan_only"
        assert payload["summary"].get("plan", {}).get("problems")


class TestHigh2DefaultMapRecovery:
    """约定默认 map 旧 schema/无效状态 → 自动重建；显式 map 只读透传。"""

    def _write_map(self, path: Path, schema: str, *, with_arms: bool = True):
        doc = {"schema_version": schema,
               "arms": {"gut_full": {}} if with_arms else {},
               "arms_state": {"gut_full": {"state": "valid",
                                           "invalid_reason": None,
                                           "bundles": 1, "pairs": 1,
                                           "probes_fired": 64,
                                           "vacuous": False}},
               "target_files": ["calc.gd"], "file_sha256": {},
               "report_recipe_hash": "x"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")
        return json.dumps(doc)

    def _auto_env(self, monkeypatch):
        # C3-R20 迁移（定案 20）：GQT_ATTRIBUTION_MAP 通道退役——地图
        # 唯一、在默认位置；只留诊断开关 ENV_MODE。
        monkeypatch.delenv("GQT_ATTRIBUTION_MAP", raising=False)
        monkeypatch.setenv(truth_select.ENV_MODE, "auto")

    def _patch_rebuild(self, monkeypatch, calls: list):
        def fake_run(root, targets, recipe, test_attribution=False,
                     attribution_out=None, **kwargs):
            calls.append(1)
            assert test_attribution is True
            assert attribution_out
            Path(attribution_out).write_text(json.dumps({
                "schema_version": NEW_SCHEMA,
                "arms": {"gut_full": {}},
                "arms_state": {"gut_full": {"state": "valid",
                                            "invalid_reason": None,
                                            "bundles": 1, "pairs": 1,
                                            "probes_fired": 64,
                                            "vacuous": False}},
                "target_files": ["calc.gd"], "file_sha256": {},
                "report_recipe_hash": "x"}), encoding="utf-8")

        monkeypatch.setattr(
            "godot_qa_toolkit.measurement.orchestrator.run_measurement",
            fake_run)

    def test_default_map_old_schema_atomically_rebuilt(
            self, tmp_path, monkeypatch):
        proj, target = _project(tmp_path)
        install_recipe(proj)
        default_map = proj / ".qa-cache" / "attribution-map.json"
        self._write_map(default_map, OLD_SCHEMA)
        self._auto_env(monkeypatch)
        self._patch_rebuild(monkeypatch, calls := [])
        truth_select.reset_ledger()

        out = truth_select.ensure_map(proj, [str(target)])

        assert out == str(default_map)
        assert json.loads(default_map.read_text())["schema_version"] == \
            NEW_SCHEMA
        assert calls == [1]
        actions = [e.get("action") for e in truth_select.ledger()["events"]]
        assert "rebuild" in actions

    def test_default_map_invalid_state_atomically_rebuilt(
            self, tmp_path, monkeypatch):
        proj, target = _project(tmp_path)
        install_recipe(proj)
        default_map = proj / ".qa-cache" / "attribution-map.json"
        self._write_map(default_map, NEW_SCHEMA, with_arms=False)
        self._auto_env(monkeypatch)
        self._patch_rebuild(monkeypatch, calls := [])
        truth_select.reset_ledger()

        out = truth_select.ensure_map(proj, [str(target)])

        doc = json.loads(default_map.read_text())
        assert out == str(default_map)
        assert doc["schema_version"] == NEW_SCHEMA
        assert doc.get("arms", {}).get("gut_full") == {}
        assert calls == [1]

    def test_explicit_map_never_rebuilt_or_deleted(self, tmp_path,
                                                   monkeypatch):
        """C3-R20 迁移（定案 20）：显式/环境变量地图入口退役——遗留
        GQT_ATTRIBUTION_MAP 被无视，ensure_map 只作用于默认位置的地图；
        外部文件本体零接触。"""
        proj, target = _project(tmp_path)
        install_recipe(proj)
        external = tmp_path / "explicit" / "map.json"
        raw = self._write_map(external, OLD_SCHEMA)
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP", str(external))
        monkeypatch.setenv(truth_select.ENV_MODE, "auto")
        self._patch_rebuild(monkeypatch, calls := [])
        truth_select.reset_ledger()

        out = truth_select.ensure_map(proj, [str(target)])

        assert out == str(proj / ".qa-cache" / "attribution-map.json")
        assert json.loads(Path(out).read_text())["schema_version"] == \
            NEW_SCHEMA
        assert external.read_text() == raw      # 外部文件逐字节未动
        assert calls == [1]                      # 重建只发生在默认位置

    def test_explicit_old_schema_map_fails_loud_file_intact(
            self, tmp_path, monkeypatch):
        """C3-R20 迁移（定案 20 / A2）：显式 `--attribution-map` 退役 →
        argparse 用法错 exit 2；外部文件本体零接触。"""
        proj, target = _project(tmp_path)
        external = tmp_path / "explicit" / "map.json"
        raw = self._write_map(external, OLD_SCHEMA)
        monkeypatch.setenv(truth_select.ENV_MODE, "off")

        with pytest.raises(SystemExit) as ei:
            cli.main(["mutation", str(target),
                      "--project-root", str(proj),
                      "--attribution-map", str(external),
                      "--jobs", "1", "--dry-run"])
        assert ei.value.code == 2
        assert external.read_text() == raw
