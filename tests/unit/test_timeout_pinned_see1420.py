"""§SPEC-1420-08：timeout_pinned（定案 D5，N=2）固定组合语义单测。

streak≥2（跨 run）→ 独立 verdict：永不并入 killed、不计入 ok 门禁失败档、
报告持续可见并披露降级来源；非 timeout 解除清零（killed 合法解除）；毒 run
守卫（单 run timeout ≥50% 且 ≥5 条 → 本 run 不落 streak，披露 suspended）；
timeout_streak 决不进任何跳过黑名单（不进任何 key）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.diffengine import (
    D5_TIMEOUT_PINNED_N,
    apply_timeout_streaks,
    streak_for,
)
from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2

TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n")


def _m(i=1):
    return Mutant(file="x.gd", line=2, column=11, operator="+", original="+",
                  mutated="-", kind="AOR", scope_id="add", n=i,
                  stable_id=f"x.gd#add#{i}")


def _rec(verdict="timeout"):
    return {"file": "x.gd", "line": 2, "column": 11, "kind": "AOR",
            "operator": "+", "original": "+", "mutated": "-",
            "verdict": verdict, "reason": "GUT timed out", "line_text": "..."}


class TestStreakSemantics:
    def test_first_timeout_streak_one_verdict_stays_timeout(self):
        streak, verdict = streak_for(None, "timeout")
        assert (streak, verdict) == (1, "timeout")

    def test_second_consecutive_timeout_pins(self):
        prior = {"verdict": "timeout", "timeout_streak": 1}
        streak, verdict = streak_for(prior, "timeout")
        assert verdict == "timeout_pinned"
        assert streak == D5_TIMEOUT_PINNED_N == 2

    def test_pinned_timeout_keeps_growing(self):
        prior = {"verdict": "timeout_pinned", "timeout_streak": 2}
        streak, verdict = streak_for(prior, "timeout")
        assert (streak, verdict) == (3, "timeout_pinned")

    def test_killed_clears_streak(self):
        """killed 是合法解除——真实 kill 证据不吞。"""
        prior = {"verdict": "timeout_pinned", "timeout_streak": 3}
        streak, verdict = streak_for(prior, "killed")
        assert (streak, verdict) == (0, "killed")

    def test_survived_clears_streak(self):
        prior = {"verdict": "timeout", "timeout_streak": 1}
        assert streak_for(prior, "survived") == (0, "survived")

    def test_poison_window_holds_streak(self):
        prior = {"verdict": "timeout", "timeout_streak": 1}
        streak, verdict = streak_for(prior, "timeout", poison=True)
        assert (streak, verdict) == (1, "timeout")


class TestPoisonRunGuard:
    def _raw(self, n_timeout, n_killed):
        pairs = []
        for i in range(n_timeout):
            pairs.append((_m(i + 1), _rec("timeout")))
        for j in range(n_killed):
            pairs.append((_m(100 + j), _rec("killed")))
        return pairs

    def test_poison_window_suspends_all_streaks(self):
        raw = self._raw(n_timeout=5, n_killed=4)  # 5/9 ≥50% 且 ≥5
        out = apply_timeout_streaks(raw, {}, {"add": "S"}, 0.0)
        assert out.suspended is True
        assert out.pinned_ids == []
        # 条目 streak 不递增（此处无 prior → 保持 0），verdict 保持 plain timeout
        assert all(e["timeout_streak"] == 0 for e in out.entries
                   if e["verdict"] == "timeout")
        assert not any(e["verdict"] == "timeout_pinned" for e in out.entries)

    def test_below_count_threshold_no_poison(self):
        # 100% 超时但 <5 条 → 非毒窗口；prior streak 1 → 本轮 +1 → pinned
        priors = {f"x.gd#add#{i}": {"verdict": "timeout",
                                    "timeout_streak": 1} for i in (1, 2)}
        raw = self._raw(n_timeout=2, n_killed=0)
        out = apply_timeout_streaks(raw, priors, {"add": "S"}, 0.0)
        assert out.suspended is False
        assert len(out.pinned_ids) == 2  # streak 1→2 → pinned

    def test_below_ratio_threshold_no_poison(self):
        raw = self._raw(n_timeout=5, n_killed=6)  # ≥5 条但 5/11 < 50%
        out = apply_timeout_streaks(raw, {}, {"add": "S"}, 0.0)
        assert out.suspended is False


class TestPinnedEndToEnd:
    def test_pinned_verdict_never_merges_into_gate_or_killed(self, tmp_path,
                                                             monkeypatch):
        """端到端：首轮 timeout（streak 1）→ 二轮重判 timeout（streak 2 →
        pinned 独立档）：ok 门禁不受其影响、永不并入 killed、报告持续可见
        披露降级来源。"""
        import subprocess

        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text(TARGET_SRC)

        state = {"n": 0}

        def timeout_fake(root, timeout_s=60, tests_glob=None):
            # 每轮调用映射（3 位点）：baseline ∈ {1,5,9} 恒 ok；mutant 轮恒超时
            state["n"] += 1
            if state["n"] in (1, 5, 9):
                return (0, "baseline ok")
            raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)

        monkeypatch.setattr(mrunner, "_run_gut_on_project", timeout_fake)
        # 3 位点：首轮 3 条 timeout < 5——毒 run 守卫不触发，构建跨 run 链
        r0 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        assert r0["summary"]["timeout"] == 3  # streak 1 → plain timeout
        assert "timeout_pinned" not in r0["summary"]
        r2 = mrunner.run_mutation(str(target), str(proj), budget=10,
                                  tests_glob="res://tests/")
        # 第二轮：重判仍 timeout → streak 2 → pinned 独立档
        assert r2["summary"]["timeout_pinned"] == 3
        assert r2["summary"]["timeout"] == 0
        assert r2["summary"]["killed"] == 0
        # D5：pinned 不计入 ok 门禁失败档
        assert r2["ok"] is True
        # 报告持续可见 + 降级来源披露
        assert len(r2["failures"]) == 3
        assert all(f["verdict"] == "timeout_pinned" for f in r2["failures"])
        assert all("consecutive timeouts" in f["reason"] for f in r2["failures"])
        # v2 条目携带 streak
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        entries = store.load_entries()
        pinned_entries = [e for e in entries.values()
                          if e["verdict"] == "timeout_pinned"]
        assert len(pinned_entries) == 3
        assert all(e["timeout_streak"] == 2 for e in pinned_entries)

    def test_killed_after_pinned_clears_and_kills(self, tmp_path,
                                                  monkeypatch):
        """解除路径：pinned 位点后续 run 被真实杀死 → streak 清零、verdict
        归位 killed（真实 kill 证据不吞）。"""
        import subprocess

        proj = tmp_path / "proj"
        (proj / "addons" / "gut").mkdir(parents=True)
        (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
        target = proj / "calc.gd"
        target.write_text(TARGET_SRC)

        state = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            state["n"] += 1
            # 调用映射：每轮 baseline + 3 mutant。baseline 轮恒 ok（轮首
            # n ∈ {1,5,9}）；n ∈ {2,3,4,6,7,8} 的 mutant 轮超时；之后 kill。
            if state["n"] in (2, 3, 4, 6, 7, 8):
                raise subprocess.TimeoutExpired(cmd="godot", timeout=timeout_s)
            if state["n"] in (1, 5, 9):
                return (0, "baseline ok")
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        mrunner.run_mutation(str(target), str(proj), budget=3,
                             tests_glob="res://tests/")
        r2 = mrunner.run_mutation(str(target), str(proj), budget=3,
                                  tests_glob="res://tests/")
        assert r2["summary"]["timeout_pinned"] == 3  # 前置：已 pinned
        r3 = mrunner.run_mutation(str(target), str(proj), budget=3,
                                  tests_glob="res://tests/")
        # 解除：重判 killed → streak 清零、真实 kill 证据不吞
        assert r3["summary"].get("timeout_pinned", 0) == 0
        assert r3["summary"]["killed"] == 3
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        executed = [e for e in store.load_entries().values()
                    if e["verdict"] == "killed"]
        assert len(executed) == 3
        assert all(e["timeout_streak"] == 0 for e in executed)

    def test_streak_never_enters_any_key(self, tmp_path):
        """D5 条款：timeout_streak 决不进任何跳过黑名单/任何 key——
        file_key 组合与 streak 无关（同输入恒同 key）。"""
        from godot_qa_toolkit.mutation.resume2 import file_key
        k1 = file_key(str(tmp_path), str(tmp_path / "a.gd"), None, "linux")
        k2 = file_key(str(tmp_path), str(tmp_path / "a.gd"), None, "linux")
        assert k1 == k2  # streak 不在签名内（幂等性钉死）
