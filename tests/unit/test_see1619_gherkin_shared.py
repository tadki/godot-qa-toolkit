"""SEE-1619 §SPEC-005/006（C2）— acceptance 共享分片与临时具名降级单测。

四组反例：全原子（process_fallbacks=[]）、临时失败（共享红→隔离绿→具名
降级）、共享/隔离双红（不包装为有效降级）、清理失败（残留→invalid）。
另覆盖共享组 keys>1、并发上限、fanout 组数、对账失败分组整组降级、
process_fallbacks 排序与键面（恰含 key/reason）。run_arm 以桩替身承载，
真实 Godot 执行由 KOL 真副本 measurement 实跑复验。
"""

from __future__ import annotations

from pathlib import Path

from godot_qa_toolkit.measurement import arms


def _proj(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "addons" / "gut").mkdir(parents=True)
    (root / "addons" / "gut" / "gut_cmdln.gd").write_text("# gut\n")
    (root / "addons" / "gqt_gherkin").mkdir(parents=True)
    (root / "addons" / "gqt_gherkin" / "harness.gd").write_text(
        "extends RefCounted\n")
    return root


def _script(key: str) -> str:
    return (f"res://gherkin/generated/.gqt-run-x/generated/"
            f"test_{key}.gd")


def _make_fake_run_arm(shared_red: set[str], isolated_red: set[str],
                       shared_rec_fail: bool = False,
                       isolated_rec_fail: bool = False,
                       fb_state: str = "valid",
                       fb_rec_present: bool = True):
    """按轮次与分组构造 run_arm 桩结果：第一轮=共享，第二轮=隔离。
    §SPEC-025：fb_state / fb_rec_present 支撑隔离轮证据形态反例。"""
    calls: list[dict] = []

    def fake(name: str, *, gherkin=None, **kw):
        calls.append({"groups": gherkin["groups"],
                      "concurrency": kw.get("concurrency")})
        rnd = 0 if len(calls) == 1 else 1
        reds = shared_red if rnd == 0 else isolated_red
        rec_fail = shared_rec_fail if rnd == 0 else isolated_rec_fail
        pairs: list[tuple[str, str]] = []
        rec_shards = []
        for gi, g in enumerate(gherkin["groups"]):
            for k in g["keys"]:
                if k in reds:
                    pairs.append((_script(k), "test_bad"))
            # 注入的对账失败只落在第一组（可定位整组降级语义）
            rec_shards.append({"ok": not (rec_fail and gi == 0),
                               "keys": list(g["keys"])})
        ok_pairs = sorted(f"{s}::{t}" for s, t in pairs)
        rec: dict | None = None
        if rnd == 0 or fb_rec_present:
            rec = {"ok": not rec_fail, "shards": rec_shards}
        return {"arm": name,
                "state": "valid" if rnd == 0 else fb_state,
                "gut_ok": not pairs,
                "wall_s": 1.0, "run_uuid": f"uuid-{rnd}",
                "arm_fail_pairs": set(pairs),
                "arm_failures": {t for _, t in pairs},
                "new_pairs": set(pairs),
                "test_failures": {
                    "observed": sorted({t for _, t in pairs}),
                    "observed_pairs": ok_pairs,
                    "new": sorted({t for _, t in pairs}),
                    "new_pairs": ok_pairs,
                    "instrumentation_intolerant": []},
                "reconciliation": rec,
                "bundles_data": {}}

    return fake, calls


def _wire(monkeypatch, tmp_path: Path, fake_run_arm) -> None:
    counter = {"n": 0}

    def fake_compile(project_root, keys, suite_dir):
        pass

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm", fake_run_arm)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    # SEE-1624：flow 入口预检不在本组测试语义内（stub 两阶段：夹具无
    # addon schema、且不得拉真 Godot）；预检语义有专项测试覆盖。
# SEE-1624：flow 入口预检不在本组测试语义内（stub 两阶段：夹具无
    # addon schema、且不得拉真 Godot）；预检语义有专项测试覆盖。
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})


KEYS = ["k1", "k2", "k3", "k4"]


def _flow(root: Path, shards: int = 2) -> dict:
    return arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=shards,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})


# ---------- 反例 1：全原子 ----------

def test_all_atomic_single_shared_round_empty_fallbacks(
        tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm(set(), set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert out["process_fallbacks"] == []
    assert out["state"] == "valid" and out["gut_ok"] is True
    # 单轮共享：仅一次 run_arm
    assert len(calls) == 1
    # 共享组 keys>1：2 组（k1,k3 / k2,k4）
    assert [sorted(g["keys"]) for g in calls[0]["groups"]] == [
        ["k1", "k3"], ["k2", "k4"]]
    # 并发上限 = shards = 组数，不突破
    assert calls[0]["concurrency"] == 2


def test_fanout_groups_equal_shards(tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm(set(), set())
    _wire(monkeypatch, tmp_path, fake)
    _flow(root, shards=4)
    assert len(calls[0]["groups"]) == 4, "组数=分片数"
    assert all(len(g["keys"]) == 1 for g in calls[0]["groups"]), \
        "keys==shards 时退化为单 key 组"


# ---------- 反例 2：临时失败（共享红→隔离绿） ----------

def test_temporary_failure_isolated_green_disclosed(tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm({"k2"}, set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert len(calls) == 2, "降级轮必须发生"
    assert out["process_fallbacks"] == [
        {"key": "k2", "reason": "shared_red_isolated_green"}]
    # 共享红对被隔离证据推翻：残集无 k2
    assert all("test_k2.gd" not in p
               for p in out["test_failures"]["observed_pairs"])
    assert out["gut_ok"] is True and out["state"] == "valid"
    # 隔离轮：单例组（keys 恰为 1）
    assert all(len(g["keys"]) == 1 for g in calls[1]["groups"])
    assert sorted(g["keys"][0] for g in calls[1]["groups"]) == ["k2"]


def test_fallbacks_sorted_and_exact_shape(tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k4", "k1"}, set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    keys = [f["key"] for f in out["process_fallbacks"]]
    assert keys == sorted(keys) == ["k1", "k4"]
    for entry in out["process_fallbacks"]:
        assert set(entry.keys()) == {"key", "reason"}, "每项恰含 key/reason"


def test_shared_rec_failure_group_falls_back_whole(tmp_path, monkeypatch):
    """对账失败分组：整组 key 进入隔离轮（结构面全组存疑）。"""
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm(set(), {"k1", "k3"},
                                     shared_rec_fail=True)
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    # 组 1 = [k1,k3] 对账失败 → 两 key 都进隔离轮
    fb_keys = sorted(g["keys"][0] for g in calls[1]["groups"])
    assert fb_keys == ["k1", "k3"]
    # 隔离也红：不得包装为降级
    assert out["process_fallbacks"] == []
    assert out["state"] == "invalid"


# ---------- 反例 3：共享/隔离双红 ----------

def test_double_red_not_wrapped_as_fallback(tmp_path, monkeypatch):
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm({"k2"}, {"k2"})
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert len(calls) == 2, "隔离轮发生"
    assert out["process_fallbacks"] == [], "双红不得包装为有效降级"
    assert out["gut_ok"] is False
    assert out["state"] == "invalid"
    assert any("test_k2.gd" in p
               for p in out["test_failures"]["observed_pairs"]), \
        "共享红保持可见"


# ---------- 反例 4：harness 契约位零触退役钉（SEE-1624 单根裁决）----------

def test_flow_never_touches_harness_contract_position(
        tmp_path, monkeypatch):
    """cp 接线退役：flow 全程不得创建/删除 harness 契约位——任何对契约位
    的 unlink 即旧接线回潮，本钉变红。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm(set(), set())
    dest = root / "addons" / "gqt_gherkin" / "harness.gd"
    before = dest.read_bytes()

    counter = {"n": 0}

    def fake_compile(project_root, keys, suite_dir):
        pass

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "run_arm", fake)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})
    real_unlink = Path.unlink
    unlink_targets: list[Path] = []

    def spy_unlink(self, *a, **k):
        unlink_targets.append(self)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    out = _flow(root, shards=2)
    assert out["state"] == "valid"
    assert dest.read_bytes() == before
    assert dest not in unlink_targets
    assert out.get("process_fallbacks") == []


def test_fallback_concurrency_capped_by_shards(tmp_path, monkeypatch):
    """§SPEC-005/006（C2-R1）：降级轮与共享轮共用同一并发上限——多 key
    失败时并发不按失败数放开（不得重新引入扇出/OOM 风险）。"""
    root = _proj(tmp_path)
    # 共享轮 4 key 全红，隔离轮全绿：降级轮 4 个单例组、并发仍 ≤ shards=2
    fake, calls = _make_fake_run_arm({"k1", "k2", "k3", "k4"}, set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert len(calls) == 2
    assert len(calls[1]["groups"]) == 4, "4 key 全部进降级轮"
    assert calls[1]["concurrency"] == 2, "降级轮并发 ≤ gherkin_shards"
    assert calls[0]["concurrency"] == 2
    assert sorted(f["key"] for f in out["process_fallbacks"]) == [
        "k1", "k2", "k3", "k4"]
    assert out["state"] == "valid" and out["gut_ok"] is True


def test_fallback_concurrency_capped_when_shards_exceed_groups(
        tmp_path, monkeypatch):
    """shards > 组数时上限=组数（不空转），仍不超过 shards。"""
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm({"k1", "k2"}, set())
    _wire(monkeypatch, tmp_path, fake)
    _flow(root, shards=4)  # 4 key / shards=4 → 4 单 key 组
    assert calls[0]["concurrency"] == 4
    assert calls[1]["concurrency"] == 4, "降级组数=2 ≤ shards，上限=组数"


def test_merged_result_is_json_serializable(tmp_path, monkeypatch):
    """C3-R2 回归：fallback 合并后的臂文档必须 JSON 可序列化——合并轮
    曾把顶层 arm_fail_pairs/arm_failures/new_pairs 存成 set，使 cli.py
    写报告时 TypeError 崩溃（A/A 三轮 rc=1 的根因）。"""
    import json as _json
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, set())
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert out["process_fallbacks"] == [{"key": "k2",
                                         "reason": "shared_red_isolated_green"}]
    dumped = _json.dumps(out, ensure_ascii=False, indent=1)
    assert "shared_red_isolated_green" in dumped
    for key in ("arm_fail_pairs", "arm_failures", "new_pairs"):
        assert not isinstance(out.get(key), (set, frozenset)), key


def test_merged_result_remaining_fail_semantics(tmp_path, monkeypatch):
    """隔离红 key 的红对以字符串列表保持，合并后 remaining 判定不弱化。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, {"k2"})
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert isinstance(out["arm_fail_pairs"], list)
    assert any("test_k2.gd" in p for p in out["arm_fail_pairs"])
    assert out["state"] == "invalid"


def test_early_failure_path_carries_process_fallbacks(tmp_path, monkeypatch):
    """§SPEC-006（C2-R1）：早失败路径（suite_error）返回字典同样带
    process_fallbacks 键（空数组）——成功与 invalid 形状一致，消费方
    不可能在错误路径 KeyError。"""
    root = _proj(tmp_path)

    def boom(project_root, keys, suite_dir):
        from godot_qa_toolkit.gherkin.suite import SuiteError
        raise SuiteError("injected compile failure")

    counter = {"n": 0}

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" /
             f".gqt-run-{counter['n']}")
        d.mkdir(parents=True, exist_ok=True)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", boom)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "process_fallbacks" in out, "早失败路径必须带该键"
    assert out["process_fallbacks"] == []


def test_isolated_rec_failure_not_wrapped_as_fallback(tmp_path, monkeypatch):
    """§SPEC-011 hardener：隔离轮对账失败（即使零红对）不得当作"隔离绿"
    ——ok_keys 判定必须同时要求隔离轮 reconciliation ok。对账失败的
    隔离证据结构面存疑，不得具名降级，共享红保持可见。"""
    root = _proj(tmp_path)
    fake, calls = _make_fake_run_arm({"k2"}, set(), isolated_rec_fail=True)
    _wire(monkeypatch, tmp_path, fake)
    out = _flow(root, shards=2)
    assert len(calls) == 2, "降级轮必须发生"
    assert out["process_fallbacks"] == [], \
        "隔离轮对账失败不得产出降级披露"
    assert out["state"] == "invalid", "对账存疑时不得转 valid"
    assert out["gut_ok"] is False
    assert any("test_k2.gd" in p
               for p in out["test_failures"]["observed_pairs"]), \
        "共享红保持可见（未被隔离证据推翻）"


def test_replay_fail_cleans_all_group_suites(tmp_path, monkeypatch):
    """共享轮 replay 失败：全部组 suite 目录（含降级轮已建组）回收。"""
    root = _proj(tmp_path)

    def fake_compile(project_root, keys, suite_dir):
        if "k2" in keys:
            from godot_qa_toolkit.gherkin.suite import SuiteError
            raise SuiteError("injected replay/compile failure")

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    made: list[Path] = []
    counter = {"n": 0}

    def fake_suite_dir(r):
        counter["n"] += 1
        n = counter["n"]
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" / f".gqt-run-{n}")
        d.mkdir(parents=True, exist_ok=True)
        made.append(d)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "suite_error" in (out.get("invalid_reason") or "")
    assert all(not d.exists() for d in made), "共享轮已建组全部回收"


# ---------- §SPEC-025：隔离绿必须以 valid + 对账 ok 证据为前提 ----------

def _fb_evidence_flow(root, monkeypatch, tmp_path, fake):
    _wire(monkeypatch, tmp_path, fake)
    return _flow(root, shards=2)


def test_fallback_invalid_state_stays_red(tmp_path, monkeypatch):
    """§SPEC-025：fallback state=invalid（timeout/invalid）时即使无观测红，
    也不得包装为隔离绿——共享红保持、结果 invalid、无降级披露。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, set(), fb_state="invalid")
    out = _fb_evidence_flow(root, monkeypatch, tmp_path, fake)
    assert out["state"] != "valid" and out["gut_ok"] is False
    assert out["process_fallbacks"] == [], "invalid 证据不得进降级披露"
    assert any("test_k2.gd" in p
               for p in out["test_failures"]["observed_pairs"]), \
        "共享红必须保持可见"


def test_fallback_missing_reconciliation_stays_red(tmp_path, monkeypatch):
    """§SPEC-025：缺 reconciliation 证据（None）时默认 ok=True 是假绿
    通道——缺证据一律保持红。"""
    root = _proj(tmp_path)
    fake, _calls = _make_fake_run_arm({"k2"}, set(), fb_rec_present=False)
    out = _fb_evidence_flow(root, monkeypatch, tmp_path, fake)
    assert out["state"] != "valid" and out["gut_ok"] is False
    assert out["process_fallbacks"] == []
    assert any("test_k2.gd" in p
               for p in out["test_failures"]["observed_pairs"])


def _fb_compile_setup(monkeypatch, tmp_path):
    """公共接线：记录每个 create_suite_dir 产物；compile 可按 chunk 注炸。"""
    made: list[Path] = []
    counter = {"n": 0}

    def fake_replay(project_root, key, *, suite_dir=None):
        return {}

    def fake_suite_dir(r):
        counter["n"] += 1
        n = counter["n"]
        counter["n"] += 1
        d = (Path(r) / "gherkin" / "generated" / f".gqt-run-{n}")
        d.mkdir(parents=True, exist_ok=True)
        made.append(d)
        # 所有权标记内容 = 目录名（绑定身份契约）
        (d / ".gqt-owner").write_text(d.name + "\n", encoding="utf-8")
        return d

    monkeypatch.setattr(arms, "replay_check", fake_replay)
    monkeypatch.setattr(arms, "_snapshot_main", lambda r: {})
    monkeypatch.setattr(arms, "create_suite_dir", fake_suite_dir)
    return made


def test_second_fallback_compile_failure_cleans_fb_suites(
        tmp_path, monkeypatch):
    """§SPEC-025 HIGH：第二个 singleton 编译失败时，已建的 fb suite 目录
    （含前一个 singleton）必须全部回收，返回后零残留。"""
    from godot_qa_toolkit.gherkin.suite import SuiteError
    root = _proj(tmp_path)
    made = _fb_compile_setup(monkeypatch, tmp_path)

    def fake_compile(project_root, keys, suite_dir):
        if list(keys) == ["k2"]:
            raise SuiteError("injected second fb compile failure")

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    fake, _calls = _make_fake_run_arm({"k1", "k2"}, set())
    monkeypatch.setattr(arms, "run_arm", fake)
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "suite_error" in (out.get("invalid_reason") or "")
    assert all(not d.exists() for d in made), \
        f"suite 残留: {[str(d) for d in made if d.exists()]}"


def test_error_path_suite_cleanup_failure_not_valid(tmp_path, monkeypatch):
    """§SPEC-025：异常路径 suite 清理失败 = 残留 = invalid_reason 必须具名
    suite_residue，不得静默吞掉。"""
    from godot_qa_toolkit.gherkin.suite import SuiteError
    root = _proj(tmp_path)
    made = _fb_compile_setup(monkeypatch, tmp_path)
    fb_dir_seen: list[Path] = []

    def fake_compile(project_root, keys, suite_dir):
        if list(keys) == ["k2"]:
            raise SuiteError("injected second fb compile failure")

    monkeypatch.setattr(arms, "compile_keys_to_suite", fake_compile)
    real_cleanup = arms._cleanup_suite_dir

    def flaky_cleanup(sd, project_root):
        fb_dir_seen.append(Path(sd))
        if len(fb_dir_seen) >= 2:
            raise OSError("injected cleanup failure")
        return real_cleanup(sd, project_root)

    monkeypatch.setattr(arms, "_cleanup_suite_dir", flaky_cleanup)
    fake, _calls = _make_fake_run_arm({"k1", "k2"}, set())
    monkeypatch.setattr(arms, "run_arm", fake)
    monkeypatch.setattr(arms, "compile_preflight",
                        lambda root, **k: {"ok": True})
    monkeypatch.setattr(arms, "run_validator_preflight",
                        lambda root, **k: {"ok": True})
    out = arms.run_gherkin_flow(
        root=root,
        recipe={"gherkin_face": {"features": [
            {"key": k} for k in KEYS]}},
        gherkin_keys=None, gherkin_shards=2,
        arms_parallel=False, gherkin_adaptive=False, arm_kwargs={})
    assert out["state"] == "invalid"
    assert "suite_residue" in (out.get("invalid_reason") or ""), \
        "清理失败必须具名，不得静默"
