"""SEE-1571 §SPEC-503：E6-4 gdself 索引进程内复用转正。

钉死行为（蓝本 = SEE-1568 批次二原型语义）：
- 规划相位只读前提下进程内复用（多文件规划只建一次索引）；
- 进程级零持久化（无落盘缓存面）；
- 多文件规划结果与逐文件构建逐位一致（glob/计划输出等价）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation import gdself, executor
from godot_qa_toolkit.mutation.testscan import (_INDEX_CACHE,
                                                derive_tests_glob)

GD_TARGET = "func add(a, b):\n\treturn a + b\n"


@pytest.fixture(autouse=True)
def _fresh_index_cache():
    """测试间清空进程内缓存（隔离：计数断言与跨测试树污染双防）。"""
    _INDEX_CACHE.clear()
    yield
    _INDEX_CACHE.clear()


def _write_multi_target_project(tmp_path: Path) -> tuple[str, list[str]]:
    proj = tmp_path / "proj"
    (proj / "tests" / "unit" / "save").mkdir(parents=True)
    (proj / "tests" / "unit" / "save" / "test_save.gd").write_text(
        'preload("res://calc.gd")\n')
    (proj / "tests" / "unit" / "misc").mkdir(parents=True)
    (proj / "tests" / "unit" / "misc" / "test_misc.gd").write_text(
        'preload("res://extra.gd")\n')
    paths = []
    for name in ("calc.gd", "extra.gd"):
        p = proj / name
        p.write_text(GD_TARGET)
        paths.append(str(p))
    return str(proj), paths


class TestInProcessReuse:
    def test_build_index_called_once_for_multifile_planning(
            self, tmp_path, monkeypatch):
        """多文件规划：索引只构建一次（其余查询复用进程内缓存）。"""
        proj, paths = _write_multi_target_project(tmp_path)
        calls = {"n": 0}
        orig = gdself.build_index

        def counting_build(root: str) -> dict:
            calls["n"] += 1
            return orig(root)

        monkeypatch.setattr(gdself, "build_index", counting_build)
        d = executor.run_mutation_files(paths, proj, budget=50,
                                        dry_run=True, jobs=None)
        assert d["ok"] is True
        assert calls["n"] == 1, "多文件规划必须复用同一份进程内索引"
        assert len(_INDEX_CACHE) == 1

    def test_reused_results_identical_to_fresh_build(self, tmp_path):
        """复用查询与逐文件新建的规划结果逐位一致（glob 等价）。"""
        proj, paths = _write_multi_target_project(tmp_path)
        tests_dir = Path(proj) / "tests"
        globs_fresh = [derive_tests_glob(
            "res://" + Path(p).name, tests_dir, project_root=proj)
            for p in paths]
        _INDEX_CACHE.clear()
        globs_reused = [derive_tests_glob(
            "res://" + Path(p).name, tests_dir, project_root=proj)
            for p in paths]
        assert globs_fresh == globs_reused
        assert all(g is not None for g in globs_reused)
        assert globs_reused[0] == "res://tests/unit/save/test_save.gd"
        assert globs_reused[1] == "res://tests/unit/misc/test_misc.gd"

    def test_cache_hit_identical_to_cache_miss(self, tmp_path):
        """同目标先冷建后复用：命中与未命中查询输出逐位一致。"""
        proj, paths = _write_multi_target_project(tmp_path)
        tests_dir = Path(proj) / "tests"
        first = derive_tests_glob("res://calc.gd", tests_dir, project_root=proj)
        second = derive_tests_glob("res://calc.gd", tests_dir,
                                   project_root=proj)
        assert first == second == "res://tests/unit/save/test_save.gd"

    def test_zero_persistence_no_disk_cache(self, tmp_path):
        """进程级零持久化：复用不产生任何落盘缓存面。"""
        proj, paths = _write_multi_target_project(tmp_path)
        executor.run_mutation_files(paths, proj, budget=50,
                                    dry_run=True, jobs=None)
        cache_dir = Path(proj) / ".qa-cache"
        assert not cache_dir.exists() or list(cache_dir.rglob("*")) == []
        assert len(_INDEX_CACHE) == 1, "复用面只在进程内（内存缓存）"

    def test_distinct_project_roots_get_distinct_entries(
            self, tmp_path):
        """不同 project_root 各自建索引（键隔离，不串树）。"""
        proj1, paths1 = _write_multi_target_project(tmp_path / "a")
        proj2, paths2 = _write_multi_target_project(tmp_path / "b")
        tests_dir1 = Path(proj1) / "tests"
        tests_dir2 = Path(proj2) / "tests"
        g1 = derive_tests_glob("res://calc.gd", tests_dir1, project_root=proj1)
        g2 = derive_tests_glob("res://calc.gd", tests_dir2, project_root=proj2)
        assert g1 == g2 == "res://tests/unit/save/test_save.gd"
        assert len(_INDEX_CACHE) == 2
