"""SEE-1594 S6.3：产品映射构建路径的懒基线门控反例（§SPEC-060a/064b）。

方案 A 必须落在产品面：`run_measurement`（measure run --test-attribution
引擎）与 `truth_select.ensure_map`（gqt mutation 默认路径）共享测量层
懒获取 helper——不再只由 harness 自建基线。反例矩阵：

- 基线吸收 → 臂有效、map 原生携带 baseline_failures_meta；
- 懒获取到的新增失败 → 臂 invalid 具名（fail-closed 不弱化）；
- 基线获取失败（无运行流）→ fail-loud 具名 stderr + 臂保持 invalid；
- 显式 --baseline-failures 优先（懒获取零触发）；
- 绿臂零额外 GUT（懒获取零触发）；
- ensure_map 入口：懒吸收后 map 可用；新增失败仍 fail-loud。
"""

from __future__ import annotations

import json
import re
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.measurement.orchestrator import (
    REPORT_VERSION,
    run_measurement,
)

TARGET_SRC = """extends RefCounted


func work(n: int) -> int:
\tvar acc = 0
\tfor i in range(n):
\t\tacc += i
\treturn acc
"""

_FAIL_FLOW = (
    "Godot Engine v4.4.1\n"
    "res://tests/unit/test_game.gd\n"
    "  * test_font_row\n"
    "\t\ttest_font_row [Failed]\n"
    "= Run Summary\n"
    "res://tests/unit/test_game.gd\n"
    "- test_font_row\n"
    "    [Failed]:  row height drifted\n"
)
# 同一失败的两种披露形态（运行流归因对 + Run Summary 回显）。


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig_run = sp.run
    yield
    sp.run = orig_run


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="lazy-fixture"\n')
    (root / ".gutconfig.json").write_text(json.dumps(
        {"dirs": ["res://tests/"], "include_subdirs": True,
         "prefix": "test_"}))
    (root / "tests" / "unit").mkdir(parents=True)
    (root / "tests" / "unit" / "test_game.gd").write_text(
        "extends GutTest\nfunc test_ok() -> void:\n\tassert_true(true)\n")
    feats = root / "addons" / "gqt_gherkin" / "features"
    feats.mkdir(parents=True)
    (feats / "demo.feature").write_text(
        "Feature: F\n  Scenario Outline: s\n    Then hp is <left>\n"
        "    # types: <hp>:int, <left>:numeric\n    Examples:\n"
        "      | hp | left |\n      | 10 | 7    |\n")
    game = root / "src_game"
    game.mkdir()
    (game / "cell.gd").write_text(TARGET_SRC)
    from gherkin_fixtures import ensure_addon_metadata
    ensure_addon_metadata(root)
    return root


def _recipe(project: Path, tmp_path: Path) -> dict:
    from godot_qa_toolkit.measurement.recipe import build_recipe
    from attribution_fixtures import install_recipe
    install_recipe(project)
    recipe, error = build_recipe(project)
    assert error is None, error
    return recipe


def _targets(project: Path) -> list[str]:
    return sorted(str(p) for p in (project / "src_game").glob("*.gd"))


class LazyScenarioGodot:
    """按 argv 形态分流的 fake GUT：

    - `.gqt-run-`（acceptance 私有目录）→ manifest 驱动 flow + 全量 sink
    - `-gpre_run_script=`（插桩 gut_full）→ 写全量 sink + 失败流（触发懒获取）
    - 其余 gut_cmdln 调用 = 懒基线运行（无插桩无钩子）→ 按 scenario 给输出
    """

    def __init__(self, project_root: Path, scenario: str = "absorb"):
        self.real_run = sp.run
        self.calls: list[list[str]] = []
        self.lazy_calls: list[list[str]] = []
        self.root = project_root
        self.scenario = scenario

    def __call__(self, *a, **k):
        argv = list(a[0])
        self.calls.append(argv)
        cmd = " ".join(argv)
        if "gut_cmdln.gd" not in cmd:
            return self.real_run(*a, **k)
        if ".gqt-run-" in cmd:
            return self._resp(0, self._acceptance_flow(cmd, k.get("env") or {}))
        if "-gpre_run_script=" in cmd:
            self._write_sinks(k.get("env") or {})
            return self._resp(1, _FAIL_FLOW)
        # 懒基线运行（无插桩无钩子）
        self.lazy_calls.append(argv)
        if self.scenario == "absorb":
            return self._resp(1, _FAIL_FLOW)
        if self.scenario == "new_failure":
            return self._resp(
                1, _FAIL_FLOW.replace("test_font_row", "test_other_fail"))
        return self._resp(1, "Godot Engine v4.4.1 — nothing ran")

    # --- helpers ---
    def _resp(self, rc: int, out: str):
        class R:
            returncode = rc
            stdout = out
            stderr = ""
        return R()

    def _acceptance_flow(self, cmd: str, env: dict) -> str:
        m = re.search(r"-gdir=res://(\S+)/generated", cmd)
        if not m:
            return ""
        self._write_sinks(env)
        gen = self.root / m.group(1) / "generated"
        parts = []
        for man_path in sorted(gen.glob("*.manifest.json")):
            man = json.loads(man_path.read_text(encoding="utf-8"))
            parts.append("res://" + m.group(1) + "/generated/test_"
                         + man_path.name.replace(".manifest.json", ".gd"))
            for att in man.get("attribution", []):
                parts.append("* " + att["test_name"])
        return "\n".join(parts)

    def _write_sinks(self, env: dict) -> None:
        pg = self.root / "project.godot"
        proj = re.search(r'config/name\s*=\s*"([^"]+)"',
                         pg.read_text(encoding="utf-8")).group(1)
        xdg = env.get("XDG_DATA_HOME")
        dest = (Path(xdg) / "godot" / "app_userdata" / proj
                if xdg else self.root)
        for gd in self.root.glob("src_game/*.gd"):
            src = gd.read_text(encoding="utf-8")
            # runtime-pid sink 形态 + 三种探针点数形态（与 canonical
            # FakeGodot 同款：direct 逐 id 函数 / R3 store_string / R4 flag）
            pid_pairs = re.findall(
                r'qa-coverage-hits-([0-9a-f-]{8,36})-" \+ '
                r'str\(OS\.get_process_id\(\)\) \+ "-([0-9a-f]{8})\.txt',
                src)
            pairs = [(u, k, True) for u, k in pid_pairs]
            for m2 in re.finditer(
                    r"qa-coverage-hits-([0-9a-f-]{8,36})-([0-9a-f]{8})\.txt",
                    src):
                if (m2.group(1), m2.group(2)) not in pid_pairs:
                    pairs.append((m2.group(1), m2.group(2), False))
            for uuid, key8, _pid in pairs:
                sink = (f"qa-coverage-hits-{uuid}-99999-{key8}.txt"
                        if _pid else f"qa-coverage-hits-{uuid}-{key8}.txt")
                probe_ids = set(re.findall(r"__qa_cov_probe_(\d+)\(\)", src)) | set(
                    re.findall(r"__qa_cov_probe_m_[0-9a-f]{8}\[(\d+)\] = 1", src))
                ids = sorted(int(i) for i in probe_ids)
                dest.mkdir(parents=True, exist_ok=True)
                (dest / sink).write_text(
                    "".join(f"{i}\n" for i in ids), encoding="utf-8")


def _map_doc(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "map.json").read_text(encoding="utf-8"))


class TestS63LazyBaselineRunMeasurement:
    def test_absorbed_arm_valid_and_map_meta(self, project, tmp_path):
        """基线吸收 → 臂 valid、零新增；map 原生携带懒获取 meta。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake
        out = tmp_path / "map.json"
        report = run_measurement(project, _targets(project),
                                 _recipe(project, tmp_path),
                                 test_attribution=True, attribution_out=str(out))
        gut = report["arms"][0]
        assert gut["state"] == "valid", gut["invalid_reason"]
        assert gut["test_failures"]["new"] == []
        assert gut["test_failures"]["observed"] == ["test_font_row"]
        assert len(fake.lazy_calls) == 1, "懒获取恰好一次（观测到失败才触发）"
        doc = _map_doc(tmp_path)
        meta = doc.get("baseline_failures_meta")
        assert meta and meta["method"] == "uninstrumented_run"
        assert meta["lazy"] is True and meta["count"] >= 1
        assert len(meta["sha256"]) == 64 and meta["wall_s"] >= 0

    def test_new_failure_invalid_named(self, project, tmp_path):
        """懒基线之外的新增失败（模拟插桩诱发）→ 臂 invalid、具名披露。"""
        fake = LazyScenarioGodot(project, scenario="new_failure")
        sp.run = fake
        report = run_measurement(project, _targets(project),
                                 _recipe(project, tmp_path),
                                 test_attribution=True,
                                 attribution_out=str(tmp_path / "map.json"))
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["test_failures"]["new"] == ["test_font_row"]
        assert report["ok"] is False

    def test_acquisition_fail_fail_loud(self, project, tmp_path, capsys):
        """基线获取失败（无运行流）→ 具名 stderr、臂保持 invalid，
        不按空基线静默转绿。"""
        fake = LazyScenarioGodot(project, scenario="garbage")
        sp.run = fake
        report = run_measurement(project, _targets(project),
                                 _recipe(project, tmp_path),
                                 test_attribution=True,
                                 attribution_out=str(tmp_path / "map.json"))
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["test_failures"]["new"] == ["test_font_row"]
        err = capsys.readouterr().err
        assert "lazy baseline acquisition failed" in err
        doc = _map_doc(tmp_path)
        assert "baseline_failures_meta" not in doc

    def test_explicit_baseline_priority_no_lazy(self, project, tmp_path):
        """显式 baseline_failures 参数优先——懒获取零触发；
        meta method=explicit_param。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake
        run_measurement(project, _targets(project),
                        _recipe(project, tmp_path), test_attribution=True,
                        attribution_out=str(tmp_path / "map.json"),
                        baseline_failures={
                            ("res://tests/unit/test_game.gd", "test_font_row"),
                            ("", "test_font_row")})
        assert fake.lazy_calls == [], "显式基线在场时不得懒获取"
        meta = _map_doc(tmp_path).get("baseline_failures_meta")
        assert meta and meta["method"] == "explicit_param"
        assert meta["count"] == 2

    def test_explicit_baseline_nonabsorbing_no_lazy(self, project, tmp_path):
        """显式基线在场但不吸收观测失败 → 懒获取零触发（显式提供即终局，
        不二次兜底），观测失败保持具名新增、臂 invalid。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake
        report = run_measurement(project, _targets(project),
                                 _recipe(project, tmp_path),
                                 test_attribution=True,
                                 attribution_out=str(tmp_path / "map.json"),
                                 baseline_failures={("", "test_other_fail")})
        assert fake.lazy_calls == [], \
            "显式基线在场（即使未吸收）不得懒获取"
        meta = _map_doc(tmp_path).get("baseline_failures_meta")
        assert meta and meta["method"] == "explicit_param"
        gut = report["arms"][0]
        assert gut["state"] == "invalid"
        assert gut["test_failures"]["new"] == ["test_font_row"]

    def test_green_arm_zero_extra_gut(self, project, tmp_path):
        """绿臂（零观测失败）→ 懒获取零触发（不给正常仓加基线成本）。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake

        orig = fake.__class__.__call__

        def all_pass(self, *a, **k):
            argv = list(a[0])
            cmd = " ".join(argv)
            if "gut_cmdln.gd" in cmd and "-gpre_run_script=" in cmd:
                self.calls.append(argv)
                self._write_sinks(k.get("env") or {})
                return self._resp(0, "[Passed]\nRun Summary\n")
            return orig(self, *a, **k)

        fake.__class__.__call__ = all_pass
        try:
            report = run_measurement(project, _targets(project),
                                     _recipe(project, tmp_path),
                                     test_attribution=True,
                                     attribution_out=str(tmp_path / "map.json"))
        finally:
            fake.__class__.__call__ = orig
        assert report["arms"][0]["state"] == "valid"
        assert fake.lazy_calls == [], "绿臂零额外 GUT（懒获取不触发）"
        assert "baseline_failures_meta" not in _map_doc(tmp_path)


class TestS63LazyBaselineMergeGuard:
    """门控守卫单元面：_lazy_baseline_merge 触发条件逐参钉死
    （acceptance 臂永不懒获取；gut_full 观测到失败才触发）。"""

    def _drive(self, monkeypatch, *, name):
        from godot_qa_toolkit.measurement import arms as arms_mod
        calls: list[list[str]] = []

        def _spy_acquire(root, gut_args, timeout_s):
            calls.append(list(gut_args))
            return set(), {"method": "uninstrumented_run", "lazy": True,
                           "count": 0, "sha256": "0" * 64, "wall_s": 0.0}

        monkeypatch.setattr(
            "godot_qa_toolkit.measurement.orchestrator."
            "_acquire_lazy_baseline", _spy_acquire)
        out = arms_mod._lazy_baseline_merge(
            name, Path("/proj"),
            {"gut_face": {"dirs": ["res://tests/"]}}, 60,
            {("res://t/g.gd", "test_x"), ("", "test_x")},
            set(), set(), {("res://t/g.gd", "test_x"), ("", "test_x")},
            None, None)
        return calls, out

    def test_acceptance_arm_never_lazy_acquires(self, monkeypatch):
        """acceptance 臂不取懒基线——其失败保持可见（债归独立 issue）；
        门控守卫按臂名拒绝，而非依赖观测面巧合为空。"""
        calls, (eff, meta, new_pairs) = self._drive(
            monkeypatch, name="gherkin")
        assert calls == [], "acceptance 臂不得触发懒获取"
        assert meta is None
        assert new_pairs, "acceptance 臂失败保持未吸收"

    def test_gut_full_arm_lazy_acquires_on_failure(self, monkeypatch):
        """对照：同参数下 gut_full 臂懒获取触发（守卫差异仅在臂名）。"""
        calls, (_eff, meta, _np) = self._drive(monkeypatch, name="gut_full")
        assert len(calls) == 1, "gut_full 臂观测到失败时懒获取恰一次"
        assert meta and meta["lazy"] is True


class TestS63EnsureMapEntry:
    @pytest.fixture(autouse=True)
    def _auto_mode(self, monkeypatch):
        from godot_qa_toolkit.mutation import truth_select as ts
        monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
        ts._fresh_recipe_hash.cache_clear()
        ts._test_scripts_by_name.cache_clear()

    def test_ensure_map_lazy_build_usable(self, project, tmp_path):
        """mutation 默认路径（ensure_map）：懒吸收后 map 建成可用、
        臂 valid、meta 在场。"""
        fake = LazyScenarioGodot(project, scenario="absorb")
        sp.run = fake
        from godot_qa_toolkit.mutation.truth_select import ensure_map
        out = ensure_map(project, _targets(project))
        assert out and Path(out).is_file()
        doc = json.loads(Path(out).read_text(encoding="utf-8"))
        assert (doc["arms_state"]["gut_full"]["state"]) == "valid"
        meta = doc.get("baseline_failures_meta")
        assert meta and meta["method"] == "uninstrumented_run" and meta["lazy"]

    def test_ensure_map_new_failure_fail_loud(self, project, tmp_path):
        fake = LazyScenarioGodot(project, scenario="new_failure")
        sp.run = fake
        from godot_qa_toolkit.mutation.truth_select import ensure_map
        from godot_qa_toolkit.mutation.truth_select import (
            MutationSelectionError)
        with pytest.raises(MutationSelectionError):
            ensure_map(project, _targets(project))


def test_report_version_pinned():
    assert REPORT_VERSION == "measurement-report/3"
