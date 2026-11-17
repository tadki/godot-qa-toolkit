"""SEE-1594 §SPEC-022：coverage 插桩事务化恢复 / 陈旧现场自愈 / fail-loud。

覆盖：正常、异常、GUT 超时（finally 覆盖）后真实目标零残留；SIGKILL 类
不可拦截中断由启动自愈恢复；无法确认清洁时 fail-loud；活跃事务不误动；
且证明插桩确实发生在事务窗口内（就地写、事务记录在场）。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal

import pytest

import godot_qa_toolkit.coverage.runner as c

GD_SOURCE = "extends Node\n\nfunc f(a):\n\tif a > 0:\n\t\treturn 1\n\treturn 0\n"
PROBE = "\nstatic func __qa_cov_probe_0():\n\tpass\n"


def _proj(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "utils" / "t.gd"
    target.parent.mkdir(parents=True)
    target.write_text(GD_SOURCE, encoding="utf-8")
    return proj, target


def _sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _txn_entries(proj):
    d = proj / ".qa-cache" / "coverage-txn"
    return sorted(d.glob("*")) if d.is_dir() else []


def _fake_gut(proj, ids, monkeypatch, captured, *, during=None):
    real = c._instrument

    def spy(src, sink_name):
        captured["sink"] = os.path.join(proj, sink_name)
        return real(src, sink_name)

    monkeypatch.setattr(c, "_instrument", spy)

    def fake(*args, **kwargs):
        if during is not None:
            during()
        target = next((proj / "utils").glob("*.gd"))
        with open(captured["sink"], "w") as fh:
            for pid in ids:
                fh.write(f"{pid}\n")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(c, "spawn_godot", fake)


def test_normal_run_restores_target_and_clears_txn(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    sha0, ino0 = _sha(target), target.stat().st_ino
    sig0 = signal.getsignal(signal.SIGTERM)
    seen = {}

    def during():
        # 事务窗口内：目标确为插桩态，事务记录在场（证明就地写 + 可恢复事务）
        seen["instrumented"] = c._PROBE_PREFIX in target.read_text(encoding="utf-8")
        seen["txn"] = len(_txn_entries(proj))

    _fake_gut(proj, {0, 1, 2, 3}, monkeypatch, captured:={}, during=during)
    res = c.run_coverage(str(target), str(proj), min_percent=80.0)

    assert res["ok"] is True
    assert seen["instrumented"] is True and seen["txn"] >= 1
    assert _sha(target) == sha0 and target.stat().st_ino == ino0
    assert _txn_entries(proj) == []
    assert signal.getsignal(signal.SIGTERM) is sig0  # handler 已还原


def test_timeout_restores_target_and_clears_txn(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    sha0 = _sha(target)

    def boom(*a, **k):
        raise c.subprocess.TimeoutExpired(cmd="godot", timeout=1)

    monkeypatch.setattr(c, "spawn_godot", boom)
    res = c.run_coverage(str(target), str(proj))

    assert res["ok"] is False
    assert _sha(target) == sha0
    assert _txn_entries(proj) == []


def test_exception_restores_target_and_clears_txn(tmp_path, monkeypatch):
    proj, target = _proj(tmp_path)
    sha0 = _sha(target)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(c, "spawn_godot", boom)
    with pytest.raises(RuntimeError):
        c.run_coverage(str(target), str(proj))
    assert _sha(target) == sha0
    assert _txn_entries(proj) == []


def test_preinstrumented_target_fails_loud(tmp_path):
    proj, target = _proj(tmp_path)
    target.write_text(GD_SOURCE + PROBE, encoding="utf-8")
    res = c.run_coverage(str(target), str(proj))
    assert res["ok"] is False
    assert "already instrumented" in res["summary"]["error"]


def _write_txn(proj, target, original, *, pid, txn_id="dead"):
    txn_dir = proj / ".qa-cache" / "coverage-txn"
    txn_dir.mkdir(parents=True, exist_ok=True)
    backup = txn_dir / f"{txn_id}.orig"
    backup.write_text(original, encoding="utf-8")
    (txn_dir / f"{txn_id}.json").write_text(json.dumps({
        "path": str(target), "pid": pid,
        "sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "backup": str(backup), "started_at": 0,
    }), encoding="utf-8")
    return txn_dir


def test_selfheal_restores_stale_txn(tmp_path):
    proj, target = _proj(tmp_path)
    target.write_text(GD_SOURCE + PROBE, encoding="utf-8")  # SIGKILL 残留现场
    _write_txn(proj, target, GD_SOURCE, pid=999999)
    out = c.selfheal_stale_instrumentation(str(proj))
    assert out["healed"] == 1
    assert target.read_text(encoding="utf-8") == GD_SOURCE
    assert _txn_entries(proj) == []


def test_selfheal_fails_loud_when_uncertain(tmp_path):
    proj, target = _proj(tmp_path)  # 目标已被用户改回（不再含探针）
    _write_txn(proj, target, GD_SOURCE, pid=999999)
    with pytest.raises(c.CoverageTxnError):
        c.selfheal_stale_instrumentation(str(proj))
    assert target.read_text(encoding="utf-8") == GD_SOURCE  # 不静默覆盖


def test_selfheal_skips_active_txn(tmp_path):
    proj, target = _proj(tmp_path)
    target.write_text(GD_SOURCE + PROBE, encoding="utf-8")
    _write_txn(proj, target, GD_SOURCE, pid=os.getpid(), txn_id="active")
    out = c.selfheal_stale_instrumentation(str(proj))
    assert out["skipped_active"] == 1 and out["healed"] == 0
    assert target.read_text(encoding="utf-8") == GD_SOURCE + PROBE
