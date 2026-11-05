"""SEE-1525 mutation 运行进度面（方案 A stderr 进度行 + 方案 B 机器可读进度）。

两条输出面共用**同一计数点**（每个变异体判定完成的瞬间，见
runner._run_mutant_loop），串行与并行路径都经过它；两者都只写 stderr /
`.qa-cache`，**绝不触碰 stdout**（contract §3：stdout 恰好一个 JSON）。

方案 B 的竞态规避（per-worker 分片 + 读取端聚合）：

- 每个执行进程只写**自己那个分片文件**——文件名含 file-key、worker 槽位与
  pid，任意两个写者永远不打开同一路径（同分片只有一个线程写，变异体循环
  本身串行），因此不存在「读-改-写」窗口，也没有跨进程共享写者。
- 分片落盘 = 同目录 tmp 文件 + `os.replace`（同卷 rename 原子语义）：读者
  要么看到旧的完整 JSON、要么看到新的完整 JSON，永不见半截文件。
- 聚合只发生在读侧（`read_progress`）：按 file 分组合计 done 与 verdict
  计数、取 `max(total)`（total 是整 run 的分母，各分片同值）、`max(elapsed)`、
  `max(updated_at)`。

结论：无共享写者、无文件锁、无读-改-写，故无并行写竞争；写失败（只读盘 /
`.qa-cache` 被占 / Windows 上读者持有目标文件）一次告警后放弃进度面，
绝不影响判定主流程。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

PROGRESS_DIRNAME = "gqt-mutation-progress"
SCHEMA = "see1525-progress/1"

# 旧 run 目录保留窗口（进度面是易失观测面，无跨 run 语义）。
_RETENTION_S = 24 * 3600

# verdict → 分片字段名（进度面字段集：done/total/killed/survived/timeout/
# run_errors/suspect/invalid_mutants + elapsed_s/updated_at）。
_VERDICT_FIELD = {
    "killed": "killed",
    "survived": "survived",
    "timeout": "timeout",
    "run_error": "run_errors",
    "suspect": "suspect",
    "invalid_mutant": "invalid_mutants",
}
_COUNT_KEYS = tuple(_VERDICT_FIELD.values())

# 每进程每项目根只清理一次过期 run 目录。
_pruned_roots: set[str] = set()


def new_run_id() -> str:
    """可排序的 run 标识（毫秒时间戳 + pid，跨进程唯一）。"""
    return f"{int(time.time() * 1000):013d}-{os.getpid()}"


def progress_root(project_root: str) -> Path:
    """``<project>/.qa-cache/gqt-mutation-progress/``（自含 .gitignore）。"""
    return (Path(str(Path(project_root).resolve())) / ".qa-cache"
            / PROGRESS_DIRNAME)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _file_slug(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


def _ensure_root(project_root: str) -> Path:
    """建根目录 + 自含 ignore（尽力而为，失败由调用方降级）。"""
    root = progress_root(project_root)
    root.mkdir(parents=True, exist_ok=True)
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        # 与 resume v2 / timing store 同款：缓存目录对目标项目 git 不可见。
        gitignore.write_text("*\n", encoding="utf-8")
    return root


def _prune_once(project_root: str) -> None:
    """清理过期 run 目录（每进程每 root 一次；失败即放弃，绝不影响主流程）。"""
    if project_root in _pruned_roots:
        return
    _pruned_roots.add(project_root)
    cutoff = time.time() - _RETENTION_S
    try:
        for entry in progress_root(project_root).iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except OSError:
        return


class ProgressReporter:
    """单进程内单文件（或单分片）的进度写者。

    total 为**整 run 的分母**（并行分片由父层注入全局执行面；串行为本 worker
    自己的执行面）。worker 标识恒在的路径：并行分片 `worker=wN`、基线降级
    的串行分片 `worker=serial`；`--jobs 1` / 单点寻址的自建 reporter 不带。
    """

    def __init__(self, project_root: str, file_path: str,
                 total: int | None = None, run_id: str | None = None,
                 worker: str | None = None, resolved_before: int = 0):
        from ..timing import project_relative

        self.project_root = str(Path(project_root).resolve())
        self.rel = project_relative(self.project_root, file_path)
        self.run_id = run_id or new_run_id()
        self.worker = worker
        self.pid = os.getpid()
        self.total = int(total) if total is not None else 0
        # 已判定但从未进 GUT 的位点（静态 invalid 预检剔除）：分母含它们，
        # done 也从它们起算，每个 worker 才不会永远停在 [n-1/n]。同步记入
        # invalid_mutants 计数，保证恒等式 done == 各 verdict 计数之和。
        self.done = int(resolved_before)
        self.counts = {key: 0 for key in _COUNT_KEYS}
        self.counts["invalid_mutants"] = int(resolved_before)
        self.started_at = _now_iso()
        self.t0 = time.monotonic()
        self._shard = self._shard_path()
        self._warned = False
        _prune_once(self.project_root)
        # 建 run 即落一条零进展分片：轮询方立刻看到「新 run 已开始」，
        # 不会把上一 run 的终点数字误当本轮进度。
        self._write()

    # -- 写侧 ------------------------------------------------------------

    def record(self, mutant, verdict: dict, elapsed_s: float) -> None:
        """一个变异体判定完成：先出人眼进度行，再原子更新本进程分片。"""
        label = str(verdict.get("verdict") or "unknown")
        self.done += 1
        field = _VERDICT_FIELD.get(label)
        if field is not None:
            self.counts[field] += 1
        self._emit_line(mutant, label, elapsed_s)
        self._write()

    def _emit_line(self, mutant, label: str, elapsed_s: float) -> None:
        line = (f"[{self.done}/{max(self.total, self.done)}] {label} "
                f"L{getattr(mutant, 'line', 0)} "
                f"{getattr(mutant, 'stable_id', '?')} {elapsed_s:.1f}s")
        if self.worker:
            line += f" worker={self.worker}"
        print(line, file=sys.stderr, flush=True)

    def _shard_path(self) -> Path:
        name = f"{_file_slug(self.rel)}.{self.worker or 'main'}.{self.pid}.json"
        return progress_root(self.project_root) / self.run_id / name

    def payload(self) -> dict:
        return {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "file": self.rel,
            "worker": self.worker,
            "pid": self.pid,
            "total": self.total,
            "done": self.done,
            **self.counts,
            "elapsed_s": round(time.monotonic() - self.t0, 3),
            "started_at": self.started_at,
            "updated_at": _now_iso(),
            "updated_at_ts": time.time(),
        }

    def _write(self) -> None:
        """tmp + os.replace 原子替换本进程分片（非共享路径，无写竞争）。"""
        path = self._shard
        tmp = path.with_name(f"{path.name}.tmp{self.pid}")
        try:
            _ensure_root(self.project_root)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.payload(), ensure_ascii=False,
                                      indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            # 清理本身也可能抛（Windows 上杀软/索引器持有刚建的 tmp 文件：
            # PermissionError 是 OSError 子类，missing_ok 只兜 FileNotFound）——
            # 进度面绝不允许把异常带回变异体循环（那会让整 run 无 JSON 崩掉）。
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            if not self._warned:
                self._warned = True
                print(f"mutation progress: shard write failed for {path}: "
                      f"{e} — continuing without machine-readable progress",
                      file=sys.stderr)


def make_reporter(project_root: str, file_path: str, progress: dict | None,
                  fallback_total: int | None = None,
                  invalid_count: int = 0) -> ProgressReporter:
    """按父层注入的 progress 上下文建 reporter（缺省自建 run_id/串行槽位）。

    invalid_count：本 worker 静态 invalid 预检剔除的位点数。父层注入全局
    分母（progress["total"] 在场，含这些位点）时 done 从它们起算，逐 worker
    才不会永远差一截；串行路径的分母自身已扣掉 invalid，故不计。

    建 reporter 失败（只读盘 / `.qa-cache` 被文件占位）→ None：进度是观测
    增强面，绝不能因其建不起来而中断判定主流程。
    """
    ctx = progress or {}
    worker_total = ctx.get("total")
    try:
        return ProgressReporter(
            project_root, file_path,
            total=worker_total if worker_total is not None else fallback_total,
            run_id=ctx.get("run_id"), worker=ctx.get("worker"),
            resolved_before=invalid_count if worker_total is not None else 0)
    except OSError as e:
        print(f"mutation progress: disabled for {file_path}: {e} — continuing",
              file=sys.stderr)
        return None


# -- 读侧：任意会话轮询入口 ------------------------------------------------


def _load_shard(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 半截/损坏/被删 = 无该分片（聚合按在场分片算）
    return data if isinstance(data, dict) else None


def _newest_run(root: Path) -> tuple[str | None, list[dict]]:
    """取 updated_at 最新的 run 目录（并发多 run 时只报最近活动的那个）。"""
    try:
        entries = list(root.iterdir())
    except OSError:
        return None, []
    best: tuple[float, str, list[dict]] | None = None
    for entry in entries:
        if not entry.is_dir():
            continue
        shards = [s for s in (_load_shard(p) for p in entry.glob("*.json")) if s]
        if not shards:
            continue
        ts = max(float(s.get("updated_at_ts") or 0.0) for s in shards)
        if best is None or ts > best[0]:
            best = (ts, entry.name, shards)
    if best is None:
        return None, []
    return best[1], best[2]


def _empty_bucket(rel: str) -> dict:
    return {"file": rel, "total": 0, "done": 0,
            **{key: 0 for key in _COUNT_KEYS},
            "elapsed_s": 0.0, "started_at": None, "updated_at": None,
            "workers": []}


def _fold_shard(bucket: dict, shard: dict) -> None:
    """单分片并入 file 桶：done/计数求和，total/elapsed 取 max，时间取极值。"""
    bucket["total"] = max(bucket["total"], int(shard.get("total") or 0))
    bucket["done"] += int(shard.get("done") or 0)
    for key in _COUNT_KEYS:
        bucket[key] += int(shard.get(key) or 0)
    bucket["elapsed_s"] = max(bucket["elapsed_s"],
                              float(shard.get("elapsed_s") or 0.0))
    bucket["started_at"] = _extreme(bucket["started_at"],
                                    shard.get("started_at"), min)
    bucket["updated_at"] = _extreme(bucket["updated_at"],
                                    shard.get("updated_at"), max)
    bucket["workers"].append({
        "worker": shard.get("worker"), "pid": shard.get("pid"),
        "total": int(shard.get("total") or 0),
        "done": int(shard.get("done") or 0),
        "elapsed_s": float(shard.get("elapsed_s") or 0.0),
    })


def _extreme(current, candidate, pick):
    if candidate is None:
        return current
    return candidate if current is None else pick(current, candidate)


def _group_by_file(shards: list[dict]) -> dict[str, dict]:
    buckets: dict[str, dict] = {}
    for shard in shards:
        rel = str(shard.get("file") or "")
        _fold_shard(buckets.setdefault(rel, _empty_bucket(rel)), shard)
    return buckets


def _flatten(run_id: str, buckets: dict[str, dict]) -> dict:
    """file 桶 → 单 run 汇总（顶层字段是全 run 合计，files 保留每文件视图）。"""
    out = {"run_id": run_id, "total": 0, "done": 0,
           **{key: 0 for key in _COUNT_KEYS}, "elapsed_s": 0.0,
           "started_at": None, "updated_at": None,
           "files": buckets, "workers": []}
    for bucket in buckets.values():
        out["total"] += bucket["total"]
        out["done"] += bucket["done"]
        for key in _COUNT_KEYS:
            out[key] += bucket[key]
        out["elapsed_s"] = max(out["elapsed_s"], bucket["elapsed_s"])
        out["started_at"] = _extreme(out["started_at"], bucket["started_at"],
                                     min)
        out["updated_at"] = _extreme(out["updated_at"], bucket["updated_at"],
                                     max)
        out["workers"].extend(bucket["workers"])
    return out


def read_progress(project_root: str, file_path: str | None = None) -> dict | None:
    """聚合最新 run 的进度；无进度面（未跑过 / 写失败）返回 None。

    file_path 指定时只返回该文件（项目内相对路径）的桶，否则返回全 run
    合计（`files` 键保留每文件明细）。纯读、无副作用，可在任意会话轮询。
    """
    run_id, shards = _newest_run(progress_root(project_root))
    if run_id is None:
        return None
    buckets = _group_by_file(shards)
    if file_path is None:
        return _flatten(run_id, buckets)
    from ..timing import project_relative

    rel = project_relative(str(Path(project_root).resolve()), str(file_path))
    bucket = buckets.get(rel)
    return None if bucket is None else {"run_id": run_id, **bucket}
