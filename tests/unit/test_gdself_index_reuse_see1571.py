"""SEE-1571 §SPEC-503 + SEE-1581：选面层的进程内复用与零持久化。

SEE-1581 起 mutation 选面改运行时真值映射（gdself 静态选面退役），本文件
的复用/零持久化断言相应迁移到新选面层：
- 多文件规划复用同一份进程内**映射解析缓存**（recipe hash 记忆化）；
- 唯一允许的落盘是确定性选择目录 `.qa-cache/gqt-attr-select/`；
- 旧的 gdself 复用/等价用例保留为静态模块自测（模块未删，主路径不用）。
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
    def test_mapping_resolution_cached_for_multifile_planning(
            self, tmp_path, monkeypatch):
        """多文件规划：映射新鲜判据按 project_root 记忆化（只算一次）。"""
        from attribution_fixtures import write_map
        from godot_qa_toolkit.mutation import truth_select
        proj, paths = _write_multi_target_project(tmp_path)
        write_map(Path(proj), {"calc.gd": ["test_save.gd::test_ok"],
                               "extra.gd": ["test_misc.gd::test_ok"]})
        truth_select._fresh_recipe_hash.cache_clear()
        d = executor.run_mutation_files(paths, proj, budget=50,
                                        dry_run=True, jobs=None)
        assert d["ok"] is True
        info = truth_select._fresh_recipe_hash.cache_info()
        assert info.misses == 1, (
            f"多文件规划必须复用同一份进程内 recipe 判据缓存: {info}")
        assert info.hits >= 1

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
        """零非预期落盘：唯一写入是确定性选择目录（映射解析不落盘）。"""
        from attribution_fixtures import write_map
        proj, paths = _write_multi_target_project(tmp_path)
        write_map(Path(proj), {"calc.gd": ["test_save.gd::test_ok"],
                               "extra.gd": ["test_misc.gd::test_ok"]})
        executor.run_mutation_files(paths, proj, budget=50,
                                    dry_run=True, jobs=None)
        cache_dir = Path(proj) / ".qa-cache"
        entries = sorted(str(p.relative_to(cache_dir))
                         for p in cache_dir.rglob("*")) if cache_dir.exists() \
            else []
        assert entries, "新鲜映射应物化确定性选择目录"
        assert all(e == "gqt-attr-select" or
                   e.startswith("gqt-attr-select/") or
                   e == "attribution-map.json" for e in entries), (
            f"非预期落盘面: {entries}")

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
