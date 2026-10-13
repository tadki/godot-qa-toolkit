"""SEE-1405 §SPEC-001：store key 路径成分归一（项目内相对路径）单测。

定案（plan-debate）：resume/timing key 的路径成分从绝对 realpath 改为
项目根相对路径——投影/跨检出的同一份源码必须产出同一 key；OPS_VERSION
bump 承载旧 store 一次性失效（显式披露，不脏合并）。
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from godot_qa_toolkit.mutation.ops import OPS_VERSION
from godot_qa_toolkit.mutation.resume import ResumeStore, resume_key
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
        sha = _sha_of(target_a)
        assert resume_key(str(root_a), str(target_a), sha,
                          "res://tests/", 50) == \
            resume_key(str(root_b), str(target_b), sha,
                       "res://tests/", 50)

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
        sha = _sha_of(root / "calc.gd")
        # 同一检出内不同相对路径 = 不同 key（路径成分仍有判别力）
        assert resume_key(str(root), str(root / "calc.gd"), sha, None, 50) != \
            resume_key(str(root), str(other / "calc.gd"), sha, None, 50)

    def test_key_still_discriminates_content_and_args(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        target = root / "calc.gd"
        sha1, sha2 = "a" * 40, "b" * 40
        base = resume_key(str(root), str(target), sha1, "res://tests/", 50)
        assert base != resume_key(str(root), str(target), sha2, "res://tests/", 50)
        assert base != resume_key(str(root), str(target), sha1, None, 50)
        assert base != resume_key(str(root), str(target), sha1, "res://tests/", 51)

    def test_ops_version_bumped_invalidates_old_keys(self, tmp_path, monkeypatch):
        root = _mk_checkout(tmp_path, "ck")
        target = root / "calc.gd"
        sha = _sha_of(target)
        before = resume_key(str(root), str(target), sha, None, 50)
        monkeypatch.setattr("godot_qa_toolkit.mutation.resume.OPS_VERSION",
                            "see1405-test/999")
        after = resume_key(str(root), str(target), sha, None, 50)
        assert before != after


class TestStoreRoundTripUnderRelativeKeys:
    def test_resume_record_then_load_at_other_checkout(self, tmp_path):
        """端到端语义（§SPEC-001）：A 检出落的 verdict 条目搬到 B 检出 store
        （模拟 anchor 直写/共享落点），B 按 key+条目身份命中复用。

        key 归一只保证「同一份源码同 key」；条目可达性由落点（D9 直写原
        检出，方案一子步骤）负责——此处以目录搬运模拟同一 store 位置。
        """
        root_a = _mk_checkout(tmp_path, "ck_a")
        root_b = _mk_checkout(tmp_path, "ck_b")
        store_a = ResumeStore(str(root_a), str(root_a / "calc.gd"),
                              "res://tests/", 50)
        rec = {"kind": "AOR", "original": "a + b", "mutated": "a - b",
               "line": 1, "verdict": "killed", "failures": ["t"],
               "elapsed_s": 0.1}
        assert store_a.record(0, rec) is True
        store_b = ResumeStore(str(root_b), str(root_b / "calc.gd"),
                              "res://tests/", 50)
        assert store_a.key == store_b.key
        store_b.ensure_store_dir()
        (store_b.store_dir / "0.json").write_text(
            (store_a.store_dir / "0.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        mutant = SimpleNamespace(kind="AOR", original="a + b",
                                 mutated="a - b", line=1)
        completed = store_b.load_completed([mutant], 0, 1)
        assert 0 in completed and completed[0]["verdict"] == "killed"

    def test_envelope_mismatch_still_rejected(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        store = ResumeStore(str(root), str(root / "calc.gd"), None, 50)
        store.ensure_store_dir()
        entry = store.entry_path(3)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps({"schema": "see1400-resume/1",
                                     "key": "forged", "seq": 3,
                                     "verdict": "killed"}),
                         encoding="utf-8")
        assert store._read_entry(3) is None

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
        k1 = resume_key(str(root), str(outside), "s", None, 50)
        k2 = resume_key(str(root), str(outside), "s", None, 50)
        assert k1 == k2


class TestOpsVersion:
    def test_ops_version_bumped_for_see1405(self):
        assert OPS_VERSION != "see1400/1"
