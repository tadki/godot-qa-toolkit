"""SEE-1420 Phase 3 — coverage 前置过滤的持久化链路（定案 L3/D7，P2）。

复用自家 JSON 落盘（拒绝 LCOV 外部格式）：
- ``<project>/.qa-cache/gqt-coverage/<key>.json``——gqt coverage 成功 run 的
  扩展落盘（stdout 契约的超集：多带全量 covered_lines；stdout 契约不动）；
- ``--reuse-coverage`` 读回时做 provenance 校验：schema + 项目内相对路径 +
  源码 sha1 + tests_glob 精确匹配 + 二进制类型——任一不符 = 无该数据
  （mutation 侧告警后【不过滤】继续跑，宁多跑不假阴性）。
- 覆盖数据是「行级事实」，位点级结论由 mutation 侧按位点行号现推——
  uncovered 不持久化、不进 v2 条目（coverage 刷新后立即生效）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ..timing import binary_kind_tag, project_relative, store_key

SCHEMA_COVERAGE = "see1420-coverage/1"

COVERAGE_CACHE_DIRNAME = "gqt-coverage"


def coverage_cache_path(project_root: str, file_path: str,
                        tests_glob: str | None,
                        binary_kind: str | None = None) -> Path:
    key = store_key(project_root, file_path, "coverage", tests_glob,
                    binary_kind=binary_kind_tag(binary_kind))
    return (Path(str(Path(project_root).resolve())) / ".qa-cache"
            / COVERAGE_CACHE_DIRNAME / f"{key}.json")


def save_coverage(covered_lines: set[int], total_lines: int,
                  project_root: str, file_path: str, tests_glob: str | None,
                  binary_kind: str | None = None,
                  coverage_percent: float | None = None) -> bool:
    """coverage 成功 run 的行级事实落盘（原子写；失败告警一次，best-effort）。"""
    kind = binary_kind_tag(binary_kind)
    rel = project_relative(project_root, file_path)
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        print(f"coverage cache: cannot read {file_path}: {e} — not saved",
              file=sys.stderr)
        return False
    doc = {
        "schema": SCHEMA_COVERAGE,
        "rel": rel,
        "tests_glob": tests_glob,
        "binary_kind": kind,
        "source_sha1": source_sha1,
        "total_lines": int(total_lines),
        "covered_lines": sorted(int(l) for l in covered_lines),
        "coverage_percent": coverage_percent,
        "recorded_at": time.time(),
    }
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = path.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(doc, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"coverage cache write failed ({path}): {e} — continuing",
              file=sys.stderr)
        return False


def load_reusable_coverage(project_root: str, file_path: str,
                           tests_glob: str | None, binary_kind=None
                           ) -> tuple[set[int] | None, str | None]:
    """读回可复用的 covered 行集；provenance 不符 = (None, 原因)。

    源码 sha1 在读回时对当前文件实测（落盘后源码变更即失效——行级事实
    必须锚定同一份源码）。vacuous（零可执行行）按无数据降级——过滤无意义。
    """
    kind = binary_kind_tag(binary_kind)
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no cached coverage for this (file, tests_glob)"
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_COVERAGE:
        return None, "coverage cache schema mismatch"
    if raw.get("rel") != project_relative(project_root, file_path):
        return None, "coverage cache path mismatch"
    if raw.get("tests_glob") != tests_glob:
        return None, "coverage cache tests_glob mismatch (exact-match rule)"
    if raw.get("binary_kind") != kind:
        return None, "coverage cache binary kind mismatch"
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        return None, f"cannot read target file: {e}"
    if raw.get("source_sha1") != source_sha1:
        return None, "coverage cache stale (source changed since measurement)"
    lines = raw.get("covered_lines")
    if not isinstance(lines, list):
        return None, "coverage cache malformed (covered_lines missing)"
    return {int(l) for l in lines if isinstance(l, (int, float))}, None
