def test_r9_env_switches_restored_after_serial_mutation(monkeypatch):
    """C3-R9 退回4（§SPEC-034）：进程内串行 mutation 的 CLI 旗标开关只在
    本次操作期间生效——run_mutation 期间可见、返回后进程环境恢复到操作前
    （同进程后续操作不再带上前一个命令的开关）。"""
    import argparse
    import os
    from types import SimpleNamespace

    import godot_qa_toolkit.cli as cli_mod
    from godot_qa_toolkit.mutation import runner as runner_mod

    seen: dict = {}

    def fake_run_mutation(**kwargs):
        seen["check"] = os.environ.get("GQT_MUTATION_CHECK_BASELINE")
        seen["fail_on_red"] = os.environ.get(
            "GQT_MEASUREMENT_FAIL_ON_RED_BASELINE")
        return {"summary": {"jobs": 1}}

    monkeypatch.setattr(runner_mod, "run_mutation", fake_run_mutation)
    args = SimpleNamespace(
        check_baseline=True, fail_on_red_baseline=True,
        intolerant_tests=None, auto_prepare=False, dry_run=True,
        files=["x.gd"], project_root="/tmp", tests=None, budget=1,
        timeout=1, mutation_warning=None, timeout_factor=1.0,
        mutate_all=False, mutation=None, select_mode="line",
        worker_channel=False)
    os.environ.pop("GQT_MUTATION_CHECK_BASELINE", None)
    os.environ.pop("GQT_MEASUREMENT_FAIL_ON_RED_BASELINE", None)
    result = cli_mod._run_mutation_serial(args, "gdscript")
    assert seen == {"check": "1", "fail_on_red": "1"}
    assert result["summary"]["jobs"] == 1
    assert "GQT_MUTATION_CHECK_BASELINE" not in os.environ
    assert "GQT_MEASUREMENT_FAIL_ON_RED_BASELINE" not in os.environ


def test_r9_env_map_restored_after_auto_map_build(tmp_path, monkeypatch):
    """C3-R9 退回4（§SPEC-034）：自动建图写入的 GQT_ATTRIBUTION_MAP 只在
    本次操作内生效，操作结束恢复进程环境（显式 --attribution-map 预设值
    原样保留）。"""
    import os

    import godot_qa_toolkit.mutation.truth_select as ts

    calls: dict = {}

    def fake_generate(*_a, **_k):
        # 模拟操作内写入 ENV_MAP（与产品写点同位等价）
        out = tmp_path / ".qa-cache" / "attribution-map.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}", encoding="utf-8")
        os.environ[ts.ENV_MAP] = str(out)
        calls["during"] = os.environ.get(ts.ENV_MAP)
        return str(out)

    monkeypatch.setattr(ts, "mode", lambda: "auto")
    monkeypatch.setattr(ts, "_ensure_generate", fake_generate)
    monkeypatch.setattr(ts, "_default_map_reuse",
                        lambda *_a, **_k: (None, ""))
    monkeypatch.setenv(ts.ENV_MAP, "/explicit/preset.json")
    got = ts.ensure_map(str(tmp_path), ["x.gd"], force=True)
    assert got == str(tmp_path / ".qa-cache" / "attribution-map.json")
    assert calls["during"] == got  # 操作内指向本次产物
    assert os.environ.get(ts.ENV_MAP) == "/explicit/preset.json"  # 恢复
