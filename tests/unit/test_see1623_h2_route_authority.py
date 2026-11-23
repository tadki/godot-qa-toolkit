"""SEE-1623 H2 — dry-run 路由状态毒化（占位身份冒充权威 scheme）行为钉。

根因（SEE-1622 R-A）：编排层 `_impl_prep` 以 `identity_pack` 缺省值
cold-shadow/1 填充 exec_scheme——该占位值在 dry-run 直通 `_core_setup`
的权威比对门（check=True）；worker 默认启用下重解析得 worker-reset/1 →
假性 route_drift，只读预览被拦而真实运行（planning 层 check=False 先
覆写）不拦。删 store/池不可治愈（权威值来自运行时占位，非持久态）。

行为契约（修复后）：
1. 编排层占位身份不声称权威（exec_scheme=None）——dry-run/实跑首次解析
   即权威，读预览不再被占位值毒化。
2. file_key 稳定：None 占位与旧缺省 cold 占位的 identity_sha1 逐位一致
   （dry-run 差分预估读侧不因占位形式变化而 miss）。
3. 既有 route_drift fail-loud 语义不放松：真权威 scheme 与实跑解析不一致
   （含 dry-run 形态）仍具名失败（SEE-1615 §SPEC-P1 契约保持）。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation import executor as executor_mod
from godot_qa_toolkit.mutation import runner as runner_mod
from godot_qa_toolkit.mutation import worker_channel as wc_mod
from godot_qa_toolkit.mutation.channel_router import RouteDecision
from godot_qa_toolkit.mutation.identity import (
    COLD_SCHEME,
    WORKER_SCHEME,
    identity_pack,
    identity_sha1,
)
from godot_qa_toolkit.mutation.inject import detect_binary_kind

_SRC = ("extends RefCounted\n\nfunc add(a, b):\n\treturn a + b\n")


def _fixture_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "tests" / "unit").mkdir(parents=True)
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n")
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\nfunc t1() -> void:\n\tpass\n", encoding="utf-8")
    target = proj / "calc.gd"
    target.write_text(_SRC, encoding="utf-8")
    return proj, target


def _worker_route(target_res):
    return RouteDecision(target_res=target_res, channel="worker",
                         reasons=("eligible_worker",),
                         closure=(target_res,))


class TestPlaceholderIdentityNotAuthoritative:
    def test_impl_prep_identity_carries_no_authority(self, tmp_path):
        """编排层占位身份 exec_scheme 必须为 None（未解析），不得用缺省
        cold 值冒充权威——这是 dry-run 毒化的直接来源。"""
        proj, target = _fixture_project(tmp_path)
        files, kind, identity, _clock, _knobs = executor_mod._impl_prep(
            [str(target)], str(proj), None, None, {})
        assert identity["exec_scheme"] is None

    def test_dry_run_placeholder_identity_no_route_drift(
            self, tmp_path, monkeypatch):
        """worker 默认启用形态（重解析=worker）：占位身份 dry-run 不得
        route_drift——只读预览与真实运行同构可达。"""
        proj, target = _fixture_project(tmp_path)
        monkeypatch.setattr(
            wc_mod, "resolve_exec_scheme",
            lambda project_root, target_res, flag=None:
                (WORKER_SCHEME, _worker_route(target_res)))
        files, _kind, identity, _clock, _knobs = executor_mod._impl_prep(
            [str(target)], str(proj), None, None, {})
        r = runner_mod._run_mutation_core(str(target), str(proj),
                                          identity=identity, dry_run=True,
                                          worker_channel=None,
                                          select_mode="full")
        assert r["ok"] is True
        assert r["summary"].get("route_drift") is not True

    def test_placeholder_sha_matches_legacy_default(self, tmp_path):
        """file_key 稳定：None 占位与旧缺省 cold 占位的身份 sha 逐位一致
        ——既有 store / dry-run 差分预估读侧不受占位形式变化影响。"""
        proj, _target = _fixture_project(tmp_path)
        kind = detect_binary_kind()
        legacy = identity_pack(str(proj), kind)
        placeholder = identity_pack(str(proj), kind, exec_scheme=None)
        assert identity_sha1(placeholder) == identity_sha1(legacy)

    def test_dry_run_true_authority_mismatch_still_drifts(
            self, tmp_path, monkeypatch):
        """fail-loud 不放松：真权威 scheme（planning 注入形态）与实跑解析
        不一致时，dry-run 同样具名 route_drift。"""
        proj, target = _fixture_project(tmp_path)
        monkeypatch.setenv("GQT_MUTATION_WORKER", "0")
        identity = identity_pack(str(proj), "linux",
                                 godot_bin="/nonexistent/godot",
                                 exec_scheme=WORKER_SCHEME)
        r = runner_mod._run_mutation_core(str(target), str(proj),
                                          identity=identity, dry_run=True,
                                          worker_channel=None,
                                          select_mode="full")
        assert r["ok"] is False
        assert r["summary"]["route_drift"] is True
        assert r["summary"]["authoritative_scheme"] == WORKER_SCHEME
        assert r["summary"]["resolved_scheme"] == COLD_SCHEME
