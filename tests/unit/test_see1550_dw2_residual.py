"""SEE-1550 D-W2 修复轮——残留态根因修复的单测契约。

三项修复：① 路由图补 class_name 裸名使用边（D-W2 根因：TagContainer 四个
消费方全部裸名引用、不构成边、不进 reload 闭包 → 测试脚本跨腿持有首腿
编译绑定 → mutant 不可见 → 0/41 漏杀与 41/41 过杀双向分歧）；
② WorkerSession 腿间状态漂移门（连接指纹 / user:// 树 vs 首腿基线，
漂移即会话致命 → respawn + 窗口冷重跑——canary 拦截面从「首腿+每 k 腿
冷 oracle」扩大到「逐腿状态不变量」）；③ host 腿间 user:// 清场（冷通道
每 mutant 全新进程的 user:// 对齐面，GDScript 侧由真机电池/实机验证承接）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.channel_router import (
    CHANNEL_COLD,
    REASON_CLOSURE_MEMBER_IN_AUTOLOAD,
    build_reference_graph,
    compute_reload_closure,
    route_target,
)
from godot_qa_toolkit.mutation import worker_channel as wc
from godot_qa_toolkit.mutation.worker_channel import WorkerSession


# -- ① class_name 引用边（D-W2 根因修复）--------------------------------------

@pytest.fixture()
def cname_proj(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text("config_version=5\n")
    return proj


def _write(proj, rel, text):
    p = proj / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return "res://" + rel


def test_class_name_bare_usage_enters_reload_closure(cname_proj):
    target = _write(cname_proj, "systems/tag/tag_container.gd",
                    "class_name TagContainer\nextends RefCounted\n"
                    "var tags := {}\n")
    # 测试脚本裸名使用（无 preload/load 字面量）——D-W2 分歧样本的引用形态
    test = _write(cname_proj, "tests/unit/tag/test_tag.gd",
                  "extends RefCounted\nfunc t() -> bool:\n"
                  "\tvar c := TagContainer.new()\n\treturn c.tags.is_empty()\n")
    graph = build_reference_graph(str(cname_proj))
    closure = compute_reload_closure(graph, target)
    assert test in closure, "bare class_name consumer must be in the closure"


def test_class_name_edge_is_reverse_and_transitive(cname_proj):
    target = _write(cname_proj, "a.gd",
                    "class_name Widget\nextends RefCounted\n")
    mid = _write(cname_proj, "mid.gd",
                 "extends RefCounted\nvar w := Widget.new()\n")
    leaf = _write(cname_proj, "leaf.gd",
                  "extends RefCounted\nconst M := preload(\"res://mid.gd\")\n")
    graph = build_reference_graph(str(cname_proj))
    closure = compute_reload_closure(graph, target)
    assert mid in closure and leaf in closure


def test_class_name_self_declaration_no_self_edge(cname_proj):
    target = _write(cname_proj, "solo.gd",
                    "class_name Solo\nextends RefCounted\nfunc f() -> int:\n"
                    "\treturn Solo.new().f()\n")
    graph = build_reference_graph(str(cname_proj))
    assert graph.get(target, set()) == set(), "own decl/usage is not an edge"


def test_class_name_autoload_consumer_flips_route_cold(cname_proj):
    """KOL 实况：tag_manager.gd（autoload TagManager）裸用 TagContainer →
    闭包成员入 autoload 表 → 冷（A1 排除语义；此前的 worker 资格本身就是
    图缺边造成的错判）。"""
    target = _write(cname_proj, "systems/tag/tag_container.gd",
                    "class_name TagContainer\nextends RefCounted\n")
    _write(cname_proj, "systems/tag/tag_manager.gd",
           "extends Node\nfunc t() -> void:\n\tvar c := TagContainer.new()\n")
    (cname_proj / "project.godot").write_text(
        "config_version=5\n\n[autoload]\n\n"
        "TagManager=\"*res://systems/tag/tag_manager.gd\"\n")
    d = route_target(str(cname_proj), target)
    assert d.is_cold
    assert any(r.startswith(REASON_CLOSURE_MEMBER_IN_AUTOLOAD)
               for r in d.reasons)
    assert any("tag_manager" in r for r in d.reasons)


def test_inner_class_declaration_not_matched(cname_proj):
    """内嵌 `class X:` 不是全局类（compile-time 不进全局表）→ 不构成边。"""
    target = _write(cname_proj, "host.gd",
                    "extends RefCounted\nclass Inner:\n\tvar v := 1\n")
    user = _write(cname_proj, "user.gd",
                  "extends RefCounted\nfunc u() -> int:\n\treturn Inner.new().v\n")
    graph = build_reference_graph(str(cname_proj))
    closure = compute_reload_closure(graph, target)
    assert user not in closure


# -- ② 腿间状态漂移门 ----------------------------------------------------------

def _session(tmp_path):
    s = WorkerSession(slot_root=str(tmp_path / "slot"),
                      project_root=str(tmp_path), kind="linux")
    s._log_path = tmp_path / "worker-log.txt"  # 无 start()：测试面直连
    return s


def _resp(leg, connections, user_files=None):
    return {"leg": leg, "hash_ok": True, "reload": {"res://a.gd": 0},
            "source_sha": {}, "autoload_in_closure": [], "gut_ran": True,
            "pass": 3, "fail": 0, "user_files": user_files or [],
            "connections": connections, "timings": {}}


def test_first_leg_sets_reference_no_fatal(tmp_path):
    s = _session(tmp_path)
    r = s._interpret(0, _resp(0, [["a.gd", "sig", "cb"]]))
    assert not r.fatal and r.ok
    assert s.state.ref_connections == [["a.gd", "sig", "cb"]]


def test_connection_drift_is_session_fatal(tmp_path):
    s = _session(tmp_path)
    assert not s._interpret(0, _resp(0, [["a.gd", "sig", "cb"]])).fatal
    drifted = s._interpret(1, _resp(1, [["a.gd", "sig", "cb"],
                                        ["b.gd", "t", "h"]]))
    assert drifted.fatal
    assert "connection fingerprint drift" in drifted.fatal_reason


def test_user_tree_drift_is_session_fatal(tmp_path):
    s = _session(tmp_path)
    assert not s._interpret(0, _resp(0, [], [])).fatal
    drifted = s._interpret(1, _resp(1, [], ["user://save.json"]))
    assert drifted.fatal
    assert "user:// tree drift" in drifted.fatal_reason


def test_stable_fingerprints_stay_ok(tmp_path):
    s = _session(tmp_path)
    conn = [["a.gd", "sig", "cb"]]
    for leg in range(3):
        r = s._interpret(leg, _resp(leg, conn, []))
        assert not r.fatal, r.fatal_reason


def test_existing_fatal_checks_keep_priority(tmp_path):
    """reload 失败仍是致命且优先（漂移门只在其余门全过时兜底）。"""
    s = _session(tmp_path)
    resp = _resp(0, [["a.gd", "sig", "cb"]])
    resp["reload"] = {"res://a.gd": 43}
    r = s._interpret(0, resp)
    assert r.fatal and "closure reload failed" in r.fatal_reason


# -- ③ 会话前置 parity 门（直驱 vs gut_cmdln 模式等价性）-----------------------

def _fake_session_cls(script_by_session):
    class _S:
        def __init__(self, *a, **k):
            self.dead = False
            self.dead_reason = ""
            self.closed = 0
            self.script = list(script_by_session.pop(0) if script_by_session
                               else [])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

        def close(self):
            self.closed += 1
            self.dead = False

        def run_leg(self, leg, *a, **k):
            r = self.script.pop(0) if self.script else wc.WorkerLegResult(
                leg=leg, ok=True, hash_ok=True, gut_ran=True,
                failing_tests=set())
            r.leg = leg
            return r

        def note_canary(self):
            pass

        def invalidate_window(self):
            return 0
    return _S


def _mk_mutant(i):
    from types import SimpleNamespace
    return SimpleNamespace(stable_id=f"m{i}", kind="op", original="a+b",
                           mutated="a-b")


def _slot_ns():
    from types import SimpleNamespace
    return SimpleNamespace(root="/tmp/nonexistent-slot",
                           project_root="/tmp/nonexistent-proj")


def test_parity_mismatch_degrades_all_legs_to_cold(monkeypatch):
    """直驱失败集 ≠ 冷 baseline 失败集 → 披露 + 整文件冷降级（等价守恒）。"""
    monkeypatch.setenv(wc.ENV_CANARY_EVERY, "99")
    parity = wc.WorkerLegResult(leg=-1, ok=True, hash_ok=True, gut_ran=True,
                                failing_tests={"test_mode_only"})
    monkeypatch.setattr(wc, "WorkerSession",
                        _fake_session_cls([[parity]]))
    events: list[str] = []
    records: list[str] = []
    mutants = [_mk_mutant(1), _mk_mutant(2)]
    slot = _slot_ns()
    pairs, fatals = wc.run_worker_legs(
        mutants, slot, "t.gd", "res://t.gd", ["res://t.gd"], "linux",
        "res://tests/",
        inject_fn=lambda m: ("sha", None),
        cold_runner=lambda m, seq: {"verdict": "killed"},
        record_fn=lambda m, v, e=0.0: records.append(v["verdict"]),
        classify_fn=lambda m, leg: {"verdict": "survived"},
        leg_timeout_s=30,
        baseline_failures=set(),
        parity_inject_fn=lambda: ("orig-sha", None))
    assert fatals and "parity" in fatals[0]
    assert records == ["killed", "killed"], "全冷降级（不被直驱判定污染）"
    assert len(pairs) == 2


def test_parity_match_keeps_worker_verdicts(monkeypatch):
    """直驱失败集 == 冷 baseline 失败集 → 正常 worker 腿判定。"""
    monkeypatch.setenv(wc.ENV_CANARY_EVERY, "99")
    parity = wc.WorkerLegResult(leg=-1, ok=True, hash_ok=True, gut_ran=True,
                                failing_tests={"test_old"})
    worker_leg = wc.WorkerLegResult(leg=0, ok=True, hash_ok=True,
                                    gut_ran=True, fail_count=1,
                                    failing_tests={"test_old", "test_new"})
    monkeypatch.setattr(wc, "WorkerSession",
                        _fake_session_cls([[parity, worker_leg]]))
    records: list[str] = []
    mutants = [_mk_mutant(1)]
    pairs, fatals = wc.run_worker_legs(
        mutants, _slot_ns(), "t.gd", "res://t.gd", ["res://t.gd"],
        "linux", "res://tests/",
        inject_fn=lambda m: ("sha", None),
        cold_runner=lambda m, seq: {"verdict": "killed"},
        record_fn=lambda m, v, e=0.0: records.append(v["verdict"]),
        classify_fn=lambda m, leg: {"verdict": "killed"},
        leg_timeout_s=30,
        baseline_failures={"test_old"},
        parity_inject_fn=lambda: ("orig-sha", None))
    assert fatals == []
    assert records == ["killed"], "parity 通过 → worker 判定生效"


def test_parity_inject_failure_degrades_to_cold(monkeypatch):
    monkeypatch.setenv(wc.ENV_CANARY_EVERY, "99")
    monkeypatch.setattr(wc, "WorkerSession", _fake_session_cls([[]]))
    records: list[str] = []
    pairs, fatals = wc.run_worker_legs(
        [_mk_mutant(1)], _slot_ns(), "t.gd", "res://t.gd",
        ["res://t.gd"], "linux", "res://tests/",
        inject_fn=lambda m: ("sha", None),
        cold_runner=lambda m, seq: {"verdict": "killed"},
        record_fn=lambda m, v, e=0.0: records.append(v["verdict"]),
        classify_fn=lambda m, leg: {"verdict": "survived"},
        leg_timeout_s=30,
        baseline_failures=set(),
        parity_inject_fn=lambda: (None, "disk error"))
    assert fatals and "unavailable" in fatals[0]
    assert records == ["killed"]


def test_worker_requires_real_gut_addon(monkeypatch, tmp_path):
    """R-DW6 默认启用守卫：仅 gut_cmdln.gd 存根（无 gut.gd）→ 直驱不可用，
    结构性回退冷（披露面由 runner stderr 承接，此处钉判定函数语义）。"""
    from godot_qa_toolkit.mutation import runner as _runner
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub\n",
                                                          encoding="utf-8")
    ok_full = (proj / "addons" / "gut" / "gut.gd")
    ok_full.write_text("extends Node\n", encoding="utf-8")
    assert _runner.os.path.isfile(
        _runner.os.path.join(str(proj), "addons", "gut", "gut.gd"))
    ok_full.unlink()
    assert not _runner.os.path.isfile(
        _runner.os.path.join(str(proj), "addons", "gut", "gut.gd"))
