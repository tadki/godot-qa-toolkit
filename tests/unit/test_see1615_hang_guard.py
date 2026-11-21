"""SEE-1615 §SPEC-HANG — 直驱腿挂起守卫契约。

判据：宿主执行前对脚本清单 load 校验（不可加载 → 具名 marker + quit，
不得进入 end_run 无限等待）；chunk 等待上限有界可配（默认 120s，超时具名
GQT_HOST_HUNG）；cold 分类把 marker 转具名 run_error（统一 JSON）；腿
spawn 超时下限兜底（_adaptive_timeout 缺失/为 0 时不得立即超时）。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation import killbail


def _norm(text: str) -> str:
    return " ".join(text.split())


# ------------------------------------------------------- 宿主 GDScript 契约


def test_host_verifies_scripts_before_running():
    from godot_qa_toolkit.mutation import directdrive

    seq = _norm(directdrive.SEQUENTIAL_RUNNER_GD)
    assert "func _gqt_verify_scripts(scripts: Array) -> String:" in seq
    assert "if res == null or not res.can_instantiate():" in seq
    cold = _norm(directdrive.COLD_DIRECT_GD_TEMPLATE)
    assert killbail.SCRIPTS_FAIL_MARKER in cold
    assert "quit(5)" in cold


def test_host_wait_is_bounded_and_named():
    from godot_qa_toolkit.mutation import directdrive

    seq = _norm(directdrive.SEQUENTIAL_RUNNER_GD)
    assert killbail.HOST_HUNG_MARKER in seq
    # SEE-1615 §SPEC-EVWAIT：end_run 等待 = 事件 race + 一次性超时事件
    # （无 10ms 轮询循环）
    assert "await _chunk_settled" in seq
    assert "create_timer(per_script_timeout_ms / 1000.0)" in seq
    assert "while not done" not in seq
    # cold 宿主把有界上限注入原语（不再硬编码 600000）
    cold = _norm(directdrive.COLD_DIRECT_GD_TEMPLATE)
    assert 'int(cfg.get("host_wait_ms", 120000))' in cold
    assert 'bail_chunk' in cold


def test_both_hosts_share_sequential_guard():
    from godot_qa_toolkit.mutation import directdrive, worker_session

    assert directdrive.SEQUENTIAL_RUNNER_GD in worker_session.WORKER_HOST_GD
    assert killbail.SCRIPTS_FAIL_MARKER in _norm(worker_session.WORKER_HOST_GD)
    assert killbail.HOST_HUNG_MARKER in _norm(worker_session.WORKER_HOST_GD)
    # worker end_run 等待同样事件化（§SPEC-EVWAIT）
    whost = _norm(worker_session.WORKER_HOST_GD)
    assert "await _leg_settled" in whost
    assert "while not done" not in whost


# ------------------------------------------------------------- marker 解析


def test_parse_scripts_fail_marker():
    tail = "noise\nGQT_DIRECT_SCRIPTS_FAIL path=res://sel/bad.gd\ntail"
    d = killbail.parse_scripts_fail_marker(tail)
    assert d == {"path": "res://sel/bad.gd"}
    assert killbail.parse_scripts_fail_marker("clean") is None


def test_parse_host_hung_marker():
    tail = "GQT_HOST_HUNG waited_ms=120000 scripts_run=8/24"
    d = killbail.parse_host_hung_marker(tail)
    assert d == {"waited_ms": 120000, "scripts_run": 8, "scripts_total": 24}
    assert killbail.parse_host_hung_marker("clean") is None


# ------------------------------------------------------- 有界等待 env 解析


def test_resolve_host_wait_ms_env(monkeypatch):
    monkeypatch.delenv(killbail.ENV_HOST_WAIT, raising=False)
    assert killbail.resolve_host_wait_ms() == killbail.DEFAULT_HOST_WAIT_MS
    monkeypatch.setenv(killbail.ENV_HOST_WAIT, "30000")
    assert killbail.resolve_host_wait_ms() == 30000
    for bad in ("0", "-5", "abc", ""):
        monkeypatch.setenv(killbail.ENV_HOST_WAIT, bad)
        assert killbail.resolve_host_wait_ms() == killbail.DEFAULT_HOST_WAIT_MS
    assert killbail.DEFAULT_HOST_WAIT_MS <= 120000


# --------------------------------------------- cold 分类：具名 run_error


def test_cold_classify_names_scripts_fail():
    from godot_qa_toolkit.mutation.runhelp import _sm_classify

    class _M:
        file = "utils/x.gd"
        line = 7
        column = 1
        kind = "boundary"
        operator = "0→1"
        original = "0"
        mutated = "1"
        stable_id = "s#7#0"

    tail = "GQT_DIRECT_SCRIPTS_FAIL path=res://sel/missing_target.gd"
    v = _sm_classify(_M(), 5, tail, set(), False, "extends RefCounted\n", None,
                     ".")
    assert v["verdict"] == "run_error"
    assert "direct_scripts_unloadable" in v["reason"]
    assert "missing_target.gd" in v["reason"]


def test_cold_classify_names_host_hung():
    from godot_qa_toolkit.mutation.runhelp import _sm_classify

    class _M:
        file = "utils/x.gd"
        line = 7
        column = 1
        kind = "boundary"
        operator = "0→1"
        original = "0"
        mutated = "1"
        stable_id = "s#7#0"

    tail = ("GUT preamble\nGQT_HOST_HUNG waited_ms=120000 scripts_run=8/24\n"
            "GQT_DIRECT_SCRIPTS=8/24 bail=false")
    v = _sm_classify(_M(), 1, tail, set(), False, "extends RefCounted\n", None,
                     ".")
    assert v["verdict"] == "run_error"
    assert "host_hung" in v["reason"]


# ------------------------------------------------- 腿 spawn 超时下限兜底


def test_leg_spawn_timeout_floor(tmp_path, monkeypatch):
    """_adaptive_timeout 缺失/0 时，腿 spawn 必须有有界下限（不得立即超时
    也不得无限等待）。"""
    import types

    from godot_qa_toolkit.mutation import runner as runner_mod

    captured = {}

    def fake_gut(project_root, timeout_s=600, tests_glob=None):
        captured["timeout_s"] = timeout_s
        return 0, "1 test / all pass"

    class _M:
        file = "utils/x.gd"
        line = 7
        column = 1
        kind = "boundary"
        operator = "0→1"
        original = "0"
        mutated = "1"
        stable_id = "s#7#0"

    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}")
    monkeypatch.setattr(runner_mod, "_sm_inject",
                        lambda *a, **k: ({"verdict": "x"}, str(cfg), None))
    monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake_gut)
    monkeypatch.setattr(runner_mod, "_adaptive_timeout", 0)
    verdict = runner_mod._run_single_mutant(
        _M(), __import__("pathlib").Path(__file__), ".", "extends RefCounted\n",
        "", None, 1, set(), False,
        slot=types.SimpleNamespace(root=str(tmp_path)), direct_scripts=None)
    assert captured["timeout_s"] >= 120
    assert verdict is not None
    # 既有语义不抬高：显式/自适应 60s 保留 60
    monkeypatch.setattr(runner_mod, "_adaptive_timeout", 60)
    runner_mod._run_single_mutant(
        _M(), __import__("pathlib").Path(__file__), ".", "extends RefCounted\n",
        "", None, 1, set(), False,
        slot=types.SimpleNamespace(root=str(tmp_path)), direct_scripts=None)
    assert captured["timeout_s"] == 60


# ------------------------------------------ MEDIUM-1 同帧边界回归（返修）


def test_host_hung_boundry_is_not_end():
    """MEDIUM-1 同帧边界回归：hung 判定必须为 `not settled["end"]`
    （end 与 cap 同帧先后触发时二者均已置位——end 优先，不得误报 hung），
    且不得残留 `settled["cap"]` 判定形态。"""
    from godot_qa_toolkit.mutation import directdrive, worker_session

    seq = _norm(directdrive.SEQUENTIAL_RUNNER_GD)
    assert 'if not settled["end"]:' in seq
    assert 'if settled["cap"]:' not in seq
    # worker 腿语义本就正确（gut_ran ⟺ end），回归钉住
    whost = _norm(worker_session.WORKER_HOST_GD)
    assert 'resp["gut_ran"] = done["end"]' in whost


def test_host_no_never_connected_disconnects():
    """MEDIUM-2 回归：_chunk_settled/_leg_settled 不得 disconnect on_cap
    （on_cap 只连接 cap.timeout——disconnect 不存在的连接产生引擎错误日志）。"""
    from godot_qa_toolkit.mutation import directdrive, worker_session

    seq = _norm(directdrive.SEQUENTIAL_RUNNER_GD)
    assert "_chunk_settled.disconnect(on_cap)" not in seq
    whost = _norm(worker_session.WORKER_HOST_GD)
    assert "_leg_settled.disconnect(on_cap)" not in whost
