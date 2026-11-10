"""SEE-1550 缓存身份包 + killed 证书 + 疑似孤儿静态诊断（终案 §3.5/§3.3）。

身份包（进 file_key/manifest/coverage store）：``inject_scheme``（注入机制
标签）、``ops_version``、精确 Godot 版本串（按二进制 path+mtime 缓存）、
boot/wiring hash（规范化 project.godot + autoload/ 清单 + testscan 引用
.tscn 清单）、GUT addon 内容哈希。任一成分漂移 = 缓存身份漂移——resume/
coverage 复用全失效并披露。

killed 证书（per-entry）：``killed_by = [{test_name, script_path,
script_sha1, deps_sha1}]`` 完整失败集（撤除旧 runner 的 [:3] 截断）。
script 归属由 tests 树 func 定义扫描解析（非 GUT 输出解析——输出不含稳定
脚本归属，且跨 GUT 版本漂移）；deps 为字面量 preload/load 闭包哈希；
解析失败或闭包不确定 → None → 该 entry 保守重跑（fail-safe 只偏向重跑）。

复用规则（diffengine 消费）：全部 killer 测试仍在且自身/依赖哈希一致 →
复用；killer 改/删/依赖变 → 仅该 entry 重跑；新增测试不失效旧 killed
（kill 单调性）；整树 tests 指纹被拒绝（KOL 963 文件实测 22.9–45s，且会让
增量循环过失效）。

疑似孤儿（unreachable 披露面）：静态索引存在动态 load、hub 截断、uid/场景
间接引用不可判定面——零静态引用只做「疑似孤儿」诊断披露，不进 verdict、
不 skip（终案 §3.3 明确拒绝 unreachable skip）。
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

from .ops import OPS_VERSION
from .testscan import _RES_REF_PATTERN

INJECT_SCHEME = "disk-farm/1"

# SEE-1551 定案 §3.6：exec_scheme 双标签——killed 证书按 scheme 隔离
# （身份包入 file_key，跨 scheme 天然不复用）。
COLD_SCHEME = "cold-shadow/1"
WORKER_SCHEME = "worker-reset/1"

# Godot --version 不可解析时的身份兜底：按二进制路径界定（不同二进制不共享
# 身份），跨 run 稳定。fail-soft 理由：版本探针失败时 dry-run/diff 规划仍须
# 可用（CI 无引擎环境）；执行路径另由 godot 启动失败契约兜底。
_ENV_UNRESOLVED_PREFIX = "unresolved:"

# 版本串缓存键 = (resolved path, mtime_ns)：二进制升级（mtime 变化）自动
# 重解析，同进程内 mutant 循环零重复付。
_VERSION_CACHE: dict[tuple[str, int], str] = {}


def _sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _file_sha1(path: Path) -> str | None:
    try:
        return _sha1_bytes(path.read_bytes())
    except OSError:
        return None


def godot_version_string(godot_bin: str) -> str:
    """精确 Godot 版本串（``--version``）；按 (path, mtime_ns) 缓存。

    解析失败（缺失/不可执行/超时）→ ``unresolved:<path>`` 兜底键（stderr
    一次性披露）——身份仍然确定性，且按二进制路径隔离不同引擎。
    """
    try:
        mtime = Path(godot_bin).stat().st_mtime_ns
    except OSError:
        mtime = -1
    cached = _VERSION_CACHE.get((godot_bin, mtime))
    if cached is not None:
        return cached
    try:
        r = subprocess.run([godot_bin, "--version"], capture_output=True,
                           text=True, timeout=30)
        version = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    if not version:
        print(f"identity: godot --version unresolved for {godot_bin} — "
              f"identity keyed by binary path (unresolved)", file=sys.stderr)
        version = f"{_ENV_UNRESOLVED_PREFIX}{godot_bin}"
    _VERSION_CACHE[(godot_bin, mtime)] = version
    return version


def normalize_project_godot(text: str) -> list[str]:
    """project.godot 规范化行：去注释/空行、剥 key=value 两侧空白，保序。

    排版/注释变更不触发 wiring 失效；键值与 section 顺序变更触发（section
    顺序在 Godot 语义上无关但改动意味着手写编辑——保守失效方向安全）。
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        if line.startswith("[") and line.endswith("]"):
            out.append(line)
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out.append(f"{k.strip()}={v.strip()}")
        else:
            out.append(line)
    return out


def boot_wiring_hash(project_root: str) -> str:
    """boot/wiring hash：规范化 project.godot + autoload/ 清单 + testscan
    引用 .tscn 清单的联合 sha1。

    autoload 清单 = autoload/ 下 .gd 的相对路径排序集（接线面，不含内容——
    内容变更由 killer 证书与差分 scope hash 承接）；.tscn 清单 = tests 树
    res://*.tscn 字面量排序集（测试驱动场景的接线面）。
    """
    root = Path(project_root)
    parts: list[str] = []
    pg = root / "project.godot"
    parts.extend(normalize_project_godot(
        pg.read_text(encoding="utf-8", errors="replace")) if pg.is_file()
        else ["<no project.godot>"])
    autoload_dir = root / "autoload"
    if autoload_dir.is_dir():
        parts.extend(sorted(
            str(p.relative_to(root)).replace("\\", "/")
            for p in autoload_dir.rglob("*.gd")))
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        scenes: set[str] = set()
        for gd in tests_dir.rglob("*.gd"):
            try:
                text = gd.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for ref in _RES_REF_PATTERN.findall(text):
                if ref.casefold().endswith(".tscn"):
                    scenes.add(ref.strip().casefold())
        parts.extend(sorted(scenes))
    return _sha1_text("\n".join(parts))


def gut_addon_hash(project_root: str) -> str:
    """GUT addon 内容哈希：addons/gut/** 的 (rel:content-sha1) 联合 sha1。

    addon 缺失 → "missing"（fixture 单测环境）；内容升级 → 身份全量失效
    （终案 §3.5：GUT/harness 升级走身份包全量失效）。
    """
    gut = Path(project_root) / "addons" / "gut"
    if not gut.is_dir():
        return "missing"
    lines: list[str] = []
    root = Path(project_root)
    for p in sorted(gut.rglob("*")):
        if p.is_file():
            sha = _file_sha1(p)
            if sha is not None:
                lines.append(f"{p.relative_to(root).as_posix()}:{sha}")
    return _sha1_text("\n".join(lines))


def identity_pack(project_root: str, binary_kind: str | None,
                  godot_bin: str | None = None,
                  exec_scheme: str = COLD_SCHEME) -> dict:
    """完整身份包（终案 §3.5）。godot_bin 缺省经统一解析源取（与启动同源）。

    exec_scheme（§SPEC-020）：cold-shadow/1（M0 冷通道）vs worker-reset/1
    （常驻 worker）——入身份包 → file_key/killed 证书按 scheme 隔离。
    """
    if godot_bin is None:
        from .inject import godot_command
        godot_bin = godot_command()
    return {
        "inject_scheme": INJECT_SCHEME,
        "exec_scheme": exec_scheme,
        "ops_version": OPS_VERSION,
        "godot_version": godot_version_string(godot_bin),
        "boot_wiring_hash": boot_wiring_hash(project_root),
        "gut_addon_hash": gut_addon_hash(project_root),
        "binary_kind": binary_kind or "",
    }


def identity_sha1(pack: dict) -> str:
    """身份包 → 稳定 sha1（file_key 成分；键序无关，值序固定）。"""
    parts = [f"{k}={pack[k]}" for k in sorted(pack)]
    return _sha1_text("\n".join(parts))


# ---------------------------------------------------------------------------
# killed 证书（§SPEC-007）
# ---------------------------------------------------------------------------

# 测试脚本内 func 定义行（GUT 测试名即 func 名）；只认 tests 树。
_TEST_FUNC_PATTERN = re.compile(r"(?m)^\s*func\s+(test_\w+)\s*\(")

_DEPS_MAX_DEPTH = 8


class TestOwnershipIndex:
    """tests 树 func 定义 → 脚本路径索引（一次构建，killed 判定多次消费）。

    同名测试定义在多个脚本 → 歧义（证书不确定 → None → 保守重跑）。
    """

    # R-3：类名以 Test 开头会被 pytest 误收集为测试类（PytestCollectionWarning
    # 存量）——产品域索引类，显式退出收集面（最小不改行为）。
    __test__ = False

    def __init__(self, project_root: str):
        self.project_root = str(Path(project_root).resolve())
        self._map: dict[str, set[str]] = {}
        tests = Path(self.project_root) / "tests"
        if tests.is_dir():
            for gd in sorted(tests.rglob("*.gd")):
                try:
                    text = gd.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                rel = gd.relative_to(self.project_root).as_posix()
                for name in _TEST_FUNC_PATTERN.findall(text):
                    self._map.setdefault(name, set()).add(rel)

    def owner_of(self, test_name: str) -> str | None:
        """唯一归属脚本的 res:// 形态；零命中或多命中 → None。"""
        owners = self._map.get(test_name)
        if not owners or len(owners) != 1:
            return None
        return "res://" + next(iter(owners))


def _res_to_fs(project_root: str, res_path: str) -> Path:
    return Path(project_root) / res_path.removeprefix("res://")


def deps_closure_sha1(project_root: str, script_res: str,
                      depth: int = 0) -> str | None:
    """字面量 preload/load 依赖闭包哈希；不确定 → None（保守重跑）。

    BFS：脚本内 res:// 字面量 → 已存在文件入集（内容 sha1）；.gd 依赖递归
    展开（深度上限防环/防失控）；任何文件读不到（字面量指向运行时生成
    路径等）→ None——闭包不确定即不签发证书。
    """
    if depth > _DEPS_MAX_DEPTH:
        return None
    root = Path(project_root).resolve()
    seen: set[str] = set()
    lines: list[str] = []

    def walk(res_path: str, level: int) -> bool:
        if res_path in seen:
            return True
        seen.add(res_path)
        fs = _res_to_fs(str(root), res_path)
        sha = _file_sha1(fs)
        if sha is None:
            return False
        lines.append(f"{res_path}:{sha}")
        if fs.suffix == ".gd" and level < _DEPS_MAX_DEPTH:
            try:
                text = fs.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            for ref in _RES_REF_PATTERN.findall(text):
                if not walk(ref, level + 1):
                    return False
        return True

    if not walk(script_res, depth):
        return None
    return _sha1_text("\n".join(sorted(lines)))


def killer_certificate(failures: set[str], ownership: TestOwnershipIndex,
                       project_root: str) -> list[dict] | None:
    """完整失败集 → killed_by 证书；任一 killer 归属/闭包不确定 → None。

    None 语义（终案 §3.5）：fail-safe 只偏向重跑——该 entry 下轮差分重跑，
    绝不静默复用。
    """
    cert: list[dict] = []
    for name in sorted(failures):
        owner_res = ownership.owner_of(name)
        if owner_res is None:
            return None
        script_sha = _file_sha1(_res_to_fs(project_root, owner_res))
        if script_sha is None:
            return None
        deps_sha = deps_closure_sha1(project_root, owner_res)
        if deps_sha is None:
            return None
        cert.append({"test_name": name, "script_path": owner_res,
                     "script_sha1": script_sha, "deps_sha1": deps_sha})
    return cert


def certificate_valid(killed_by: list, project_root: str) -> bool:
    """复用前证书校验：每个 killer 仍在、内容一致、依赖闭包一致。

    校验只碰 killer 测试与其闭包（定向），绝不整树指纹（终案 §3.5 明确
    拒绝整树 tests 指纹——新增测试不失效旧 killed）。
    """
    if not isinstance(killed_by, list) or not killed_by:
        return False
    for k in killed_by:
        if not isinstance(k, dict):
            return False
        path = k.get("script_path")
        if not isinstance(path, str) or not path.startswith("res://"):
            return False
        fs = _res_to_fs(project_root, path)
        if _file_sha1(fs) != k.get("script_sha1"):
            return False
        if deps_closure_sha1(project_root, path) != k.get("deps_sha1"):
            return False
    return True


# ---------------------------------------------------------------------------
# 疑似孤儿静态诊断（unreachable 披露面——不判定，只披露）
# ---------------------------------------------------------------------------

def _stem_word(target_res: str) -> re.Pattern:
    stem = Path(target_res).stem.casefold()
    return re.compile(r"\b" + re.escape(stem) + r"\b")


def suspected_orphan(project_root: str, target_res: str) -> bool:
    """目标脚本零静态引用 → True（疑似孤儿披露）。

    引用面四层：project.godot（autoload 接线）→ .tscn/.tres（ext_resource
    路径）→ 其余 .gd（不含目标自身与 addons/）→ tests 树。不可判定面
    （动态 load 构造路径、uid 间接引用）本来就扫不到——零命中只够「疑似」，
    永远不够「不可达」。扫描域排除 .qa-cache/.godot/.git（影子槽副本与
    引擎缓存不是引用）。
    """
    root = Path(project_root)
    if _referenced_by_project_godot(root, target_res):
        return False
    if _referenced_by_scenes(root, target_res):
        return False
    if _referenced_by_scripts(root, target_res):
        return False
    return not _referenced_by_tests(root, target_res)


def _project_text_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    skip_parts = {".qa-cache", ".godot", ".git"}
    out: list[Path] = []
    for suffix in suffixes:
        out.extend(p for p in root.rglob(f"*{suffix}")
                   if not (skip_parts & set(p.parts)))
    return out


def _referenced_by_project_godot(root: Path, target_res: str) -> bool:
    pg = root / "project.godot"
    if not pg.is_file():
        return False
    try:
        text = pg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return target_res.strip().casefold() in text.casefold()


def _referenced_by_scenes(root: Path, target_res: str) -> bool:
    target_cf = target_res.strip().casefold()
    for p in _project_text_files(root, (".tscn", ".tres")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if target_cf in text.casefold():
            return True
    return False


def _referenced_by_scripts(root: Path, target_res: str) -> bool:
    target_fs = _res_to_fs(str(root), target_res).resolve()
    tests = root / "tests"
    stem_word = _stem_word(target_res)
    for gd in _project_text_files(root, (".gd",)):
        if "addons" in gd.parts or tests in gd.parents:
            continue
        if gd.resolve() == target_fs:
            continue
        try:
            text = gd.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _text_references_target(text, target_res, stem_word):
            return True
    return False


def _referenced_by_tests(root: Path, target_res: str) -> bool:
    tests = root / "tests"
    if not tests.is_dir():
        return False
    stem_word = _stem_word(target_res)
    for gd in tests.rglob("*.gd"):
        try:
            text = gd.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _text_references_target(text, target_res, stem_word):
            return True
    return False


def _text_references_target(text: str, target_res: str, stem_word) -> bool:
    return (any(_res_hits(r, target_res)
                for r in _RES_REF_PATTERN.findall(text))
            or bool(stem_word.search(text.casefold())))


def _res_hits(ref: str, target_res: str) -> bool:
    """res:// 字面量是否指向目标（testscan._ref_hits_target 的独立窄版，
    避免对 scanner 内部签名依赖扩散）。"""
    ref_cf = ref.strip().casefold()
    target_cf = target_res.strip().casefold()
    return ref_cf == target_cf or ref_cf.endswith("/" + target_cf)


# ---------------------------------------------------------------------------
# 旧 store 一次性失效披露（Owner 裁决菜单 B）
# ---------------------------------------------------------------------------

def disclose_superseded(project_root: str, resume_store_dir: Path,
                        coverage_store_dir: Path) -> None:
    """OPS_VERSION 已 bump 后的旧 v2 store：不读不写不删，仅 stderr 披露。

    判据 = manifest.json 的 ops_version ≠ 当前 OPS_VERSION（只读 manifest
    索引层，条目文件零扫描）；coverage store 以目录在场 + schema 无
    inject_scheme 字段为旧态信号（读一个已存在条目样本）。"""
    try:
        manifests = sorted(resume_store_dir.glob("*/manifest.json"))
    except OSError:
        manifests = []
    stale = 0
    for m in manifests:
        try:
            import json
            raw = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stale += 1
            continue
        if isinstance(raw, dict) and raw.get("ops_version") != OPS_VERSION:
            stale += 1
    if stale:
        print(f"resume store: {stale} file-key group(s) under "
              f"{resume_store_dir} carry ops_version != {OPS_VERSION} — "
              f"superseded by OPS {OPS_VERSION} (disk-farm injection): old "
              f"entries are neither read, written, nor deleted; first run "
              f"re-executes all sites (one-time invalidation)", file=sys.stderr)
    try:
        cov_files = sorted(coverage_store_dir.glob("*.json"))
    except OSError:
        cov_files = []
    if cov_files:
        try:
            import json
            sample = json.loads(cov_files[0].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sample = None
        if not isinstance(sample, dict) or \
                "inject_scheme" not in (sample.get("identity") or {}):
            print(f"coverage cache: entries under {coverage_store_dir} lack "
                  f"inject_scheme identity — superseded by OPS {OPS_VERSION} "
                  f"(old entries neither read nor deleted; first run "
                  f"re-measures)", file=sys.stderr)
