"""SEE-1405 §SPEC-001：store key 路径成分归一（项目内相对路径）单测。

定案（plan-debate）：resume/timing key 的路径成分从绝对 realpath 改为
项目根相对路径——投影/跨检出的同一份源码必须产出同一 key。

SEE-1463 §SPEC-003：v1 resume（resume.py）物理删除——resume 侧 key 断言
迁移到 resume2 的 file_key（路径归一语义不变）；timing 部分保留。
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from godot_qa_toolkit.mutation.ops import OPS_VERSION
from godot_qa_toolkit.mutation.resume2 import ResumeStoreV2, file_key
from godot_qa_toolkit.timing import TimingStore, store_key


def _mk_checkout(tmp_path: Path, root_name: str = "ck") -> Path:
    """一个最小项目检出：root/calc.gd + root/tests/。"""
    root = tmp_path / root_name
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n",
                                  encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_calc.gd").write_text("extends RefCounted\n", encoding="utf-8")
    return root


def _sha_of(p):
    return hashlib.sha1(p.read_bytes()).hexdigest()


class TestCrossCheckoutKeyStability:
    def test_resume_key_same_across_checkouts(self, tmp_path):
        root_a = _mk_checkout(tmp_path, "checkout_a")
        root_b = _mk_checkout(tmp_path, "checkout_b")
        target_a = root_a / "calc.gd"
        target_b = root_b / "calc.gd"
        assert file_key(str(root_a), str(target_a), "res://tests/") == \
            file_key(str(root_b), str(target_b), "res://tests/")

    def test_timing_key_same_across_checkouts(self, tmp_path):
        root_a = _mk_checkout(tmp_path, "checkout_a")
        root_b = _mk_checkout(tmp_path, "checkout_b")
        assert store_key(str(root_a), str(root_a / "calc.gd"),
                         "mutation", "res://tests/") == \
            store_key(str(root_b), str(root_b / "calc.gd"),
                      "mutation", "res://tests/")

    def test_relative_path_is_the_differentiator(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        other = _mk_checkout(tmp_path, "ck2")
        # 同一检出内不同相对路径 = 不同 key（路径成分仍有判别力）
        assert file_key(str(root), str(root / "calc.gd"), None) != \
            file_key(str(root), str(other / "calc.gd"), None)

    def test_key_still_discriminates_content_and_args(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        target = root / "calc.gd"
        base = file_key(str(root), str(target), "res://tests/")
        assert base != file_key(str(root), str(target), None)
        assert base != file_key(str(root), str(target), "res://other/")

    def test_ops_version_bumped_invalidates_old_keys(self, tmp_path, monkeypatch):
        root = _mk_checkout(tmp_path, "ck")
        target = root / "calc.gd"
        from godot_qa_toolkit.mutation import resume2
        real_version = resume2.OPS_VERSION
        before = file_key(str(root), str(target), None)
        monkeypatch.setattr("godot_qa_toolkit.mutation.resume2.OPS_VERSION",
                            "see1405-test/999")
        after = file_key(str(root), str(target), None)
        monkeypatch.setattr("godot_qa_toolkit.mutation.resume2.OPS_VERSION",
                            real_version)
        assert before != after
        assert resume2.OPS_VERSION == real_version


class TestStoreRoundTripUnderRelativeKeys:
    def test_envelope_mismatch_still_rejected(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        store = ResumeStoreV2(str(root), str(root / "calc.gd"), None)
        store.ensure_store_dir()
        entry = store.entry_path("calc.gd#add#1")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps({"schema": "see1400-resume/1",
                                     "file_key": "forged",
                                     "id": "calc.gd#add#1",
                                     "verdict": "killed"}),
                         encoding="utf-8")
        assert store.load_entries() == {}

    def test_timing_store_key_uses_relative_across_instances(self, tmp_path):
        root_a = _mk_checkout(tmp_path, "ck_a")
        root_b = _mk_checkout(tmp_path, "ck_b")
        ta = TimingStore(str(root_a), str(root_a / "calc.gd"), "mutation",
                         "res://tests/")
        tb = TimingStore(str(root_b), str(root_b / "calc.gd"), "mutation",
                         "res://tests/")
        assert ta.key == tb.key
        assert ta.entry_path.name == tb.entry_path.name

    def test_out_of_project_file_deterministic_fallback(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        outside = tmp_path / "elsewhere.gd"
        outside.write_text("x", encoding="utf-8")
        k1 = file_key(str(root), str(outside), None)
        k2 = file_key(str(root), str(outside), None)
        assert k1 == k2


class TestOpsVersion:
    def test_ops_version_bumped_for_see1405(self):
        assert OPS_VERSION != "see1400/1"
