"""SEE-1400 — per-mutant resume store（断点续跑持久化层）。

根因（SEE-1397 实证）：mutation run 被取消/中断后全部作废重来——results
仅内存累积，3h run 的采样数据全丢。

形态（TimingStore 先例，SEE-1356 L1）：
- 存储：``<project>/.qa-cache/gqt-mutation-resume/<key>/<seq>.json``——
  每 mutant 一文件、tmp+os.replace 原子写；同文件多 worker 各管各 seq，
  天然无跨 worker 竞态。
- key = sha1(realpath(target) + 源码 sha1 + tests_glob + budget + OPS_VERSION)。
  任一成分变化 = 指纹不匹配 = 换 key = 旧条目全部作废重跑（防脏合并；
  ops.py 升级后旧 verdict 不得跨版本复用）。
- 常规 run（不带 --resume）同样落盘——中断后即可续跑；``--resume`` 只打开
  「跳过已完成」语义。
- 诚实降级：store 损坏 / 读失败 / 条目身份与当前 mutant 不符 = 无该条
  （重跑），绝不抛出、绝不静默合并脏数据。

verdict 六态（runner._mutant_record）全部视为「已完成」：killed / survived /
suspect / timeout / run_error / invalid_mutant。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .ops import OPS_VERSION

SCHEMA = "see1400-resume/1"

# 已完成的 verdict 全集——store 只复用这些；缺 schema/verdict 非法的条目作废。
FINAL_VERDICTS = frozenset({
    "killed", "survived", "suspect", "timeout", "run_error", "invalid_mutant",
})


def resume_key(file_path: str, source_sha1: str, tests_glob: str | None,
               budget: int) -> str:
    """指纹 key：realpath + 源码 sha1 + 生效 tests_glob + budget + ops 版本。"""
    payload = (f"{str(Path(file_path).resolve())}|{source_sha1}|"
               f"{tests_glob or ''}|{int(budget)}|{OPS_VERSION}")
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class ResumeStore:
    """每 mutant 一文件、按 seq 索引的增量 verdict 持久化。"""

    def __init__(self, project_root: str, file_path: str,
                 tests_glob: str | None, budget: int,
                 now: float | None = None):
        self.project_root = str(Path(project_root).resolve())
        self.file_path = file_path
        self.tests_glob = tests_glob
        self.budget = int(budget)
        self.source_sha1 = self._sha1_of(file_path)
        self.key = resume_key(file_path, self.source_sha1, tests_glob,
                              self.budget)
        self._now = time.time() if now is None else now
        self._warned_write = False

    @staticmethod
    def _sha1_of(file_path: str) -> str:
        try:
            return hashlib.sha1(
                Path(file_path).read_bytes()).hexdigest()
        except OSError as e:
            # 目标文件读不了本就是 run 前置失败——这里给确定性空指纹，
            # 让 store 保持可构造（真实失败由 runner 的 FileNotFoundError 走契约）。
            print(f"resume store: cannot read {file_path}: {e}", file=sys.stderr)
            return "unreadable"

    @property
    def store_dir(self) -> Path:
        return (Path(self.project_root) / ".qa-cache"
                / "gqt-mutation-resume" / self.key)

    def ensure_store_dir(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.store_dir / ".gitignore"
        if not gitignore.exists():
            # 自含 ignore：缓存目录对目标项目 git 不可见（TimingStore 同款）。
            gitignore.write_text("*\n", encoding="utf-8")

    def entry_path(self, seq: int) -> Path:
        return self.store_dir / f"{int(seq)}.json"

    def record(self, seq: int, verdict_record: dict) -> bool:
        """一个 mutant verdict 原子落盘；写失败告警一次并返回 False（best-effort）。"""
        try:
            self.ensure_store_dir()
            doc = {
                "schema": SCHEMA,
                "key": self.key,
                "seq": int(seq),
                "recorded_at": self._now,
                **verdict_record,
            }
            tmp = self.store_dir / f"{int(seq)}.json.tmp.{os.getpid()}"
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, self.entry_path(seq))
            return True
        except OSError as e:
            if not self._warned_write:
                self._warned_write = True
                print(f"resume store write failed ({self.store_dir}): {e} "
                      f"— continuing without resumable history", file=sys.stderr)
            return False

    def _read_entry(self, seq: int) -> dict | None:
        """读一个条目；schema/verdict 非法或读失败 = 无该条（诚实降级）。"""
        try:
            raw = json.loads(self.entry_path(seq).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return None
        if raw.get("verdict") not in FINAL_VERDICTS:
            return None
        return raw

    def load_completed(self, mutants: list, start: int, stop: int) -> dict[int, dict]:
        """[start, stop) 内可复用的已完成条目 {seq: verdict_record}。

        身份核对（belt-and-braces）：指纹 key 只保证源码/算子版本一致，
        条目自身还要与当前 mutant 的 kind/original/mutated/line 完全一致才
        复用——任何不一致按无历史处理（宁可重跑，不脏合并）。
        """
        completed: dict[int, dict] = {}
        for seq in range(max(0, start), min(stop, len(mutants))):
            raw = self._read_entry(seq)
            if raw is None:
                continue
            m = mutants[seq]
            if (raw.get("kind") == m.kind and raw.get("original") == m.original
                    and raw.get("mutated") == m.mutated
                    and raw.get("line") == m.line):
                completed[seq] = raw
        return completed

    def completed_count(self) -> int:
        """store 内合法条目数（forensic/判据用；读失败按 0 计）。"""
        try:
            return sum(
                1 for p in sorted(self.store_dir.glob("*.json"))
                if self._valid_entry_file(p))
        except OSError:
            return 0

    def _valid_entry_file(self, path: Path) -> bool:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (isinstance(raw, dict) and raw.get("schema") == SCHEMA
                and raw.get("verdict") in FINAL_VERDICTS)
