"""accept 私有目录管线（§SPEC-035，acceptance-suite/1）。

选择在编译期完成：compile 把选中 keys 写进一次性私有目录
`acceptance/generated/.gqt-run-<uuid>/`（事先为空，非空即拒），run 对该
目录整目录执行——目录即选择集，绕开 GUT -gselect 子串碰撞（选 {a} 不会
误吸 test_ab.gd）。执行前后做对账五项：

  R1 生成数==选中数          generated_eq_selected
  R2 执行脚本集==生成词干集  executed_scripts_eq_generated
  R3 计数相等                executed_count_eq_expected
  R4 未选/旧 generated 缺席  unselected_absent（含主目录字节级快照比对）
  R5 私有目录重放语义        replay_checked

五项全过 → 清理零残留（私有目录移除；父目录为本管线创建且已空才一并
移除，KOL 主 generated 有既有产物则原样保留）；任一不过 → 私有目录保留
现场、ok=false。主 acceptance/ir|generated 全程只读不碰。W4 成本探针
（run_acceptance_probe）实测「编译 + 执行」墙钟，产出
performance.acceptance_probe 记录。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .compile import compile_feature
from .run import AcceptRunError, _strip_ansi, replay_check
from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.gutrun import _looks_like_godot_launch_failure
from ..mutation.inject import godot_command

SUITES_VERSION = "acceptance-suite/1"
PROBE_VERSION = "acceptance-probe/1"
EXTRAPOLATION_ASSUMPTION = "linear-in-feature-count"


class SuiteError(Exception):
    """私有目录生命周期违例（非空创建、未知 key、清理失败等）。"""


def create_suite_dir(project_root: str | Path, *,
                     run_uuid: str | None = None) -> Path:
    """创建一次性私有目录 `<root>/acceptance/generated/.gqt-run-<uuid>/`。

    事先为空是硬约束：同路径已存在 = SuiteError（绝不复用旧目录，防旧
    generated 混入选择集）。
    """
    root = Path(project_root)
    token = run_uuid or uuid.uuid4().hex[:12]
    d = root / "acceptance" / "generated" / f".gqt-run-{token}"
    if d.exists():
        raise SuiteError(
            f"private suite dir already exists (must be created empty): {d}")
    d.mkdir(parents=True)
    return d


def _resolve_feature(project_root: Path, key: str) -> Path | None:
    feats = project_root / "acceptance" / "features"
    if not feats.is_dir():
        return None
    for p in sorted(feats.rglob("*.feature")):
        if p.stem == key:
            return p
    return None


def compile_keys_to_suite(project_root: str | Path, keys: list[str],
                          suite_dir: str | Path) -> dict:
    """把选中 keys 逐个编译进私有目录（out_dir 分支），返回选择集记录。

    编译期完成选择：只有选中 keys 的产物进目录——目录即选择集。
    """
    root = Path(project_root)
    suite_dir = Path(suite_dir)
    selected = sorted(set(keys))
    manifests: dict[str, dict] = {}
    for key in selected:
        f = _resolve_feature(root, key)
        if f is None:
            raise SuiteError(
                f"unknown key: {key} — no acceptance/features entry "
                f"with that stem")
        suite, error = compile_feature(f, root, write=True,
                                       out_dir=suite_dir)
        if error is not None:
            raise SuiteError(f"compile failed for {key}: {error['reason']}")
        manifests[key] = suite.manifest
    gen = suite_dir / "generated"
    for key in selected:
        for must in (f"test_{key}.gd", f"{key}.manifest.json"):
            if not (gen / must).is_file():
                raise SuiteError(f"compile wrote no {must} for key {key}")
    return {"keys": selected, "suite_dir": str(suite_dir),
            "manifests": manifests}


# 真机 GUT 9.6 输出锚定（lab 实跑证据）：运行流按脚本分段，脚本头是独占
# 一行的 res://….gd 路径；段内 `* test_x` 归属该脚本。跨脚本同名测试
# （不同 feature 同名场景）只有靠脚本段才能区分——R2/R3 以脚本段为准。
_SCRIPT_HEADER_RE = re.compile(r"^res://\S+\.gd$")
_SUMMARY_MARKER = "Run Summary"
# GUT 运行流把生命周期钩子也以 `* name` 打进脚本段（真机锚定：aidomain
# 探针 11 行 = 10 测试 + before_all）——钩子不是测试，排除出对账计数。
_GUT_HOOKS = frozenset(
    {"before_all", "after_all", "before_each", "after_each"})


def _executed_scripts(gut_output: str) -> dict[str, list[str]]:
    """运行流解析：{script res:// path: [执行过的 test 名]}（仅 Run
    Summary 之前的运行流段；Summary 段用 `-` 前缀会重复计数）。"""
    flow: dict[str, list[str]] = {}
    current: str | None = None
    for line in _strip_ansi(gut_output).splitlines():
        if _SUMMARY_MARKER in line:
            break
        s = line.strip()
        if _SCRIPT_HEADER_RE.match(s):
            current = s
            flow.setdefault(current, [])
        elif current is not None and s.startswith("*"):
            parts = s.split()
            # GUT 运行流 `*` 行不止测试：孤儿节点报告 `* [Class:<Node#id>`
            # 也走同前缀（真机全量 36 特性实证：5 脚本 630 条 vs 154 期望）。
            # 只计数 GUT 命名约定的 test_ 前缀条目，孤儿/杂行不入对账。
            if len(parts) >= 2 and parts[1].startswith("test_") \
                    and parts[1] not in _GUT_HOOKS:
                flow[current].append(parts[1])
    return flow


def _snapshot_main(root: Path) -> dict[str, str]:
    """主 acceptance/ir|generated 字节级快照（.gqt-run-* 私有目录除外）。"""
    snap: dict[str, str] = {}
    for d in (root / "acceptance" / "ir",
              root / "acceptance" / "generated"):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if ".gqt-run-" in rel:
                continue
            snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def reconcile(project_root: str | Path, suite_dir: str | Path,
              keys: list[str], gut_output: str, *,
              replay_ok: bool = False,
              main_snapshot: dict[str, str] | None = None) -> dict:
    """acceptance-suite/1 对账五项。纯检查无副作用（清理归 run_suite_dir）。"""
    root = Path(project_root)
    suite_dir = Path(suite_dir)
    keys = sorted(set(keys))
    gen = suite_dir / "generated"
    expected_files = {f"test_{k}.gd" for k in keys}
    actual_files = {p.name for p in gen.glob("test_*.gd")}
    r1 = actual_files == expected_files
    flow = _executed_scripts(gut_output)
    executed_scripts = sorted(flow)
    manifests = {
        k: json.loads((gen / f"{k}.manifest.json").read_text(encoding="utf-8"))
        for k in keys
    }
    name_to_stems: dict[str, list[str]] = {}
    for k, man in manifests.items():
        for a in man["attribution"]:
            name_to_stems.setdefault(a["test_name"], []).append(k)
    # R2 执行脚本集==生成词干集：脚本段文件名逐一对应 test_<key>.gd
    executed_script_files = {Path(p).name for p in executed_scripts}
    r2 = executed_script_files == expected_files and all(
        Path(p).name in expected_files for p in executed_scripts)
    # R3 计数相等：脚本段内测试条目数（脚本作用域多重度，同名不塌缩）
    entries = [(s, n) for s, names in flow.items() for n in names]
    unknown = sorted({n for _, n in entries if n not in name_to_stems})
    expected_total = sum(len(m["attribution"]) for m in manifests.values())
    r3 = len(entries) == expected_total and not unknown
    executed = {n for _, n in entries}
    if main_snapshot is None:
        main_snapshot = _snapshot_main(root)
    # R4 只管主目录快照一致性——文件集对账是 R1 的职责（tidy L1：去重
    # 首合取，行为等价，X4 守卫用例保持绿即证）。
    r4 = _snapshot_main(root) == main_snapshot
    return {
        "suite_version": SUITES_VERSION,
        "suite_dir": str(suite_dir),
        "keys": keys,
        "rules": {
            "generated_eq_selected": r1,
            "executed_scripts_eq_generated": r2,
            "executed_count_eq_expected": r3,
            "unselected_absent": r4,
            "replay_checked": bool(replay_ok),
        },
        "ok": r1 and r2 and r3 and r4 and bool(replay_ok),
        "generated_files": sorted(actual_files),
        "executed_scripts": executed_scripts,
        "executed_tests": sorted(executed),
        "unknown_executed": unknown,
    }


def _cleanup_suite_dir(suite_dir: Path) -> str:
    parent = suite_dir.parent
    shutil.rmtree(suite_dir)
    # 零残留：父目录若因本管线而创建且现已空 → 一并移除；主 generated
    # 有既有产物（KOL 实树）则 rmdir 失败原样保留。
    try:
        parent.rmdir()
    except OSError:
        pass
    return "removed"


def run_suite_dir(project_root: str | Path, suite_dir: str | Path,
                  timeout_s: int = 120) -> dict:
    """私有目录全生命周期：重放(R5) → 整目录 GUT 执行 → 对账五项 → 清理。

    任一重放失配 = AcceptRunError（先于 GUT，不执行不可信产物）；对账
    不过 = ok=false 且私有目录保留现场；全过 = 清理零残留。
    """
    root = Path(project_root).resolve()
    suite_dir = Path(suite_dir).resolve()
    gen = suite_dir / "generated"
    keys = sorted(p.name.removesuffix(".manifest.json")
                  for p in gen.glob("*.manifest.json"))
    if not keys:
        raise SuiteError(f"private suite dir has no compiled manifests: {gen}")
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise AcceptRunError(f"GUT runner not found: {gut}")
    snapshot = _snapshot_main(root)
    for k in keys:
        replay_check(root, k, suite_dir=suite_dir)
    rel = suite_dir.relative_to(root).as_posix()
    try:
        with GlobalGodotGate(str(root)):
            r = subprocess.run(
                [godot_command(), "--headless", "--path", str(root),
                 "-s", "res://addons/gut/gut_cmdln.gd",
                 "-gdir=res://" + rel + "/generated", "-gexit"],
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(root))
    except subprocess.TimeoutExpired as e:
        raise AcceptRunError(
            f"GUT timed out after {timeout_s}s on private suite dir") from e
    output = _strip_ansi((r.stdout or "") + "\n" + (r.stderr or ""))
    if _looks_like_godot_launch_failure(output):
        tail = "\n".join(output.splitlines()[-10:])
        raise AcceptRunError(
            f"godot launch failed on private suite dir: {tail}")
    record = reconcile(root, suite_dir, keys, output,
                       replay_ok=True, main_snapshot=snapshot)
    if record["ok"]:
        record["cleanup"] = _cleanup_suite_dir(suite_dir)
    else:
        record["cleanup"] = "kept-on-failure"
    return record


def run_acceptance_probe(project_root: str | Path, keys: list[str],
                         timeout_s: int = 120) -> dict:
    """W4 acceptance 成本探针：私有目录管线实测「编译 + 执行」墙钟。"""
    root = Path(project_root)
    selected = sorted(set(keys))
    t0 = time.monotonic()
    suite_dir = create_suite_dir(root)
    compile_keys_to_suite(root, selected, suite_dir)
    compile_s = time.monotonic() - t0
    t1 = time.monotonic()
    record = run_suite_dir(root, suite_dir, timeout_s)
    run_s = time.monotonic() - t1
    return {
        "probe_version": PROBE_VERSION,
        "keys": selected,
        "compile_s": round(compile_s, 3),
        "run_s": round(run_s, 3),
        "extrapolation_assumption": EXTRAPOLATION_ASSUMPTION,
        "suite_ok": record["ok"],
        "reconciliation": record,
    }
