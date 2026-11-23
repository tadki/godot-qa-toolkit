"""SEE-1627 §SPEC-002 / §SPEC-007：worker 腿选面递归语义与冷通道逐字对齐。

根因：`WorkerSession.run_leg` 曾以 `tests_glob or "res://tests/"` 把 full
审计面（`tests_glob=None`）折叠成显式目录，宿主 `add_directory` 不递归
子目录（GUT 9.6）→ 标准布局（`tests/unit/` 等）收集 0 测试。修复后请求
必须携带递归意图：full 面（None）递归收集（= 冷通道 `-ginclude_subdirs`），
`.gd` 形态走单脚本精确执行；显式目录面复刻冷通道 gut_cmdln 读取的项目
`.gutconfig.json#include_subdirs`（§SPEC-007：true 递归、false/缺失非递归）。

行为契约：
1. full 面请求递归标记为真，且与「显式 `res://tests/`」目录形态可区分
   （`or "res://tests/"` 折叠 bug 的直接回归钉）。
2. 显式目录递归标记跟随项目 `.gutconfig include_subdirs`（§SPEC-007）；
   无配置/false 时非递归（§SPEC-002 语义保持）。
3. `.gd` 形态请求递归标记恒为假、携带精确脚本路径（单脚本执行不回归）。
4. 目录类选面的 worker 递归标记 == 冷通道有效递归（full 恒真；显式目录面
   == `.gutconfig include_subdirs`）。
"""

import json
import uuid

from godot_qa_toolkit.mutation import worker_session as ws


def _captured_request(tmp_path, monkeypatch, tests_glob, project_root=None):
    slot = tmp_path / f"slot-{uuid.uuid4().hex}"
    (slot / ws.WORKER_DIRNAME).mkdir(parents=True)
    sess = ws.WorkerSession(str(slot), str(project_root or tmp_path), "linux")
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


def _project_with_gutconfig(tmp_path, data):
    """建项目根并按需写 `.gutconfig.json`；data=None 表示缺失。"""
    proj = tmp_path / f"proj-{uuid.uuid4().hex}"
    proj.mkdir(parents=True)
    if data is not None:
        (proj / ".gutconfig.json").write_text(
            json.dumps(data), encoding="utf-8")
    return proj


def _expected_dir_recurse(tests_glob, cfg_value):
    """独立 oracle（不调用产品函数，避免对实现近似自证）。

    由已知写入的配置状态直接推导期望：full 面 CLI 旗标 `-ginclude_subdirs`
    恒递归（覆盖项目配置）；`.gd` 恒单脚本精确；显式目录面跟随项目
    `.gutconfig include_subdirs`——仅 True 递归，false/缺失/非法/无键/非对象
    一律非递归（GUT 缺省 false）。
    """
    if tests_glob is None:
        return True
    if tests_glob.endswith(".gd"):
        return False
    return cfg_value is True


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

    def test_empty_glob_collapses_to_root_and_stays_non_recursive(
            self, tmp_path, monkeypatch):
        """空 glob 与冷通道一致：折叠为根目录且不递归。

        回归钉：递归决策必须由 `tests_glob is None`（full 面）决定，而非
        真值判断——空串是显式目录面（冷通道 `_gut_extra_args("")` 折叠为
        `-gdir=res://tests/` 且不递归），不得被误判为 full 面递归。
        """
        req = _captured_request(tmp_path, monkeypatch, "")
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/"

    def test_explicit_directory_with_trailing_slash_stays_non_recursive(
            self, tmp_path, monkeypatch):
        """带尾斜杠的显式目录仍是显式面：不因尾斜杠被误判为递归。"""
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit/")
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/unit/"

    def test_gd_form_is_single_script_non_recursive(self, tmp_path, monkeypatch):
        req = _captured_request(
            tmp_path, monkeypatch, "res://tests/unit/test_calc.gd")
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/unit/test_calc.gd"


class TestWorkerColdAlignment:
    def test_directory_faces_match_cold_recursion_condition(
            self, tmp_path, monkeypatch):
        """目录类选面：worker 递归标记 == 独立 oracle 的冷通道语义
        （无项目配置时，full 恒递归、显式目录面均非递归）。"""
        for glob in (None, "", "res://tests/", "res://tests/unit",
                     "res://tests/unit/"):
            req = _captured_request(tmp_path, monkeypatch, glob)
            assert req["tests_recurse"] == _expected_dir_recurse(glob, None), glob


class TestGutconfigRecursionAlignment:
    """§SPEC-007：显式目录面递归跟随项目 `.gutconfig include_subdirs`。"""

    def test_explicit_dir_recurses_when_project_config_true(
            self, tmp_path, monkeypatch):
        proj = _project_with_gutconfig(tmp_path, {"include_subdirs": True})
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is True

    def test_explicit_dir_non_recursive_when_config_false(
            self, tmp_path, monkeypatch):
        proj = _project_with_gutconfig(tmp_path, {"include_subdirs": False})
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is False

    def test_explicit_dir_non_recursive_when_config_missing(
            self, tmp_path, monkeypatch):
        proj = _project_with_gutconfig(tmp_path, None)
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is False

    def test_full_face_recursive_even_when_config_false(
            self, tmp_path, monkeypatch):
        """full 面 CLI 旗标 `-ginclude_subdirs` 覆盖项目配置，恒递归。"""
        proj = _project_with_gutconfig(tmp_path, {"include_subdirs": False})
        req = _captured_request(tmp_path, monkeypatch, None, project_root=proj)
        assert req["tests_recurse"] is True

    def test_gd_form_exact_single_script_regardless_of_config(
            self, tmp_path, monkeypatch):
        proj = _project_with_gutconfig(tmp_path, {"include_subdirs": True})
        req = _captured_request(tmp_path, monkeypatch,
                                "res://tests/unit/test_calc.gd",
                                project_root=proj)
        assert req["tests_recurse"] is False
        assert req["tests_glob"] == "res://tests/unit/test_calc.gd"

    def test_malformed_config_falls_back_to_non_recursive(
            self, tmp_path, monkeypatch):
        proj = _project_with_gutconfig(tmp_path, None)
        (proj / ".gutconfig.json").write_text("{not json", encoding="utf-8")
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is False

    def test_config_dict_without_key_falls_back_to_non_recursive(
            self, tmp_path, monkeypatch):
        """合法字典但无 `include_subdirs` 键：GUT 缺省 false（非递归）。

        回归钉：读取不得用下标（无键会 KeyError），默认值必须为 false。
        """
        proj = _project_with_gutconfig(tmp_path, {"other_setting": True})
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is False

    def test_non_dict_json_falls_back_to_non_recursive(
            self, tmp_path, monkeypatch):
        """合法 JSON 但非对象（数组）：回落非递归且不崩溃（GUT 缺省 false）。"""
        proj = _project_with_gutconfig(tmp_path, [])
        req = _captured_request(tmp_path, monkeypatch, "res://tests/unit",
                                project_root=proj)
        assert req["tests_recurse"] is False

    def test_worker_matches_independent_recurse_across_config_states(
            self, tmp_path, monkeypatch):
        """三态（true/false/缺失）× 目录类选面 + `.gd`：worker 递归标记
        == 独立 oracle 推导值（oracle 不调用产品函数，消除近似自证）。"""
        for cfg in (True, False, None):
            proj = _project_with_gutconfig(
                tmp_path, None if cfg is None else {"include_subdirs": cfg})
            for glob in (None, "", "res://tests/", "res://tests/unit",
                         "res://tests/unit/", "res://tests/unit/x.gd"):
                req = _captured_request(tmp_path, monkeypatch, glob,
                                        project_root=proj)
                assert req["tests_recurse"] == _expected_dir_recurse(
                    glob, cfg), (cfg, glob)
