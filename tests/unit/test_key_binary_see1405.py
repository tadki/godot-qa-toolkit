"""SEE-1405 §SPEC-012 子集：key 增二进制标识成分单测（发现二 MEDIUM）。

发现二（Revy P1 实测）：key 归一后无二进制成分——linux run 的 verdict
覆写 win64 run 的 killed 条目（跨二进制 store 互写）。修复：resume/timing
key 增二进制标识成分。

SEE-1463 §SPEC-003：v1 resume（resume.py）物理删除——resume 侧 key 断言
迁移到 resume2 的 file_key（binary 成分语义不变）；timing 部分保留。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit.mutation import inject
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2, file_key
from godot_qa_toolkit.timing import TimingStore, store_key


@pytest.fixture(autouse=True)
def _fresh_kind_cache():
    inject.reset_binary_kind_cache()
    yield
    inject.reset_binary_kind_cache()


def _mk_checkout(tmp_path: Path, name: str = "ck") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n",
                                  encoding="utf-8")
    return root


def _sha_of(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


class TestKeyBinaryComponent:
    def test_resume_key_discriminates_binary_kind(self, tmp_path,
                                                  monkeypatch):
        root = _mk_checkout(tmp_path)
        target = root / "calc.gd"
        monkeypatch.setattr("godot_qa_toolkit.mutation.inject."
                            "_BINARY_KIND_CACHE", "win64")
        k_win = file_key(str(root), str(target), None, binary_kind="win64")
        monkeypatch.setattr("godot_qa_toolkit.mutation.inject."
                            "_BINARY_KIND_CACHE", "linux")
        k_linux = file_key(str(root), str(target), None, binary_kind="linux")
        assert k_win != k_linux

    def test_timing_key_discriminates_binary_kind(self, tmp_path,
                                                  monkeypatch):
        root = _mk_checkout(tmp_path)
        monkeypatch.setattr("godot_qa_toolkit.mutation.inject."
                            "_BINARY_KIND_CACHE", "win64")
        k_win = store_key(str(root), str(root / "calc.gd"), "mutation", None)
        monkeypatch.setattr("godot_qa_toolkit.mutation.inject."
                            "_BINARY_KIND_CACHE", "linux")
        k_linux = store_key(str(root), str(root / "calc.gd"), "mutation", None)
        assert k_win != k_linux

    def test_store_roundtrip_blocks_cross_binary_reuse(self, tmp_path,
                                                       monkeypatch):
        """端到端语义（发现二）：win64 落的 killed 条目，linux run 不得命中。"""
        root = _mk_checkout(tmp_path)
        target = root / "calc.gd"
        store_win = ResumeStoreV2(str(root), str(target), "res://tests/",
                                  binary_kind="win64")
        store_win.record_entry({"id": "calc.gd#add#1", "kind": "AOR",
                                "original": "a + b", "mutated": "a - b",
                                "line": 1, "verdict": "killed"})
        store_linux = ResumeStoreV2(str(root), str(target), "res://tests/",
                                    binary_kind="linux")
        assert store_win.key != store_linux.key
        assert store_linux.load_entries() == {}

    def test_ops_version_bumped_again_for_binary_component(self, tmp_path,
                                                           monkeypatch):
        # resume2 模块 from-import 绑定的是模块属性——monkeypatch resume2 侧
        # 绑定模拟「版本回退」并断言当前版本 key 不同
        from godot_qa_toolkit.mutation import resume2
        real_version = resume2.OPS_VERSION
        monkeypatch.setattr("godot_qa_toolkit.mutation.resume2.OPS_VERSION",
                            "see1405/2")
        root = _mk_checkout(tmp_path, "ck_ops")
        target = root / "calc.gd"
        k_v2 = file_key(str(root), str(target), None, binary_kind="win64")
        monkeypatch.setattr("godot_qa_toolkit.mutation.resume2.OPS_VERSION",
                            real_version)
        k_v3 = file_key(str(root), str(target), None, binary_kind="win64")
        assert k_v2 != k_v3

    def test_binary_kind_fingerprint_is_type_not_path(self, tmp_path,
                                                      monkeypatch):
        """成分是「类型标识」而非二进制路径——同类型不同安装位置同 key。"""
        root = _mk_checkout(tmp_path)
        target = root / "calc.gd"
        k1 = file_key(str(root), str(target), None, binary_kind="linux")
        k2 = file_key(str(root), str(target), None, binary_kind="linux")
        assert k1 == k2
        assert k1 != file_key(str(root), str(target), None,
                              binary_kind="win64")


class TestStoreCtorWiring:
    def test_resume_store_accepts_binary_kind(self, tmp_path):
        root = _mk_checkout(tmp_path)
        s = ResumeStoreV2(str(root), str(root / "calc.gd"), None,
                          binary_kind="linux")
        assert s.binary_kind == "linux"

    def test_timing_store_accepts_binary_kind(self, tmp_path):
        root = _mk_checkout(tmp_path)
        t = TimingStore(str(root), str(root / "calc.gd"), "mutation", None,
                        binary_kind="linux")
        assert t.binary_kind == "linux"

    def test_store_default_kind_resolves_from_env(self, tmp_path, monkeypatch):
        """不显式传 binary_kind 时按 inject.detect_binary_kind 解析——
        生产调用点（cli 层钉死后）语义自动一致。"""
        root = _mk_checkout(tmp_path)
        monkeypatch.setattr("godot_qa_toolkit.mutation.inject."
                            "_BINARY_KIND_CACHE", "linux")
        s = ResumeStoreV2(str(root), str(root / "calc.gd"), None)
        assert s.binary_kind == "linux"