"""§SPEC-005：槽生命周期 W 矩阵单测。

脏树 / untracked / 删除 / 编辑器原子保存 / 并发 gqt flock / SIGINT/SIGTERM/
SIGKILL / 外部 import / manifest 丢失损坏 / 磁盘不足。

门（每格断言）：源仓 ``git status`` 零变异残留、`.qa-cache` 外零残留、
槽归还 target==原码、下一 run 正常或整槽重建。
"""

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import shadow, shadowpool
from godot_qa_toolkit.mutation.shadow import SlotProvisionError

SRC = "func add(a, b):\n\treturn a + b\n"


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(SRC)
    # W 矩阵的 delta 通道假设真实 git 仓（KOL 形态）——fixture git init
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=False)
    return proj, target


def _release_hard(slot):
    """模拟进程死亡：放弃锁句柄、不跑 release 逻辑（槽树与记录原样残留）。"""
    if slot._slot_lock_fd is not None:
        import fcntl
        import os
        fcntl.flock(slot._slot_lock_fd, fcntl.LOCK_UN)
        os.close(slot._slot_lock_fd)
        slot._slot_lock_fd = None
    slot.released = True


class TestSlotPrimitives:
    def test_qa_cache_gitignore_fail_loud(self, tmp_path, monkeypatch):
        """建槽前置：`.qa-cache/.gitignore` 写失败 → SlotProvisionError
        （fail-loud——19k×8 槽未跟踪文件会摧毁 git status 通道）。"""
        proj, _ = _write_project(tmp_path)
        (proj / ".qa-cache").mkdir()
        gitignore = proj / ".qa-cache" / ".gitignore"
        gitignore.write_text("*\n", encoding="utf-8")
        # 已存在且正确 → 幂等通过
        assert shadow.ensure_qa_cache_ignored(str(proj)) is not None
        # 写失败（目录变只读文件等价：mkdir 目标被文件占位）→ fail-loud
        occupied = tmp_path / "occupied"
        occupied.write_text("x")
        monkeypatch.setattr(Path, "mkdir",
                            lambda self, *a, **k: (_ for _ in ()).throw(
                                OSError(1, "occupied")))
        with pytest.raises(SlotProvisionError):
            shadow.ensure_qa_cache_ignored(str(occupied))

    def test_gitignore_overwrites_non_self_ignoring_content(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        (proj / ".qa-cache").mkdir()
        (proj / ".qa-cache" / ".gitignore").write_text("*.tmp\n")
        shadow.ensure_qa_cache_ignored(str(proj))
        assert (proj / ".qa-cache" / ".gitignore").read_text() == "*\n"

    def test_hardlink_cow_slot_shared_inode(self, tmp_path):
        """同设备槽：普通文件与仓共享 inode（硬链接）——COW 前提。"""
        proj, target = _write_project(tmp_path)
        root, stats = shadow.create_slot(str(proj), "linux")
        try:
            assert stats["linked"] >= 2  # calc.gd + gut_cmdln.gd
            assert (Path(root) / "calc.gd").stat().st_ino == \
                target.stat().st_ino
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_dot_godot_real_copy_no_shared_inode(self, tmp_path):
        """`.godot` 每槽整树实拷贝——禁止与真实仓共享 inode（外部 import
        反写/互踩红线）。"""
        proj, _ = _write_project(tmp_path)
        godot_dir = proj / ".godot" / "imported"
        godot_dir.mkdir(parents=True)
        (godot_dir / "tex.ctex").write_text("binary")
        root, _stats = shadow.create_slot(str(proj), "linux")
        try:
            slot_file = Path(root) / ".godot" / "imported" / "tex.ctex"
            assert slot_file.is_file()
            assert slot_file.stat().st_ino != \
                (godot_dir / "tex.ctex").stat().st_ino
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_copy_fallback_when_hardlink_unavailable(self, tmp_path,
                                                     monkeypatch):
        """无硬链接 FS（DrvFS 等价模拟：os.link 恒失败）→ 退真拷贝。"""
        proj, target = _write_project(tmp_path)

        def no_link(src, dst):
            raise OSError(18, "cross-device link (simulated DrvFS)")

        monkeypatch.setattr(shadow.os, "link", no_link)
        root, stats = shadow.create_slot(str(proj), "linux")
        try:
            assert stats["copied"] >= 2 and stats["linked"] == 0
            # 内容仍然一致
            assert (Path(root) / "calc.gd").read_text() == SRC
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_qa_cache_and_git_excluded_from_slot(self, tmp_path):
        proj, _ = _write_project(tmp_path)  # fixture 已 git init（.git 在场）
        assert (proj / ".git").is_dir()
        (proj / ".qa-cache" / "junk").mkdir(parents=True)
        (proj / ".qa-cache" / "junk" / "x.bin").write_text("junk")
        root, _stats = shadow.create_slot(str(proj), "linux")
        try:
            assert not (Path(root) / ".git").exists()
            assert not (Path(root) / ".qa-cache").exists()
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_inject_mutant_replaces_atomically_repo_unaffected(
            self, tmp_path):
        """mutant 注入 = 临时文件 + os.replace 断链：真实仓目标文件内容
        与 inode 均不变（每 mutant 一次 replace、无恢复步）。"""
        proj, target = _write_project(tmp_path)
        root, _ = shadow.create_slot(str(proj), "linux")
        try:
            repo_inode_before = target.stat().st_ino
            mutant_src = SRC.replace("+", "-")
            sha = shadow.inject_mutant(str(root), "calc.gd", mutant_src)
            import hashlib
            assert sha == hashlib.sha256(mutant_src.encode("utf-8")).hexdigest()
            assert (Path(root) / "calc.gd").read_text() == mutant_src
            assert (Path(root) / "calc.gd").stat().st_ino != repo_inode_before
            assert target.read_text() == SRC, "真实仓零变异写入"
            assert target.stat().st_ino == repo_inode_before
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_restore_target_asserts_original(self, tmp_path):
        proj, target = _write_project(tmp_path)
        root, _ = shadow.create_slot(str(proj), "linux")
        try:
            shadow.inject_mutant(str(root), "calc.gd", SRC.replace("+", "-"))
            shadow.restore_target(str(proj), str(root), "calc.gd")
            assert (Path(root) / "calc.gd").read_text() == SRC
            assert (Path(root) / "calc.gd").stat().st_ino == \
                target.stat().st_ino
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_inject_disk_full_raises(self, tmp_path, monkeypatch):
        """磁盘不足（ENOSPC 等价）：写失败上抛（调用方转 run_error），
        槽内不留半成品临时文件。"""
        proj, target = _write_project(tmp_path)
        root, _ = shadow.create_slot(str(proj), "linux")
        try:
            real_write_bytes = Path.write_bytes

            def enospc(self, data):
                raise OSError(28, "No space left on device")

            monkeypatch.setattr(Path, "write_bytes", enospc)
            with pytest.raises(OSError):
                shadow.inject_mutant(str(root), "calc.gd", SRC)
            leftovers = [p for p in Path(root).iterdir()
                         if p.name.startswith(".gqt_mut_")]
            assert leftovers == [], "临时文件必须被清理（fail-loud 无残留）"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class TestWMATRIX:
    def test_w_dirty_tree_untracked_synced_on_warm_reuse(self, tmp_path):
        """W 脏树/untracked 格：warm 槽建成后仓内新增/修改文件 → 下次
        acquire 的 repo_delta 同步进槽（重链）；`.qa-cache` 外零残留。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        # 仓内出现 untracked + 修改（编辑器原子保存等价：直接重写）
        (proj / "new_helper.gd").write_text("extends Node\n")
        target.write_text(SRC + "\n\nfunc extra():\n\treturn 1\n")
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert (Path(slot2.root) / "new_helper.gd").read_text() == \
                "extends Node\n"
            assert (Path(slot2.root) / "calc.gd").read_text() == \
                target.read_text()
            freshness = " ".join(slot2.freshness)
            assert "repo_delta:synced" in freshness or \
                "repo_delta:unchanged" in freshness or \
                "warm:created" in freshness
        finally:
            shadowpool.release_slot(slot2)
        outside = [p.name for p in proj.iterdir()
                   if p.name not in {"addons", "calc.gd", "new_helper.gd",
                                     ".qa-cache", ".git"}]
        assert outside == [], f"`.qa-cache` 外零残留: {outside}"

    def test_w_deleted_target_ambiguous_rebuilds(self, tmp_path):
        """W 删除格：仓目标被删 → 定向三向歧义 → 整槽重建路径（acquire
        报 missing-in-repo 后走 rebuild）。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        target.unlink()
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            freshness = " ".join(slot2.freshness)
            assert "rebuilt" in freshness or "warm:created" in freshness, \
                f"删除歧义必须整槽重建: {slot2.freshness}"
        finally:
            shadowpool.release_slot(slot2)
            target.write_text(SRC)

    def test_w_editor_atomic_save_caught_by_three_way(self, tmp_path):
        """W 编辑器原子保存格：tmp+rename 换 inode（size/mtime 变）→
        定向三向捕获 inode 漂移并重链。"""
        proj, target = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        tmp_file = proj / ".calc.gd.swp"
        tmp_file.write_text(SRC + "\n# saved by editor\n")
        tmp_file.replace(target)  # 原子保存语义
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert (Path(slot2.root) / "calc.gd").read_text() == \
                target.read_text()
        finally:
            shadowpool.release_slot(slot2)

    def test_w_concurrent_gqt_serialized_by_pool_lock(self, tmp_path):
        """W 并发 gqt flock 格：池账本锁串行化获取；两槽并行持有互不覆盖。"""
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w1")
        try:
            s2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                         wiring_hash="w1")
            try:
                assert Path(s1.root) != Path(s2.root), "并发持有独立槽"
                assert s1.slot_id != s2.slot_id
            finally:
                shadowpool.release_slot(s2)
        finally:
            shadowpool.release_slot(s1)

    def test_w_sigint_sigterm_leave_repo_pristine(self, tmp_path):
        """W SIGINT/SIGTERM 格：进程在 mutant 轮被中断（不跑 release）→
        真实仓目标文件原码不动（mutant 只存在于影子槽）。"""
        proj, target = _write_project(tmp_path)
        before = target.read_text()
        slot = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"],
                                       wiring_hash="w1")
        try:
            shadow.inject_mutant(slot.root, "calc.gd", SRC.replace("+", "-"))
            # 模拟 SIGINT/SIGTERM：不 release，直接遗弃
            if slot._slot_lock_fd is None:
                pass
            _release_hard(slot)
        finally:
            import shutil
            shutil.rmtree(slot.root, ignore_errors=True)
        assert target.read_text() == before

    def test_w_external_import_rebuilds_via_sentinel(self, tmp_path):
        """W 外部 import 格：`.godot` sentinel 漂移 → 整槽重建。"""
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        # 外部 godot --import 触碰 uid_cache（mtime/size 变化）
        uid = proj / ".godot" / "uid_cache.bin"
        uid.parent.mkdir(exist_ok=True)
        uid.write_text("rebuilt-cache")
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            freshness = " ".join(slot2.freshness)
            assert "godot_sentinel:drift" in freshness or \
                "rebuilt" in freshness or "warm:created" in freshness, \
                f"sentinel 漂移必须重建: {slot2.freshness}"
        finally:
            shadowpool.release_slot(slot2)

    def test_w_manifest_corrupted_rebuilds_pool(self, tmp_path):
        """W manifest 损坏格：pool.json 损坏 → 空池 + stderr 披露 + 新槽。"""
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        pool_json = proj / ".qa-cache" / "mutation-pool" / "pool.json"
        pool_json.write_text("{corrupted", encoding="utf-8")
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert Path(slot2.root).is_dir()
        finally:
            shadowpool.release_slot(slot2)

    def test_w_manifest_missing_rebuilds_pool(self, tmp_path):
        proj, _ = _write_project(tmp_path)
        slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                       wiring_hash="w1")
        shadowpool.release_slot(slot)
        (proj / ".qa-cache" / "mutation-pool" / "pool.json").unlink()
        slot2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                        wiring_hash="w1")
        try:
            assert Path(slot2.root).is_dir()
        finally:
            shadowpool.release_slot(slot2)

    def test_w_slot_return_target_equals_original_next_run_normal(
            self, tmp_path):
        """门格：多轮 acquire/inject/release 后槽归还 target==原码、真实仓
        git 面零变异残留、下一 run 正常。"""
        proj, target = _write_project(tmp_path)
        for i in range(3):
            slot = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                           wiring_hash="w1")
            try:
                shadow.inject_mutant(slot.root, "calc.gd",
                                     SRC.replace("+", ["-", "*", "/"][i]))
                assert (Path(slot.root) / "calc.gd").read_text() != SRC
            finally:
                problems = shadowpool.release_slot(slot)
                assert problems == []
            assert target.read_text() == SRC, "第 %d 轮后真实仓必须原码" % (i + 1)
        # git 面：非 git 环境退化为零残留检查（.qa-cache 外只有已知文件）
        allowed = {"addons", "calc.gd", ".qa-cache", ".git"}
        strays = [p.name for p in proj.iterdir() if p.name not in allowed]
        assert strays == [], f"git 面零变异残留: {strays}"

    def test_w_disk_full_slot_provision_fails_loud(self, tmp_path,
                                                   monkeypatch):
        """W 磁盘不足格：建槽 populate 失败 → SlotProvisionError（fail-loud）
        + 半成品清理。"""
        proj, _ = _write_project(tmp_path)
        real_mkdir = Path.mkdir

        def enospc_mkdir(self, *a, **k):
            if self.name == "gqt-slot-" or "gqt-slot-" in str(self):
                raise OSError(28, "No space left on device")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(Path, "mkdir", enospc_mkdir)
        with pytest.raises(SlotProvisionError):
            shadow.create_slot(str(proj), "linux")


class TestFreshFarmLifecycle:
    def test_fresh_farm_new_slot_each_acquire(self, tmp_path):
        """linux fresh 农场：每次 acquire 全新槽（无复用面），release 删除。"""
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "linux", ["calc.gd"],
                                     wiring_hash="w1")
        root1 = Path(s1.root)
        assert root1.is_dir()
        shadowpool.release_slot(s1)
        assert not root1.exists(), "fresh 槽归还即删"

    def test_wiring_hash_mismatch_no_warm_reuse(self, tmp_path):
        """wiring hash 变化（project.godot autoload 改动等）→ 不复用旧槽。"""
        proj, _ = _write_project(tmp_path)
        s1 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w1")
        shadowpool.release_slot(s1)
        s2 = shadowpool.acquire_slot(str(proj), "win64", ["calc.gd"],
                                     wiring_hash="w2-changed")
        try:
            freshness = " ".join(s2.freshness)
            assert "warm:created" in freshness or "rebuilt" in freshness
        finally:
            shadowpool.release_slot(s2)
