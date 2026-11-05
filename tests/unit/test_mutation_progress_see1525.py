"""SEE-1525 追加第二批：mutation 运行进度面三件套的对抗钉子。

三项目标（Owner 2026-11-05 直接下令）：
1. 基线快照失败 → 整文件退单 worker 串行时发 stderr warning（不失败）；
2. 每个变异体判定完成向 stderr 出一行人眼进度（并行带 worker 标识），
   串行与并行路径都覆盖，且绝不污染 stdout 的单 JSON 契约；
3. 同一计数点原子更新机器可读进度面（`.qa-cache/gqt-mutation-progress/`），
   供任意会话轮询——per-worker 分片 + 读取端聚合，无并行写竞争。
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit import cli as cli_mod
from godot_qa_toolkit.cli import build_parser
from godot_qa_toolkit.mutation import executor, progress
from godot_qa_toolkit.mutation import runner as runner_mod
from godot_qa_toolkit.mutation.runner import run_mutation

# 6 个变异点（与 test_mutation_warning_see1420 同源 fixture，够切 2 个分片）。
SRC = (
    "func add(a, b):\n"
    "\tif a < b:\n"
    "\t\treturn a + b\n"
    "\treturn b\n"
)
TESTS_GLOB = "res://tests/unit/save/"
_TEST_MUTANTS = 6

_PROGRESS_LINE = re.compile(r"^\[(\d+)/(\d+)\] (\w+) L(\d+) (\S+#\S+#\d+) "
                            r"(\d+\.\d)s( worker=(\S+))?$")


def _write_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "addons" / "gut").mkdir(parents=True)
    (proj / "addons" / "gut" / "gut_cmdln.gd").write_text("# stub")
    save = proj / "tests" / "unit" / "save"
    save.mkdir(parents=True)
    (save / "test_save.gd").write_text('preload("res://calc.gd")\n')
    target = proj / "calc.gd"
    target.write_text(SRC)
    return str(proj), str(target)


class _SerialPool:
    """同进程串行池（并发用例绝不建真进程池——编排环境挂起第一疑因）。"""

    def __init__(self, max_workers=None):
        self.max_workers = max_workers

    def map(self, fn, iterable):
        return [fn(x) for x in iterable]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def killing_gut(monkeypatch):
    """baseline 干净、变异轮一律出新失败 → 全部 killed。"""

    def fake(root, timeout_s=60, tests_glob=None):
        if runner_mod._ACTIVE_ENV is not None:
            return (1, "* test_probe\n\t[Failed]\n")
        return (0, "pass")

    monkeypatch.setattr(runner_mod, "_run_gut_on_project", fake)


@pytest.fixture()
def sealed(monkeypatch, killing_gut):
    """密闭并发路径：串行池 + 假 baseline + 假预热。"""
    monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
    monkeypatch.setattr(executor, "_run_baseline_snapshot",
                        lambda *a, **k: {"rc": 0, "tail": "pass",
                                         "elapsed": 5.0})
    monkeypatch.setattr(executor, "prewarm_import", lambda root: None)


def _progress_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("[")]


# ============================================================================
# 目标 1：基线快照失败 → 退串行的降级 warning（不改变行为、不失败）
# ============================================================================

class TestBaselineFallbackWarning:
    def test_warns_with_file_reason_and_falls_back_wording(
            self, tmp_path, monkeypatch, capsys):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(executor, "_run_baseline_snapshot",
                            lambda *a, **k: None)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None:
                (1, "* test_probe\n\t[Failed]\n")
            if runner_mod._ACTIVE_ENV is not None else (0, "pass"))

        d = executor.run_mutation_files([target], proj, budget=50,
                                        timeout_s=60,
                                        tests_glob=TESTS_GLOB, jobs=2)

        err = capsys.readouterr().err
        assert "baseline snapshot failed" in err
        assert target in err                      # 目标文件
        assert "falls back to serial" in err       # 降级字样
        assert "no baseline snapshot returned" in err  # 失败原因摘要（替身面）
        # 不改变行为：照跑、不失败、整文件退单 worker
        assert d["ok"] is True
        assert d["results"][0]["summary"]["jobs"] == 1
        assert d["results"][0]["summary"]["mutants"] == _TEST_MUTANTS

    def test_reason_carries_real_exception_summary(self, tmp_path, monkeypatch,
                                                   capsys):
        proj, target = _write_project(tmp_path)

        calls = {"n": 0}

        def boom(root, tests_glob=None, timeout_s=60):
            calls["n"] += 1
            if calls["n"] == 1:  # 只有父层预跑失败；worker 自跑照常
                raise OSError("cache root busy")
            return (0, "pass", 1.0)

        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(runner_mod, "_run_baseline", boom)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))

        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)
        err = capsys.readouterr().err
        assert "OSError: cache root busy" in err

    def test_reason_carries_nonzero_rc(self, tmp_path, monkeypatch, capsys):
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(runner_mod, "_run_baseline",
                            lambda root, tests_glob=None, timeout_s=60:
                                (1, "dirty", 1.0))
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))

        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)
        assert "baseline GUT exited rc=1" in capsys.readouterr().err

    def test_no_warning_when_baseline_succeeds(self, tmp_path, sealed, capsys):
        proj, target = _write_project(tmp_path)
        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)
        err = capsys.readouterr().err
        assert "baseline snapshot failed" not in err
        assert "falls back to serial" not in err

    def test_full_suite_glob_never_precomputed_is_not_a_failure(
            self, tmp_path, monkeypatch, capsys):
        """glob=None（全量面不预跑 baseline）不是「快照失败」——不得误报。"""
        proj, target = _write_project(tmp_path)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(executor, "_auto_scan_tests_glob",
                            lambda *a, **k: None)
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None: (0, "pass"))

        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    jobs=2)
        err = capsys.readouterr().err
        assert "baseline snapshot failed" not in err


# ============================================================================
# 目标 2：人眼进度行（stderr；串行 + 并行；stdout 契约零污染）
# ============================================================================

class TestProgressLines:
    def test_serial_path_one_line_per_mutant(self, tmp_path, killing_gut,
                                             capsys):
        proj, target = _write_project(tmp_path)
        r = run_mutation(target, proj, budget=50, tests_glob=TESTS_GLOB)

        captured = capsys.readouterr()
        assert captured.out == ""  # stdout 仍归判定 JSON（函数级不打印）
        lines = _progress_lines(captured.err)
        assert len(lines) == r["summary"]["mutants"] == _TEST_MUTANTS
        first = _PROGRESS_LINE.match(lines[0])
        assert first, lines[0]
        assert (first.group(1), first.group(2), first.group(3)) == ("1", "6",
                                                                    "killed")
        assert first.group(5).endswith("calc.gd#func:add#1")
        assert first.group(8) is None  # 串行路径不带 worker 标识
        assert [int(_PROGRESS_LINE.match(x).group(1)) for x in lines] == \
            list(range(1, _TEST_MUTANTS + 1))

    def test_parallel_path_lines_carry_worker_tag(self, tmp_path, sealed,
                                                  capsys):
        proj, target = _write_project(tmp_path)
        d = executor.run_mutation_files([target], proj, budget=50,
                                        timeout_s=60, tests_glob=TESTS_GLOB,
                                        jobs=2)
        err = capsys.readouterr().err
        lines = _progress_lines(err)
        assert len(lines) == _TEST_MUTANTS
        workers = {_PROGRESS_LINE.match(x).group(8) for x in lines}
        assert workers == {"w0", "w1"}
        # 分母 = 整 run 的执行面（不是单 worker 的切片长度）
        assert {int(_PROGRESS_LINE.match(x).group(2)) for x in lines} == \
            {_TEST_MUTANTS}
        # 每 worker 自己的 done 从 1 起各自计数
        assert sorted(int(_PROGRESS_LINE.match(x).group(1)) for x in lines) == \
            [1, 1, 2, 2, 3, 3]
        assert d["jobs"] == 2

    def test_cli_stdout_stays_exactly_one_json(self, tmp_path, sealed,
                                               capsys):
        proj, target = _write_project(tmp_path)
        args = build_parser().parse_args(
            ["mutation", target, "--project-root", proj])
        cli_mod.cmd_mutation(args)
        captured = capsys.readouterr()
        out = json.loads(captured.out)  # 恰好一个 JSON，进度全在 stderr
        assert set(out) == {"tool", "ok", "summary", "failures"}
        assert _progress_lines(captured.err)


# ============================================================================
# 目标 3：机器可读进度面（字段 / 一致性 / 原子性 / 无共享写者）
# ============================================================================

class TestProgressFile:
    def _run_dir(self, proj):
        root = progress.progress_root(proj)
        dirs = [d for d in root.iterdir() if d.is_dir()]
        assert len(dirs) == 1, dirs
        return dirs[0]

    def test_serial_snapshot_matches_report(self, tmp_path, killing_gut):
        proj, target = _write_project(tmp_path)
        r = run_mutation(target, proj, budget=50, tests_glob=TESTS_GLOB)

        snap = progress.read_progress(proj, target)
        assert snap["done"] == r["summary"]["mutants"] == _TEST_MUTANTS
        assert snap["total"] == _TEST_MUTANTS
        assert snap["killed"] == r["summary"]["killed"]
        assert snap["survived"] == r["summary"]["survived"] == 0
        assert snap["timeout"] == 0 and snap["run_errors"] == 0
        assert snap["elapsed_s"] >= 0.0
        assert snap["updated_at"].endswith("Z")

    def test_parallel_shards_aggregate_to_full_run(self, tmp_path, sealed):
        proj, target = _write_project(tmp_path)
        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)

        run_dir = self._run_dir(proj)
        shards = sorted(run_dir.glob("*.json"))
        assert len(shards) == 2  # 每 worker 一份分片（各写各的路径）
        assert not list(run_dir.glob("*.tmp*"))  # 原子替换后无 tmp 残留
        for shard in shards:
            assert json.loads(shard.read_text(encoding="utf-8"))["done"] >= 0

        snap = progress.read_progress(proj)
        assert snap["done"] == snap["total"] == _TEST_MUTANTS
        assert snap["killed"] == _TEST_MUTANTS
        assert len(snap["workers"]) == 2
        assert sum(w["done"] for w in snap["workers"]) == snap["done"]
        per_file = progress.read_progress(proj, target)
        assert per_file["done"] == _TEST_MUTANTS

    def test_shard_filenames_are_process_private(self, tmp_path, sealed):
        """无共享写者的结构证据：分片名含 worker 槽位 + pid，互不相同。"""
        proj, target = _write_project(tmp_path)
        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)
        run_dir = self._run_dir(proj)
        names = sorted(p.name for p in run_dir.glob("*.json"))
        assert names[0] != names[1]
        workers = {json.loads((run_dir / n).read_text(encoding="utf-8"))["worker"]
                   for n in names}
        assert workers == {"w0", "w1"}

    def test_reader_prefers_newest_run(self, tmp_path, killing_gut):
        proj, target = _write_project(tmp_path)
        run_mutation(target, proj, budget=2, tests_glob=TESTS_GLOB)
        first = progress.read_progress(proj)["run_id"]

        # 伪造一个更旧的 run 目录：读取端必须按 updated_at 取最新，不得串台
        stale = progress.progress_root(proj) / "0000000000001-1"
        stale.mkdir(parents=True)
        (stale / "deadbeef.w0.1.json").write_text(json.dumps({
            "schema": progress.SCHEMA, "run_id": "0000000000001-1",
            "file": "calc.gd", "worker": "w0", "pid": 1, "total": 99,
            "done": 99, "updated_at_ts": 1.0,
            "updated_at": "2000-01-01T00:00:00Z"}), encoding="utf-8")
        assert progress.read_progress(proj)["run_id"] == first

        run_mutation(target, proj, budget=2, tests_glob=TESTS_GLOB,
                     mutate_all=True)
        assert progress.read_progress(proj)["run_id"] != first

    def test_write_failure_degrades_without_breaking_run(
            self, tmp_path, killing_gut, capsys):
        proj, target = _write_project(tmp_path)
        (Path(proj) / ".qa-cache").write_text("occupied")

        r = run_mutation(target, proj, budget=50, tests_glob=TESTS_GLOB)

        assert r["summary"]["killed"] == _TEST_MUTANTS  # 判定零影响
        err = capsys.readouterr().err
        assert err.count("mutation progress:") == 1
        assert progress.read_progress(proj) is None

    def test_dry_run_writes_nothing(self, tmp_path, killing_gut):
        proj, target = _write_project(tmp_path)
        run_mutation(target, proj, budget=50, tests_glob=TESTS_GLOB,
                     dry_run=True)
        assert progress.read_progress(proj) is None

    def test_invalid_precheck_does_not_strand_done_below_total(
            self, tmp_path, monkeypatch):
        """静态 invalid 位点计入分母 → 每个 worker 的 done 都能走到 n/n。

        回归反例：分母用 len(retry_ids)（含 invalid）而 done 只数真正进 GUT
        的位点 → 并行 run 永远停在 done < total，轮询方无法分辨「跑完了」
        与「卡住了」。
        """
        from godot_qa_toolkit.mutation import reporting as _rep

        proj, target = _write_project(tmp_path)
        orig_split = _rep._split_invalid_mutants

        def split_with_one_invalid(src_text, mutants):
            valid, invalid = orig_split(src_text, mutants)
            victim = next((m for m in valid if m.stable_id.endswith("#1")),
                          None)
            if victim is None:
                return valid, invalid
            return ([m for m in valid if m is not victim], invalid + [victim])

        monkeypatch.setattr(_rep, "_split_invalid_mutants",
                            split_with_one_invalid)
        monkeypatch.setattr(executor, "_worker_pool", _SerialPool)
        monkeypatch.setattr(executor, "_run_baseline_snapshot",
                            lambda *a, **k: {"rc": 0, "tail": "pass",
                                             "elapsed": 5.0})
        monkeypatch.setattr(executor, "prewarm_import", lambda root: None)
        monkeypatch.setattr(
            runner_mod, "_run_gut_on_project",
            lambda root, timeout_s=60, tests_glob=None:
                (1, "* test_probe\n\t[Failed]\n")
            if runner_mod._ACTIVE_ENV is not None else (0, "pass"))

        executor.run_mutation_files([target], proj, budget=50, timeout_s=60,
                                    tests_glob=TESTS_GLOB, jobs=2)

        snap = progress.read_progress(proj)
        assert snap["invalid_mutants"] == 1
        assert snap["done"] == snap["total"], snap
        assert snap["done"] == sum(snap[k] for k in (
            "killed", "survived", "timeout", "run_errors", "suspect",
            "invalid_mutants"))

    def test_corrupt_shard_is_treated_as_absent(self, tmp_path, killing_gut):
        """原子写被外力打断留下半截文件 → 聚合按缺该分片处理，不得抛。"""
        proj, target = _write_project(tmp_path)
        run_mutation(target, proj, budget=2, tests_glob=TESTS_GLOB)
        good = progress.read_progress(proj)
        run_dir = next(d for d in progress.progress_root(proj).iterdir()
                       if d.is_dir())
        (run_dir / "deadbeef.w9.1.json").write_text('{"done": 3',
                                                    encoding="utf-8")
        assert progress.read_progress(proj) == good

    def test_cleanup_failure_after_failed_replace_cannot_crash_run(
            self, tmp_path, monkeypatch, killing_gut):
        """replace 失败 + 清理也失败（Windows 杀软持 tmp）→ 只告警，不冒泡。"""
        proj, target = _write_project(tmp_path)
        real_replace = progress.os.replace
        real_unlink = Path.unlink

        def bad_replace(src, dst):
            raise OSError("reader holds destination")

        def bad_unlink(self, *a, **k):
            if ".tmp" in self.name:  # 只打断进度分片的 tmp 清理
                raise PermissionError("tmp held by indexer")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(progress.os, "replace", bad_replace)
        monkeypatch.setattr(progress.Path, "unlink", bad_unlink, raising=False)
        try:
            r = run_mutation(target, proj, budget=50, tests_glob=TESTS_GLOB)
        finally:
            monkeypatch.setattr(progress.os, "replace", real_replace)

        assert r["summary"]["mutants"] == _TEST_MUTANTS  # 判定零影响
