"""SEE-1405 ③ hardener——store key 归一失效语义对抗补强。

必杀目标（§SPEC-001 高风险通道①）：
- key 路径成分回退绝对路径：伪造「当前版本 + 绝对路径」key 的自洽条目，
  正确实现必须 miss；实现回退即静默复用（红）
- OPS_VERSION bump 被回退：伪造「旧版本 + 相对路径」key 的自洽条目，同上
- 信封 verdict / 条目 id 伪造混入复用集合
- 项目外路径 fallback 语义钉死为绝对 realpath（非仅确定性）

SEE-1463 §SPEC-003：v1 resume（resume.py）物理删除——resume 侧 key 断言
迁移到 resume2 的 file_key / SCHEMA_V2 / load_entries（失效语义不变）；
timing 部分保留。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from godot_qa_toolkit.mutation.ops import OPS_VERSION
from godot_qa_toolkit.mutation.resume2 import SCHEMA_V2, ResumeStoreV2, file_key
from godot_qa_toolkit.timing import TimingStore, project_relative, store_key


def _mk_checkout(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n",
                                  encoding="utf-8")
    return root


def _plant(store_dir: Path, key: str, sid: str, **extra) -> None:
    """按自洽信封伪造一条 verdict=killed 的历史（模拟旧版本落盘/伪造残留）。"""
    store_dir.mkdir(parents=True, exist_ok=True)
    doc = {"schema": SCHEMA_V2, "file_key": key, "id": sid,
           "verdict": "killed", "kind": "AOR", "original": "a + b",
           "mutated": "a - b", "line": 1}
    doc.update(extra)
    (store_dir / f"{sid.replace('/', '__')}.json").write_text(
        json.dumps(doc), encoding="utf-8")


class TestKeyNormalizationInvalidation:
    def test_absolute_path_component_key_rejected(self, tmp_path):
        """同版本但路径成分退回绝对路径的 key 不得命中。

        变异必杀：project_relative 回退为绝对 realpath 时 file_key
        复现伪造 key → 条目被静默复用 → 断言红。
        """
        root_b = _mk_checkout(tmp_path, "ck_b")
        store = ResumeStoreV2(str(root_b), str(root_b / "calc.gd"),
                              "res://tests/")
        abs_payload = f"{Path(root_b / 'calc.gd').resolve()}|" \
                      f"res://tests/|{OPS_VERSION}"
        abs_key = hashlib.sha1(abs_payload.encode("utf-8")).hexdigest()
        assert abs_key != store.key
        _plant(store.store_dir.parent / abs_key, abs_key,
               "calc.gd#add#1")
        assert store.load_entries() == {}

    def test_pre_bump_ops_version_key_rejected(self, tmp_path):
        """旧 OPS_VERSION（see1400/1）条目随 bump 一次性失效。

        变异必杀：OPS_VERSION 回退为 see1400/1 时 file_key 复现伪造
        旧 key → 静默复用脏 verdict → 断言红。
        """
        root = _mk_checkout(tmp_path, "ck")
        store = ResumeStoreV2(str(root), str(root / "calc.gd"),
                              "res://tests/")
        legacy_payload = f"calc.gd|res://tests/|see1400/1"
        legacy = hashlib.sha1(legacy_payload.encode("utf-8")).hexdigest()
        assert legacy != store.key
        _plant(store.store_dir.parent / legacy, legacy, "calc.gd#add#1")
        assert store.load_entries() == {}

    def test_timing_absolute_key_entry_is_no_history(self, tmp_path):
        """TimingStore 同款失效：绝对路径 key 的落盘条目不得被读为历史。"""
        root_b = _mk_checkout(tmp_path, "ck_b")
        tstore = TimingStore(str(root_b), str(root_b / "calc.gd"),
                             "mutation", "res://tests/", now=1000.0)
        abs_key = hashlib.sha1(
            f"{Path(root_b / 'calc.gd').resolve()}|mutation|"
            f"res://tests/".encode("utf-8")).hexdigest()
        assert abs_key != tstore.key
        entry = tstore.store_dir / f"{abs_key}.json"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(json.dumps({
            "schema": "see1356-l1-timing/1",
            "suite_kind": "mutation",
            "tests_glob": "res://tests/",
            "git_rev": None,
            "updated_at": 1000.0,
            "samples": [12.0],
            "last": 12.0,
            "p90": 12.0,
            "samples_n": 1,
        }), encoding="utf-8")
        assert tstore.read() is None

    def test_out_of_project_fallback_is_absolute_realpath(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        outside = tmp_path / "elsewhere.gd"
        outside.write_text("x", encoding="utf-8")
        assert project_relative(str(root), str(outside)) == \
            str(outside.resolve())
        assert store_key(str(root), str(outside), "mutation", None) == \
            store_key(str(root), str(outside.resolve()), "mutation", None)


class TestForgedEntryHardening:
    def test_bad_schema_and_id_forgery_ignored(self, tmp_path):
        root = _mk_checkout(tmp_path, "ck")
        store = ResumeStoreV2(str(root), str(root / "calc.gd"), None)
        store.ensure_store_dir()
        (store.store_dir / "0.json").write_text(
            json.dumps({"schema": "other/1", "verdict": "killed"}),
            encoding="utf-8")
        no_id = store.store_dir / "calc.gd__add__9.json"
        no_id.write_text(json.dumps({"schema": SCHEMA_V2,
                                     "file_key": store.key,
                                     "id": "", "verdict": "killed"}),
                         encoding="utf-8")
        assert store.load_entries() == {}