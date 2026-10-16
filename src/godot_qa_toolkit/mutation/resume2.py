"""SEE-1420 Phase 1 — resume v2 store（per-file manifest 索引层 + per-mutant
条目保留，一次 schema bump 定案 §三）。

形态：
- ``<project>/.qa-cache/gqt-mutation-resume-v2/<file-key>/manifest.json``——
  per-file 索引层：schema/ops 版本/header hash/含位点 scope hash 表。由
  run 收尾方（串行 run 自身 / 并发父层）单点写入，规避并发 worker 竞态。
- ``<file-key>/<stable-id>.json``——per-mutant 条目（verdict + timeout_
  streak + 全签名五元组），tmp+os.replace 原子写；同文件多 worker 各写各
  id，天然无跨 worker 竞态（v1 per-seq 同款结论）。
- file-key = sha1(rel|tests_glob|OPS_VERSION|binary)——**无 budget**（D6：
  budget 是每 run 差分后 retry 面的执行上限，不是 verdict 身份成分；进 key
  会让调预算的用户新建全空 manifest，差分收益归零）、**无 --mutation-warning
  / --timeout-factor / timeout_streak**（D6 负面清单 + D5 黑名单条款）。
- v1 目录（gqt-mutation-resume）**不读不写不删**；在场即 stderr 一次性
  显式披露（一次性失效窗口：首个 v2 run 全量重跑）。
- 诚实降级：manifest/条目读失败、schema 不符、毒条目 = 无该数据（重跑），
  绝不抛出、绝不静默合并脏数据。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ..timing import binary_kind_tag, project_relative
from .ops import OPS_VERSION

SCHEMA_V2 = "see1420-resume/2"

RESUME_V1_DIRNAME = "gqt-mutation-resume"
RESUME_V2_DIRNAME = "gqt-mutation-resume-v2"

# 信封核对失败（key 不符）与毒条目同样按无数据处理——身份漂移的条目
# 绝不复用（v1 hardener 同款红线）。
_LAUNCH_FAILURE_MARKERS = (
    "memory injection failed:",
    "godot launch failed (tests did not run):",
)


def file_key(project_root: str, file_path: str, tests_glob: str | None,
             binary_kind: str | None = None) -> str:
    kind = binary_kind if binary_kind is not None else binary_kind_tag(None)
    rel = project_relative(project_root, file_path)
    payload = f"{rel}|{tests_glob or ''}|{OPS_VERSION}|{kind}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class ResumeStoreV2:
    """v2 断点续跑持久化：manifest.json 索引层 + per-id 条目文件。"""

    def __init__(self, project_root: str, file_path: str,
                 tests_glob: str | None, binary_kind: str | None = None,
                 now: float | None = None):
        self.project_root = str(Path(project_root).resolve())
        self.file_path = file_path
        self.tests_glob = tests_glob
        self.binary_kind = binary_kind_tag(binary_kind)
        self.key = file_key(project_root, file_path, tests_glob,
                            binary_kind=self.binary_kind)
        self._now = time.time() if now is None else now
        self._warned_write = False
        self._warned_manifest = False

    # -- 路径面 ----------------------------------------------------------

    @property
    def store_dir(self) -> Path:
        return (Path(self.project_root) / ".qa-cache" / RESUME_V2_DIRNAME
                / self.key)

    @property
    def manifest_path(self) -> Path:
        return self.store_dir / "manifest.json"

    def entry_path(self, stable_id: str) -> Path:
        # stable_id 含 '/' 形态的 file 成分时压平（GDScript 标识符本无 '/',
        # 防御外部构造的 id 破坏目录结构；'\' 同压——Windows 路径分隔符
        # 的目录逃逸面，合法 id 经 project_relative 归一后不含反斜杠）。
        safe = stable_id.replace("/", "__").replace("\\", "__")
        return self.store_dir / f"{safe}.json"

    def ensure_store_dir(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.store_dir / ".gitignore"
        if not gitignore.exists():
            # 自含 ignore：缓存目录对目标项目 git 不可见（TimingStore 同款）。
            gitignore.write_text("*\n", encoding="utf-8")

    # -- v1 超越披露 ------------------------------------------------------

    @staticmethod
    def _sha1_text(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def disclose_v1_superseded(self) -> bool:
        """v1 目录在场 → stderr 一次性披露；返回是否在场。只披露，不读不写不删。"""
        v1 = (Path(self.project_root) / ".qa-cache" / RESUME_V1_DIRNAME)
        try:
            present = v1.is_dir() and any(v1.iterdir())
        except OSError:
            return False
        if present:
            print(f"resume v1 store found at {v1} — schema {SCHEMA_V2} "
                  f"supersedes it: v1 entries are neither read, written, nor "
                  f"deleted; first v2 run re-executes all sites (one-time "
                  f"invalidation, SEE-1420)", file=sys.stderr)
        return present

    # -- manifest 索引层 --------------------------------------------------

    def write_manifest(self, header_hash: str, scopes: dict[str, str],
                       source_sha1: str, rel: str) -> bool:
        """单点写入 manifest 索引层（收尾方调用）；写失败告警一次（best-effort）。"""
        try:
            self.ensure_store_dir()
            doc = {
                "schema": SCHEMA_V2,
                "file_key": self.key,
                "rel": rel,
                "tests_glob": self.tests_glob,
                "ops_version": OPS_VERSION,
                "binary_kind": self.binary_kind,
                "source_sha1": source_sha1,
                "header_hash": header_hash,
                "scopes": dict(scopes),
                "updated_at": self._now,
            }
            return self._atomic_write(self.manifest_path, doc)
        except OSError as e:
            self._warn_write_failure(e)
            return False

    def load_manifest(self) -> dict | None:
        """读合法 manifest；schema/key 不符或读失败 = None（诚实降级→全量跑）。

        §SPEC-1420-05：信封校验失败必须告警（拒复用 ≠ 静默吞）。
        """
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except OSError as e:
            # 读失败（缺失=正常首跑；权限/IO 异常=存疑）——缺失静默，异常告警
            if self.manifest_path.exists():
                self._warn_manifest_once(
                    f"manifest unreadable ({self.manifest_path.name}: {e})")
            return None
        except ValueError as e:
            # O1：损坏 JSON 静默降级 = 坏条目同款纪律违反——按无历史处理
            # 但必须告警（拒复用 ≠ 静默吞；每实例一次，不重刷）。
            self._warn_manifest_once(
                f"manifest corrupted ({self.manifest_path.name}: {e})")
            return None
        if not isinstance(raw, dict):
            print(f"resume v2 store: manifest is not an object "
                  f"({self.manifest_path.name}) — treating as no history",
                  file=sys.stderr)
            return None
        if raw.get("schema") != SCHEMA_V2 or raw.get("file_key") != self.key:
            print(f"resume v2 store: manifest envelope mismatch "
                  f"(schema={raw.get('schema')!r}, file_key_match="
                  f"{raw.get('file_key') == self.key}) — treating as no "
                  f"history (full rerun)", file=sys.stderr)
            return None
        return raw

    # -- per-mutant 条目 ---------------------------------------------------

    def record_entry(self, entry: dict) -> bool:
        """单 mutant 条目原子落盘；毒条目（启动失败类 run_error）拒收。

        timeout_streak 决不进任何 key/黑名单（D5 条款）——它只是条目字段。
        """
        if _is_poison_entry(entry):
            return False
        try:
            self.ensure_store_dir()
            doc = {
                "schema": SCHEMA_V2,
                "file_key": self.key,
                "recorded_at": self._now,
                **entry,
            }
            return self._atomic_write(self.entry_path(entry["id"]), doc)
        except (OSError, KeyError) as e:
            self._warn_write_failure(e)
            return False

    def load_entries(self) -> dict[str, dict]:
        """{stable_id: entry}；损坏/毒/信封漂移条目告警一次并按无数据处理。

        §SPEC-1420-05：信封（schema/file_key）校验失败与毒条目拒用的同批
        汇总告警一次——拒复用，但绝不静默吞掉身份漂移的证据。
        """
        entries: dict[str, dict] = {}
        try:
            paths = sorted(self.store_dir.glob("*.json"))
        except OSError:
            return entries
        warned = False
        drifted = 0
        poisoned = 0
        for p in paths:
            if p.name == "manifest.json":
                continue
            try:
                raw = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if not warned:
                    warned = True
                    print(f"resume v2 store: unreadable entry {p.name} "
                          f"— treating as no history", file=sys.stderr)
                continue
            if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_V2 \
                    or raw.get("file_key") != self.key:
                drifted += 1
                continue
            if _is_poison_entry(raw):
                poisoned += 1
                continue
            sid = raw.get("id")
            if not isinstance(sid, str) or not sid:
                drifted += 1
                continue
            entries[sid] = raw
        if drifted or poisoned:
            print(f"resume v2 store: {drifted} envelope-drift and "
                  f"{poisoned} poison entries rejected — treating as "
                  f"no history", file=sys.stderr)
        return entries

    # -- 内部 ---------------------------------------------------------------

    def _atomic_write(self, path: Path, doc: dict) -> bool:
        try:
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError as e:
            self._warn_write_failure(e)
            return False

    def _warn_write_failure(self, err: OSError) -> None:
        if not self._warned_write:
            self._warned_write = True
            print(f"resume v2 store write failed ({self.store_dir}): {err} "
                  f"— continuing without resumable history", file=sys.stderr)

    def _warn_manifest_once(self, detail: str) -> None:
        """O1：每实例告警一次——与坏条目 load 告警同口径。"""
        if not self._warned_manifest:
            self._warned_manifest = True
            print(f"resume v2 store: {detail} — treating as no history "
                  f"(full rerun)", file=sys.stderr)


def _is_poison_entry(entry: dict) -> bool:
    """启动失败类 run_error 对 mutant 零信息量——落盘侧拒收、载入侧拒用。"""
    return (entry.get("verdict") == "run_error" and any(
        m in entry.get("reason", "") for m in _LAUNCH_FAILURE_MARKERS))
