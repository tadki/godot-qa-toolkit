"""§SPEC-1420-05：resume v2 store 单测（per-file manifest 索引层 + per-mutant
条目保留；原子写；毒条目拒收；信封/全签名校验失败告警拒复用；v1 不读不写
不删、显式披露）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import runner as mrunner
from godot_qa_toolkit.mutation.ops import OPS_VERSION, _collect_mutations
from godot_qa_toolkit.mutation.resume2 import (
    RESUME_V1_DIRNAME,
    RESUME_V2_DIRNAME,
    SCHEMA_V2,
    ResumeStoreV2,
    file_key,
)

TARGET_SRC = ("func add(a, b):\n"
              "\treturn a + b\n")


def _write_project(tmp_path, src=TARGET_SRC):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    target = proj / "calc.gd"
    target.write_text(src)
    return proj, target


def _entry(i=1, verdict="killed"):
    return {"id": f"calc.gd#add#{i}", "scope_id": "add", "scope_hash": "S",
            "line": 2, "column": 11, "kind": "AOR", "original": "+",
            "mutated": "-", "verdict": verdict, "reason": "r",
            "timeout_streak": 0}


class TestStoreLayout:
    def test_store_dir_shape(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert RESUME_V2_DIRNAME in str(store.store_dir)
        assert store.manifest_path.name == "manifest.json"
        assert store.entry_path("calc.gd#add#1").name.endswith(".json")

    def test_file_key_excludes_budget_and_negative_list(self, tmp_path):
        """D6：budget 不进 v2 file-key（预算是执行上限不是身份）；D6 负面
        清单：--mutation-warning / --timeout-factor 无从进入 key 签名。"""
        proj, target = _write_project(tmp_path)
        k1 = file_key(str(proj), str(target), "res://tests/", "linux")
        k2 = file_key(str(proj), str(target), "res://tests/", "linux")
        assert k1 == k2
        # glob / ops / binary 是身份成分
        assert k1 != file_key(str(proj), str(target), "res://tests/x/", "linux")
        assert k1 != file_key(str(proj), str(target), "res://tests/", "win64")

    def test_manifest_roundtrip_and_schema_echo(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert store.write_manifest("H1", {"add": "S1"}, "SHA", "calc.gd")
        doc = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        assert doc["schema"] == SCHEMA_V2
        assert doc["ops_version"] == OPS_VERSION
        assert doc["header_hash"] == "H1"
        assert doc["scopes"] == {"add": "S1"}
        assert store.load_manifest()["header_hash"] == "H1"

    def test_entry_roundtrip_and_load(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert store.record_entry(_entry()) is True
        entries = store.load_entries()
        assert list(entries) == ["calc.gd#add#1"]
        assert entries["calc.gd#add#1"]["verdict"] == "killed"

    def test_corrupt_manifest_degrades_to_none(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        store.manifest_path.write_text("{broken", encoding="utf-8")
        assert store.load_manifest() is None

    def test_wrong_schema_manifest_degrades(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        store.manifest_path.write_text(
            json.dumps({"schema": "see1400-resume/1", "file_key": store.key}),
            encoding="utf-8")
        assert store.load_manifest() is None


class TestPoisonEntries:
    def test_launch_failure_entry_rejected_on_record(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        poison = _entry(verdict="run_error")
        poison["reason"] = "memory injection failed: env missing"
        assert store.record_entry(poison) is False
        assert store.load_entries() == {}

    def test_launch_failure_entry_rejected_on_load(self, tmp_path):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        # 模拟修复前断连期落盘的存量毒条目
        p = store.entry_path("calc.gd#add#1")
        p.write_text(json.dumps({"schema": SCHEMA_V2, "file_key": store.key,
                                 **_entry(verdict="run_error"),
                                 "reason": "godot launch failed (tests did "
                                           "not run): x"}),
                     encoding="utf-8")
        assert store.load_entries() == {}

    def test_tampered_envelope_rejected_on_load(self, tmp_path):
        """信封漂移（file_key 不符）= 条目被拷贝/换位——按无数据处理。"""
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        store.ensure_store_dir()
        p = store.entry_path("calc.gd#add#1")
        p.write_text(json.dumps({"schema": SCHEMA_V2, "file_key": "forged",
                                 **_entry()}), encoding="utf-8")
        assert store.load_entries() == {}


class TestV1Supersession:
    def test_v1_dir_disclosed_never_touched(self, tmp_path, capsys):
        """v1 目录在场：stderr 一次性显式披露；不读不写不删。"""
        proj, target = _write_project(tmp_path)
        v1 = proj / ".qa-cache" / RESUME_V1_DIRNAME / "somekey"
        v1.mkdir(parents=True)
        (v1 / "0.json").write_text("{}", encoding="utf-8")
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert store.disclose_v1_superseded() is True
        err = capsys.readouterr().err
        assert "v1" in err and "neither read, written, nor deleted" in err
        # v1 内容原样保留（不删）
        assert (v1 / "0.json").exists()

    def test_no_v1_dir_no_disclosure(self, tmp_path, capsys):
        proj, target = _write_project(tmp_path)
        store = ResumeStoreV2(str(proj), str(target), "res://tests/")
        assert store.disclose_v1_superseded() is False
        assert "v1" not in capsys.readouterr().err

    def test_first_v2_run_reruns_all_with_v1_present(self, tmp_path,
                                                     monkeypatch, capsys):
        """一次性失效窗口端到端：v1 有历史 + v2 无 manifest → 全量重跑。"""
        proj, target = _write_project(tmp_path)
        v1 = proj / ".qa-cache" / RESUME_V1_DIRNAME / "k"
        v1.mkdir(parents=True)
        (v1 / "0.json").write_text("{}", encoding="utf-8")
        calls = {"n": 0}

        def fake(root, timeout_s=60, tests_glob=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return (0, "baseline ok")
            return (1, "* test_x\n\t\ttest_x [Failed]")

        monkeypatch.setattr(mrunner, "_run_gut_on_project", fake)
        r = mrunner.run_mutation(str(target), str(proj), budget=10,
                                 tests_glob="res://tests/")
        assert r["summary"]["killed"] == 3
        assert calls["n"] == 4  # baseline + 3（v1 历史未迁移，不脏合并）
        assert "resume v1 store found" in capsys.readouterr().err
