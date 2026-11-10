"""SEE-1550 §SPEC-020 ①——静态路由器单测（定案 A1–A9 排除语义）。

8 形态 fixture 断言：autoload_form / preload_boot 必判冷（目标入表 /
闭包成员入表），六形态必判 worker；GUT 域排除；reason 码全量披露。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.channel_router import (
    CHANNEL_COLD,
    CHANNEL_WORKER,
    REASON_CLOSURE_INTERSECTS_GUT_DOMAIN,
    REASON_CLOSURE_MEMBER_IN_AUTOLOAD,
    REASON_ELIGIBLE_WORKER,
    REASON_TARGET_IN_AUTOLOAD,
    REASON_TARGET_IN_GUT_DOMAIN,
    compute_reload_closure,
    route_project_census,
    route_target,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "see1550_forms"

FORMS = {
    "runtime_load": "res://forms/runtime_load.gd",
    "preload_boot": "res://forms/preload_boot.gd",
    "preload_late": "res://forms/preload_late.gd",
    "autoload_form": "res://forms/autoload_form.gd",
    "composition_root": "res://forms/composition_root.gd",
    "class_name_form": "res://forms/class_name_form.gd",
    "scene_script": "res://forms/scene_script.gd",
    "inner_class_form": "res://forms/inner_class_form.gd",
    "orphan_form": "res://forms/orphan_form.gd",
}


@pytest.fixture(scope="module")
def router_env():
    return {"root": str(FIXTURE), "graph": None, "table": None}


def _route(env, target_key):
    return route_target(env["root"], FORMS[target_key],
                        graph=env["graph"],
                        autoload_table=env["table"])


class TestFixtureRouting:
    """§SPEC-020 目标1 判据：autoload_form/preload_boot 必判冷，
    其余形态必判 worker。"""

    def test_autoload_form_target_in_table_is_cold(self, router_env):
        d = _route(router_env, "autoload_form")
        assert d.is_cold
        assert REASON_TARGET_IN_AUTOLOAD in d.reasons

    def test_preload_boot_carried_by_autoload_is_cold(self, router_env):
        """preload_boot 自身不在表，但被 autoload_form 静态承载（const
        preload）→ 闭包成员入表 → 冷（J3 实测 8/8 阻断组的静态重合）。"""
        d = _route(router_env, "preload_boot")
        assert d.is_cold
        member_reasons = [r for r in d.reasons
                          if r.startswith(REASON_CLOSURE_MEMBER_IN_AUTOLOAD)]
        assert member_reasons, d.reasons
        assert any("autoload_form" in r for r in member_reasons)

    @pytest.mark.parametrize("key", [
        "runtime_load", "class_name_form", "inner_class_form",
        "scene_script", "composition_root", "orphan_form",
    ])
    def test_six_forms_eligible_worker(self, router_env, key):
        d = _route(router_env, key)
        assert d.channel == CHANNEL_WORKER, (key, d.reasons)
        assert REASON_ELIGIBLE_WORKER in d.reasons

    def test_preload_late_eligible_worker(self, router_env):
        """晚期 preload（const 在测试脚本与组合根）——不触 autoload 表。"""
        d = _route(router_env, "preload_late")
        assert d.channel == CHANNEL_WORKER

    def test_closure_of_runtime_load_contains_test_holder(self, router_env):
        d = _route(router_env, "runtime_load")
        assert any("test_forms" in m for m in d.closure)


class TestGutDomainExclusion:
    def test_target_in_gut_domain_is_cold(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "helper.gd").write_text(
            "extends RefCounted\nfunc f() -> int:\n\treturn 1\n")
        (proj / "project.godot").write_text("config_version=5\n")
        d = route_target(str(proj), "res://addons/gut/helper.gd")
        assert d.is_cold
        assert REASON_TARGET_IN_GUT_DOMAIN in d.reasons

    def test_closure_intersecting_gut_domain_is_cold(self, tmp_path):
        """reload 集与 GUT 域相交（非 gut 目标被 gut 脚本引用）→ 冷
        （synthesis §3.7：reload 集与 GUT 域相交 → 强制冷通道）。"""
        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "loader.gd").write_text(
            'extends RefCounted\nconst T := preload("res://forms/x.gd")\n')
        (proj / "forms").mkdir()
        (proj / "forms" / "x.gd").write_text(
            "extends RefCounted\nfunc f() -> int:\n\treturn 1\n")
        (proj / "project.godot").write_text("config_version=5\n")
        d = route_target(str(proj), "res://forms/x.gd")
        assert d.is_cold
        inter = [r for r in d.reasons
                 if r.startswith(REASON_CLOSURE_INTERSECTS_GUT_DOMAIN)]
        assert inter and "loader.gd" in inter[0]


class TestCensus:
    def test_fixture_census_counts(self, router_env):
        census = route_project_census(router_env["root"])
        # fixture .gd 总数：9 forms + vendored + test_forms + vendor test
        # = 12 个目标（tests/ 下的 .gd 也是合法变异目标——显式指定面）
        assert census["total_targets"] == 12
        cold = {d["target"] for d in census["decisions"]
                if d["channel"] == CHANNEL_COLD}
        assert FORMS["autoload_form"] in cold
        assert FORMS["preload_boot"] in cold
        worker = {d["target"] for d in census["decisions"]
                  if d["channel"] == CHANNEL_WORKER}
        assert FORMS["runtime_load"] in worker
        assert census["worker_eligible"] + census["cold_targets"] \
            == census["total_targets"]

    def test_census_share_bounds(self, router_env):
        census = route_project_census(router_env["root"])
        assert 0.0 <= census["worker_share"] <= 1.0
