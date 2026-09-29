"""SEE-1356 L1 — cross-process timing store + timeout budget negotiation.

根因（SEE-1268 实证）：mutation/coverage 的默认 timeout 静态（60s/120s），KOL
GUT baseline ~108s；每个新进程从 0 学习，baseline 轮自身无预算保护。

定案（plan-debate 终裁，v2 适配）：
- 预算公式：``budget = min(cap=600, max(explicit, 2.0×anchor + 1))``；
  anchor = 有历史取 last，否则 bootstrap 180s（SEE-1268 封条，不许再砍）。
  explicit 只抬下限、不受 cap 截断；有历史不设额外 floor（低耗 chunk 不拍
  脑袋抬预算）；``+1`` 继承 mutation runner 的现行语义。
- 存储：``<project>/.qa-cache/gqt-timing/``（自含 .gitignore），key =
  sha1(realpath(file) + suite_kind + tests_glob)，TTL 7 天 + git_rev 失效。
- 统计量：samples tail-10 + p90 只落盘为观察字段（baseline_p90），待实证
  再升格为锚。
- 判定语义：baseline 永远实测、缓存只喂预算锚（killed/alive 差集对照基准
  不得来自缓存）；SPEC-009 dry-run 跳过读写。
- 双 env：GQT_TIMEOUT_SCALE（默认 2.0）/ GQT_BOOTSTRAP_TIMEOUT_S（默认 180）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

# 预算封顶（秒）：只有"无 explicit"的派生预算被截断到它；explicit 不受 cap。
BUDGET_CAP_S = 600
# bootstrap 锚（秒）：SEE-1268 封条——无历史时的唯一 floor 出处。
BOOTSTRAP_TIMEOUT_S = 180
# 缓存条目有效期（天）：超期视为无历史。
TTL_DAYS = 7
SAMPLES_TAIL = 10

DEFAULT_TIMEOUT_SCALE = 2.0
ENV_TIMEOUT_SCALE = "GQT_TIMEOUT_SCALE"
ENV_BOOTSTRAP_S = "GQT_BOOTSTRAP_TIMEOUT_S"


def timeout_scale() -> float:
    raw = os.environ.get(ENV_TIMEOUT_SCALE, "")
    try:
        v = float(raw)
        return v if v > 0 else DEFAULT_TIMEOUT_SCALE
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SCALE


def bootstrap_timeout_s() -> int:
    raw = os.environ.get(ENV_BOOTSTRAP_S, "")
    try:
        v = int(float(raw))
        return v if v > 0 else BOOTSTRAP_TIMEOUT_S
    except (TypeError, ValueError):
        return BOOTSTRAP_TIMEOUT_S


def resolve_budget(
    explicit: int | None,
    last_elapsed: float | None,
    scale: float | None = None,
    bootstrap: int | None = None,
) -> tuple[int, str]:
    """纯函数：预算协商四分支全覆盖。

    返回 ``(budget_s, timing_source)``，timing_source ∈ explicit|store|bootstrap
    ——归因给"最终决定预算的那一方"：
    - explicit 给定且高于派生值：explicit 赢（不受 cap 截断）→ "explicit"；
    - 其余情况归因 anchor 来源：有历史 "store"，无历史 "bootstrap"。
    - explicit 抬下限：budget = max(explicit, anchor×scale+1)；explicit 缺省：
      budget = min(cap, anchor×scale+1)；anchor：有历史取 last（不设额外
      floor），无历史取 bootstrap。``+1`` 继承 runner.py 现行语义（非新设计）。
    """
    # env 缺省实时读取（GQT_TIMEOUT_SCALE / GQT_BOOTSTRAP_TIMEOUT_S 双 env）。
    k = timeout_scale() if scale is None else scale
    b = bootstrap_timeout_s() if bootstrap is None else bootstrap
    anchor = last_elapsed if last_elapsed is not None else float(b)
    derived = int(anchor * k) + 1
    if explicit is not None and int(explicit) > derived:
        return int(explicit), "explicit"
    if last_elapsed is not None:
        return max(int(explicit), derived) if explicit is not None \
            else min(BUDGET_CAP_S, derived), "store"
    return max(int(explicit), derived) if explicit is not None \
        else min(BUDGET_CAP_S, derived), "bootstrap"


def suggested_action(timeout_s: int) -> str:
    return f"rerun with --timeout {timeout_s}"


def store_key(file_path: str, suite_kind: str, tests_glob: str | None) -> str:
    real = str(Path(file_path).resolve())
    payload = f"{real}|{suite_kind}|{tests_glob or ''}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


class TimingStore:
    """``<project>/.qa-cache/gqt-timing/<key>.json`` — one sample per baseline run.

    key = sha1(realpath(file_path) + suite_kind + tests_glob)。失效判据：
    mtime 超 TTL 天，或记录的 git_rev ≠ 当前 rev。读失败/损坏 = 无历史
    （诚实降级，绝不抛出）。
    """

    def __init__(self, project_root: str, file_path: str, suite_kind: str,
                 tests_glob: str | None = None, now: float | None = None):
        self.project_root = str(Path(project_root).resolve())
        self.file_path = file_path
        self.suite_kind = suite_kind
        self.tests_glob = tests_glob
        self.key = store_key(file_path, suite_kind, tests_glob)
        self._now = time.time() if now is None else now

    @property
    def store_dir(self) -> Path:
        return Path(self.project_root) / ".qa-cache" / "gqt-timing"

    @property
    def entry_path(self) -> Path:
        return self.store_dir / f"{self.key}.json"

    def _git_rev(self) -> str | None:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.project_root, capture_output=True, text=True, timeout=10,
            )
            rev = out.stdout.strip()
            return rev or None
        except (OSError, subprocess.SubprocessError):
            return None

    def read(self) -> dict | None:
        """有效条目 {last, p90, samples_n, samples, git_rev} 或 None（无历史）。"""
        try:
            raw = json.loads(self.entry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        age_s = self._now - float(raw.get("updated_at", 0))
        if age_s > TTL_DAYS * 86400:
            return None
        if raw.get("git_rev") != self._git_rev():
            return None
        samples = [float(s) for s in raw.get("samples", []) if isinstance(s, (int, float))]
        if not samples:
            return None
        return {
            "last": samples[-1],
            "p90": _p90(samples),
            "samples_n": len(samples),
            "samples": samples,
        }

    def record(self, elapsed_s: float) -> bool:
        """Append one baseline sample (tail-10 kept). Returns True on write.

        best-effort：写失败静默返回 False（预算锚是增强，不是契约）。
        """
        try:
            self.store_dir.mkdir(parents=True, exist_ok=True)
            samples: list[float] = []
            try:
                old = json.loads(self.entry_path.read_text(encoding="utf-8"))
                samples = [float(s) for s in old.get("samples", [])]
            except (OSError, ValueError):
                samples = []
            samples.append(round(float(elapsed_s), 3))
            samples = samples[-SAMPLES_TAIL:]
            doc = {
                "schema": "see1356-l1-timing/1",
                "suite_kind": self.suite_kind,
                "tests_glob": self.tests_glob,
                "git_rev": self._git_rev(),
                "updated_at": self._now,
                "samples": samples,
                "last": samples[-1],
                "p90": _p90(samples),
                "samples_n": len(samples),
            }
            tmp = self.entry_path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.entry_path)
            # 自含 ignore：缓存目录对目标项目 git 不可见（不改目标 .gitignore）。
            (self.store_dir / ".gitignore").write_text("*\n", encoding="utf-8")
            return True
        except OSError:
            return False


def _p90(samples: list[float]) -> float:
    """Nearest-rank p90 over the tail-10 sample set（观察字段，非锚）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = max(0, min(len(ordered) - 1, -(-len(ordered) * 9 // 10) - 1))
    return ordered[idx]
