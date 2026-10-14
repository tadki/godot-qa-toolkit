"""SEE-1405 定案第 7/8 条 — fsprobe：落盘位置探测 + 启动基线独立 store +
迁移判定层（探测/判定/建议值输出；投影执行不在本模块）。

定案口径：
- 独立 store：键 = fstype + 二进制标识 + 挂载点（**无 git_rev**——启动时间
  随 fs/二进制而非代码 rev 变化，rev 失效会让每提交重付双基线空跑），
  TTL 30 天；run 内只读、机会性回填（同 fstype+同二进制的 baseline elapsed
  顺手回填）、探针回填初值（Revy P1 三格矩阵 JSON 即初值来源），
  拿不到即不投影。
- 探测（全只读）：fstype（stat -f f_type → 已知 fstype 名表；失败回退
  /proc/mounts 路径前缀匹配；再失败 fstype=None 诚实降级）+ WSL 标记
  （/proc/version 含 microsoft）+ 挂载点归属。
- 判定式（§SPEC-011 Revy 校准初值）：同机实测基线差 ≥T（T≥2s）且
  轮级税占比 ≥S（S≥10%）→ 建议投影/换二进制；输出建议值与量化依据，
  报告 `summary.fs_projection` 形态，机器可审；不自动执行任何迁移。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

SCHEMA = "see1405-fsprobe/1"
TTL_DAYS = 30

# 判定式初值（§SPEC-011 Revy 实测校准：本机触发差 11-12s、税占比 84-91%——
# 初值在本机全面触发，留作机器可判下界，探针后续可再校准）。
DEFAULT_T_S = 2.0
DEFAULT_S_PCT = 10.0

ENV_FS_PROBE_T = "GQT_FS_PROBE_T"
ENV_FS_PROBE_S = "GQT_FS_PROBE_S"

# stat -f f_type → fstype 名（Linux magic numbers，mgaudit 内核头文件口径）
_FSTYPE_MAGIC = {
    0xEF53: "ext4",
    0x5346544E: "ntfs",
    0x65735546: "tmpfs",
    0x9FA2: "proc",
    0x6969: "nfs",
    0xFF534D42: "cifs",
    0x534F434B: "sockfs",
}


def detect_fstype(path: str) -> str | None:
    """路径所在文件系统类型；stat -f 优先，失败回退 /proc/mounts 前缀，
    再失败 None（诚实降级，不猜）。"""
    try:
        r = subprocess.run(["stat", "-f", "-c", "%T", path],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            out = r.stdout.strip().casefold()
            return out or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _fstype_from_mounts(path)


def _read_mounts() -> list[list[str]]:
    """挂载表行（字段切分）；集中读点供测试注入。"""
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            return [line.split() for line in f]
    except OSError:
        return []


def _fstype_from_mounts(path: str) -> str | None:
    """回退：/proc/mounts 最长前缀匹配行的 fs 字段。"""
    target = str(Path(path).resolve())
    best: tuple[int, str] | None = None
    for parts in _read_mounts():
        if len(parts) < 3:
            continue
        mnt = parts[1].replace("\\040", " ")
        try:
            Path(target).relative_to(mnt)
        except ValueError:
            continue
        depth = len(mnt.rstrip("/").split("/"))
        if best is None or depth > best[0]:
            best = (depth, parts[2])
    return best[1] if best else None


def is_wsl() -> bool:
    """/proc/version 含 microsoft 标记（dispatcher.sh 同款探测语义）。"""
    try:
        return "microsoft" in Path("/proc/version").read_text(
            encoding="utf-8", errors="replace").casefold()
    except OSError:
        return False


def binary_identity() -> str:
    """二进制标识（fsprobe 键成分）：类型 + 可执行文件 sha1 前 12 位。

    P1 定案「同机同二进制」——同类型不同安装位置是不同格；hash 比
    路径稳（PATH 重排不失效键）。
    """
    from .mutation.inject import _resolved_godot_path
    resolved = _resolved_godot_path()
    if not resolved:
        return "unknown"
    try:
        import hashlib
        with open(resolved, "rb") as f:
            digest = hashlib.sha1(f.read(1 << 20)).hexdigest()[:12]
        return f"{os.path.basename(resolved)}:{digest}"
    except OSError:
        return f"{os.path.basename(resolved)}:unreadable"


def probe_environment(project_root: str) -> dict:
    """只读环境探测：fstype / 挂载点 / WSL / 二进制标识（零写入）。"""
    root = str(Path(project_root).resolve())
    return {
        "fstype": detect_fstype(root),
        "mount_point": _mount_point_of(root),
        "wsl": is_wsl(),
        "binary": binary_identity(),
    }


def _mount_point_of(path: str) -> str | None:
    target = str(Path(path).resolve())
    best: tuple[int, str] | None = None
    for parts in _read_mounts():
        if len(parts) < 2:
            continue
        mnt = parts[1].replace("\\040", " ")
        try:
            Path(target).relative_to(mnt)
        except ValueError:
            continue
        depth = len(mnt.rstrip("/").split("/"))
        if best is None or depth > best[0]:
            best = (depth, mnt)
    return best[1] if best else None


class FsBaselineStore:
    """``<project>/.qa-cache/gqt-fsprobe/<key>.json`` 启动基线独立 store。

    键 = fstype + 二进制标识 + 挂载点（无 git_rev、TTL 30 天）。读失败/
    损坏/超期 = 无基线（诚实降级，绝不抛出——降级方向 = 不投影）。
    """

    def __init__(self, project_root: str, fstype: str | None,
                 binary: str, mount_point: str | None,
                 now: float | None = None):
        self.project_root = str(Path(project_root).resolve())
        self.key = self._store_key(fstype, binary, mount_point)
        self._now = time.time() if now is None else now

    @staticmethod
    def _store_key(fstype: str | None, binary: str,
                   mount_point: str | None) -> str:
        import hashlib
        payload = f"{fstype or 'unknown'}|{binary}|{mount_point or 'unknown'}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    @property
    def store_dir(self) -> Path:
        return Path(self.project_root) / ".qa-cache" / "gqt-fsprobe"

    @property
    def entry_path(self) -> Path:
        return self.store_dir / f"{self.key}.json"

    def ensure_store_dir(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.store_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")

    def read(self) -> dict | None:
        try:
            raw = json.loads(self.entry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
            return None
        if self._now - float(raw.get("updated_at", 0)) > TTL_DAYS * 86400:
            return None
        samples = [float(s) for s in raw.get("samples", [])
                   if isinstance(s, (int, float))]
        if not samples:
            return None
        samples.sort()
        n = len(samples)
        mid = n // 2
        median = samples[mid] if n % 2 else (samples[mid - 1] + samples[mid]) / 2
        return {"baseline_s": median, "samples_n": n, "samples": samples}

    def record(self, baseline_s: float) -> bool:
        """追加一个基线采样（机会性回填通道；tail-10 保留）。best-effort。"""
        try:
            self.ensure_store_dir()
            samples: list[float] = []
            try:
                old = json.loads(
                    self.entry_path.read_text(encoding="utf-8"))
                samples = [float(s) for s in old.get("samples", [])][-9:]
            except (OSError, ValueError):
                samples = []
            samples.append(float(baseline_s))
            doc = {"schema": SCHEMA, "key": self.key,
                   "updated_at": self._now, "samples": samples}
            tmp = self.entry_path.with_suffix(f".tmp.{os.getpid()}")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.entry_path)
            return True
        except OSError as e:
            print(f"fsprobe store write failed ({self.entry_path}): {e} — "
                  f"continuing without baseline anchor", file=__import__(
                      "sys").stderr)
            return False


def thresholds() -> tuple[float, float]:
    """T/S 判定阈值：env 可覆写，缺省 T=2.0s / S=10%（§SPEC-011 校准初值）。"""
    def _num(env: str, default: float) -> float:
        raw = os.environ.get(env, "")
        try:
            v = float(raw)
            return v if v > 0 else default
        except ValueError:
            return default
    return _num(ENV_FS_PROBE_T, DEFAULT_T_S), _num(ENV_FS_PROBE_S, DEFAULT_S_PCT)


def recommend_projection(slow_baseline_s: float | None,
                         fast_baseline_s: float | None,
                         expected_runs: int = 1,
                         t_s: float | None = None,
                         s_pct: float | None = None) -> dict:
    """判定式（只出建议值，不执行迁移——§SPEC-012 本子步骤边界）。

    worth = 同机实测基线差 ≥T 且 轮级税占比 ≥S。两基线任一缺失 →
    not_worthy + reason（诚实降级方向 = 不投影）。
    """
    t = DEFAULT_T_S if t_s is None else t_s
    s = DEFAULT_S_PCT if s_pct is None else s_pct
    if slow_baseline_s is None or fast_baseline_s is None:
        return {"worth": False, "reason": "missing_baseline",
                "slow_baseline_s": slow_baseline_s,
                "fast_baseline_s": fast_baseline_s, "t_s": t, "s_pct": s}
    delta = slow_baseline_s - fast_baseline_s
    share = delta / slow_baseline_s * 100 if slow_baseline_s > 0 else 0.0
    worth = delta >= t and share >= s
    return {
        "worth": worth,
        "reason": None if worth else (
            "delta_below_t" if delta < t else "share_below_s"),
        "slow_baseline_s": slow_baseline_s,
        "fast_baseline_s": fast_baseline_s,
        "delta_s": round(delta, 3),
        "tax_share_pct": round(share, 2),
        "expected_runs": expected_runs,
        "projected_saving_s": round(delta * max(0, expected_runs), 3),
        "t_s": t, "s_pct": s,
    }
