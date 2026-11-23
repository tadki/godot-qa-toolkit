"""SEE-1627 §SPEC-002：worker 腿选面递归语义与冷通道逐字对齐。

根因：`WorkerSession.run_leg` 曾以 `tests_glob or "res://tests/"` 把 full
审计面（`tests_glob=None`）折叠成显式目录，宿主 `add_directory` 不递归
子目录（GUT 9.6）→ 标准布局（`tests/unit/` 等）收集 0 测试。修复后请求
必须携带递归意图：full 面（None）递归收集（= 冷通道 `-ginclude_subdirs`），
显式目录 `--tests` 保持非递归，`.gd` 形态走单脚本精确执行。

行为契约：
1. full 面请求递归标记为真，且与「显式 `res://tests/`」目录形态可区分
   （`or "res://tests/"` 折叠 bug 的直接回归钉）。
2. 显式目录请求递归标记为假（非递归语义不回归）。
3. `.gd` 形态请求递归标记为假、携带精确脚本路径（单脚本执行不回归）。
4. 目录类选面的 worker 递归标记 == 冷通道是否附加 `-ginclude_subdirs`。
"""

import json
import uuid

from godot_qa_toolkit.mutation import worker_session as ws
from godot_qa_toolkit.mutation.runner import _gut_extra_args


def _captured_request(tmp_path, monkeypatch, tests_glob):
    slot = tmp_path / f"slot-{uuid.uuid4().hex}"
    (slot / ws.WORKER_DIRNAME).mkdir(parents=True)
    sess = ws.WorkerSession(str(slot), str(tmp_path), "linux")
    monkeypatch.setattr(sess, "start", lambda: None)
    sess._log_path = tmp_path / "worker-log.txt"
    captured = {}

    def fake_await(resp_path, leg):
        captured["req"] = json.loads(
            (slot / ws.WORKER_DIRNAME / "req.json").read_text(
                encoding="utf-8"))
        return {"leg": leg, "hash_ok": True, "gut_ran": True, "fail": 0,
                "pass": 1, "reload": {}}

    monkeypatch.setattr(sess, "_await_resp", fake_await)
    sess.run_leg(0, "res://x.gd", "sha", [], tests_glob)
    return captured["req"]


class TestWorkerRecursionFlag:
    def test_full_face_requests_recursive_collection(self, tmp_path, monkeypatch):
        req = _captured_request(tmp_path, monkeypatch, None)
        assert req["tests_recurse"] is True
        assert req["tests_glob"] == "res://tests/"

    def test_full_face_distinct_from_explicit_root_dir(self, tmp_path, monkeypatch):
        """full 面与显式 `res://tests/` 目录必须可区分——前者递归、后者不递归。"""
        full = _captured_request(tmp_path, monkeypatch, None)
        explicit = _captured_request(tmp_path, monkeypatch, "res://tests/")
        assert full["tests_recurse"] is True
        assert explicit["tests_recurse"] is False

    def test_explicit_directory_stays_non_recursive(self, tmp_path, monkeypatch):
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit")
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/unit"

    def test_gd_form_is_single_script_non_recursive(self, tmp_path, monkeypatch):
        req = _captured_request(
            tmp_path, monkeypatch, "res://tests/unit/test_calc.gd")
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/unit/test_calc.gd"


class TestWorkerColdAlignment:
    def test_directory_faces_match_cold_recursion_condition(
            self, tmp_path, monkeypatch):
        """目录类选面：worker 递归标记 == 冷通道附加 `-ginclude_subdirs`。"""
        for glob in (None, "res://tests/", "res://tests/unit"):
            req = _captured_request(tmp_path, monkeypatch, glob)
            cold_recurses = "-ginclude_subdirs" in _gut_extra_args(glob)
            assert req["tests_recurse"] == cold_recurses, glob
