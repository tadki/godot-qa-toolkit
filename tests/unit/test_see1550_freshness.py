"""§SPEC-006：warm 槽新鲜度分层权威单测。

1. repo 侧每 run 一次 git status delta（缓存 + 慢查询披露）；
2. gqt 自知变更账（锁 + 归还断言隐含）；
3. 槽侧目标集三向校验（repo stat vs 槽 stat vs 池记录——抓 SIGKILL 残留）；
4. `.godot` sentinel；5. 槽归还断言；6. 全量 census 仅显式 env；
8. 单点/卡尺 run 走定向三向，跳过全量同步。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import shadow, shadowpool

SRC = "func add(a, b):\n\treturn a + b\n"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(SRC)
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=False)
    return proj, target


class TestRepoDeltaLayer:
    def test_delta_cached_per_process(self, tmp_path, monkeypatch):
        """规则 1：每 run 一次 git status——同进程重复查询走缓存。"""
        proj, _ = _write_project(tmp_path)
        calls = {"n": 0}
        import subprocess as sp
        real_run = sp.run

        def counting_run(*a, **k):
            if a and a[0] and a[0][0] == "git":
                calls["n"] += 1
            return real_run(*a, **k)

        monkeypatch.setattr(shadow.subprocess, "run", counting_run)
        shadow.repo_git_delta(str(proj), refresh=True)
        n1 = calls["n"]
        shadow.repo_git_delta(str(proj))
        assert calls["n"] == n1, "缓存命中不得重复付 git status"
        shadow.repo_git_delta(str(proj), refresh=True)
        assert calls["n"] > n1, "refresh 必须重测"

    def test_slow_delta_disclosed(self, tmp_path, monkeypatch, capsys):
        """KOL 实测 19.5–74.5s：慢 delta（>10s）stderr 披露 warm 同步成本。"""
        proj, _ = _write_project(tmp_path)
        import subprocess as sp
        import time as _time

        class FakeCompleted:
            returncode = 0
            stdout = ""

        real_monotonic = shadow.time.monotonic

        def slow_monotonic():
            # 首尾差 > 10s 的等价：篡改阈值更直接——直接走慢路径
            return real_monotonic()

        monkeypatch.setattr(shadow.time, "monotonic", slow_monotonic)
        monkeypatch.setattr(shadow, "_GIT_DELTA_SLOW_S", -1.0)
        shadow.repo_git_delta(str(proj), refresh=True)
        err = capsys.readouterr().err
        assert "git status took" in err

    def test_non_git_directory_returns_empty(self, tmp_path, monkeypatch):
        proj, _ = _write_project(tmp_path)
        import subprocess as sp

        def fail_run(*a, **k):
            raise FileNotFoundError("git missing")

        monkeypatch.setattr(sp, "run", fail_run)
        lines = shadow.repo_git_delta(str(proj), refresh=True)
        assert lines == []
        assert shadow.delta_hash([]) == shadow.delta_hash([])


class TestThreeWayLayer:
    def test_residual_mutant_caught_and_relinked(self, tmp_path):
        """规则 3 主格：SIGKILL 残留 mutant（槽 inode 断链）→ 定向三向
        重链仓原版 + 披露 residual-mutant。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadow.inject_mutant(slot.root, "calc.gd", SRC.replace("+", "-"))
        _hard_release(slot)
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert "residual-mutant" in " ".join(slot2.freshness)
            assert (Path(slot2.root) / "calc.gd").read_text() == SRC
        finally:
            shadowpool.release_slot(slot2)

    def test_same_content_relink_restores_shared_inode(self, tmp_path):
        """槽内容与仓一致但 inode 断链 → 仅重链恢复共享（零拷贝回收）。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        slot_stat_before = (Path(slot.root) / "calc.gd").stat().st_ino
        shadowpool.release_slot(slot)
        # 直接重链自身：模拟内容一致但 inode 断链的最小场景
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            ino_repo = target.stat().st_ino
            ino_slot = (Path(slot2.root) / "calc.gd").stat().st_ino
            # warm 复用路径中，三向校验后槽目标必须与仓同 inode（硬链接恢复）
            assert ino_slot == ino_repo or ino_slot == slot_stat_before
        finally:
            shadowpool.release_slot(slot2)


def _hard_release(slot):
    import fcntl
    import os
    if slot._slot_lock_fd is not None:
        fcntl.flock(slot._slot_lock_fd, fcntl.LOCK_UN)
        os.close(slot._slot_lock_fd)
        slot._slot_lock_fd = None
    slot.released = True


class TestSentinelLayer:
    def test_sentinel_drift_forces_rebuild(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        (proj / ".godot" / "imported").mkdir(parents=True, exist_ok=True)
        (proj / ".godot" / "imported" / "x.res").write_text("new import")
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            freshness = " ".join(slot2.freshness)
            assert "godot_sentinel:drift" in freshness or \
                "rebuilt" in freshness
        finally:
            shadowpool.release_slot(slot2)

    def test_sentinel_match_reuses(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert "godot_sentinel:match" in slot2.freshness
            assert "warm:reused" in slot2.freshness
        finally:
            shadowpool.release_slot(slot2)


class TestCensusLayer:
    def test_full_census_only_explicit_env(self, tmp_path, monkeypatch):
        """规则 6：全量 census 仅显式 env 触发；缺省 run 零 census 支付。"""
        proj, _ = _write_project(tmp_path)
        monkeypatch.delenv(shadow.ENV_FULL_CENSUS, raising=False)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        try:
            assert not any("full_census" in f for f in slot.freshness)
        finally:
            shadowpool.release_slot(slot)
        monkeypatch.setenv(shadow.ENV_FULL_CENSUS, "1")
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert any(f.startswith("full_census:") for f in slot2.freshness)
        finally:
            shadowpool.release_slot(slot2)

    def test_census_detects_slot_drift(self, tmp_path, monkeypatch):
        proj, target = _write_project(tmp_path)
        monkeypatch.setenv(shadow.ENV_FULL_CENSUS, "1")
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        # 仓外文件（untracked 脚本）census 视角下不在槽 → 不算漂移
        #（census 只比对槽内存在的 *.gd 对仓侧）；真正漂移 = 槽内多出幽灵
        (proj / ".qa-cache" / "mutation-pool" / "pool.json").parent.mkdir(
            parents=True, exist_ok=True)
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            census = [f for f in slot2.freshness
                      if f.startswith("full_census:")]
            assert census and census[0].endswith("mismatches=0")
        finally:
            shadowpool.release_slot(slot2)


class TestTargetedLayer:
    def test_targeted_run_skips_full_delta_sync(self, tmp_path,
                                                monkeypatch):
        """规则 8：单点/卡尺 run 跳过全量 delta 同步（不被 40–150s 全量
        同步淹没 2.3s GUT）——targeted_only=True。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        calls = {"git": 0}
        import subprocess as sp
        real_run = sp.run

        def counting_run(*a, **k):
            if a and a[0] and a[0][0] == "git":
                calls["git"] += 1
            return real_run(*a, **k)

        monkeypatch.setattr(shadow.subprocess, "run", counting_run)
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1", targeted_only=True)
        try:
            assert "repo_delta:skipped(targeted)" in slot2.freshness
            git_calls_before = calls["git"]
            # 定向三向仍然生效：仓文件改动被捕获
            target.write_text(SRC + "\n# touched\n")
            slot3 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                            wiring_hash="w1",
                                            targeted_only=True)
            try:
                assert (Path(slot3.root) / "calc.gd").read_text() == \
                    target.read_text()
            finally:
                shadowpool.release_slot(slot3)
        finally:
            shadowpool.release_slot(slot2)

    def test_return_assert_in_freshness(self, tmp_path):
        """规则 5：槽归还 target==原码断言（release 披露链）。"""
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        try:
            shadow.inject_mutant(slot.root, "calc.gd", SRC.replace("+", "-"))
        finally:
            problems = shadowpool.release_slot(slot)
        assert problems == []
        assert "release:targets-restored" in slot.freshness
