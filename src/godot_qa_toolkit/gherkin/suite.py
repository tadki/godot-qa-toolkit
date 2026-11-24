"""gherkin 私有目录管线（§SPEC-035，gherkin-suite/1）。

选择在编译期完成：compile 把选中 keys 写进一次性私有目录
`gherkin/generated/.gqt-run-<uuid>/`（事先为空，非空即拒），run 对该
目录整目录执行——目录即选择集，绕开 GUT -gselect 子串碰撞（选 {a} 不会
误吸 test_ab.gd）。执行前后做对账五项：

  R1 生成数==选中数          generated_eq_selected
  R2 执行脚本集==生成词干集  executed_scripts_eq_generated
  R3 计数相等                executed_count_eq_expected
  R4 未选/旧 generated 缺席  unselected_absent（含主目录字节级快照比对）
  R5 私有目录重放语义        replay_checked

五项全过 → 清理零残留（私有目录移除；父目录为本管线创建且已空才一并
移除，KOL 主 generated 有既有产物则原样保留）；任一不过 → 私有目录保留
现场、ok=false。主 gherkin/ir|generated 全程只读不碰。W4 成本探针
（run_gherkin_probe）实测「编译 + 执行」墙钟，产出
performance.gherkin_probe 记录。

SEE-1624：features 只从 `addons/gqt_gherkin/features` 单根解析（缺根/
空集 fail-loud）；run_suite_dir 预检（addon validator 拉起一次）先于
业务执行。
"""

from __future__ import annotations
from ..digest import sha256_file

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from .addon import (SUITE_OWNER_MARKER, AddonError, compile_preflight,
                    features_root, is_owned_suite_dir, normalize_feature_key,
                    resolve_feature, run_validator_preflight)
from .isolation import isolated_godot_env
from .compile import compile_feature
from .run import GherkinRunError, _strip_ansi, replay_check
from ..mutation.godot_gate import GlobalGodotGate
from ..mutation.gutrun import _looks_like_godot_launch_failure
from ..mutation.inject import godot_command, spawn_godot

SUITES_VERSION = "gherkin-suite/1"
PROBE_VERSION = "gherkin-probe/1"
EXTRAPOLATION_ASSUMPTION = "linear-in-feature-count"


class SuiteError(Exception):
    """私有目录生命周期违例（非空创建、未知 key、清理失败等）。"""


def create_suite_dir(project_root: str | Path, *,
                     run_uuid: str | None = None) -> Path:
    """创建一次性私有目录 `<root>/gherkin/generated/.gqt-run-<uuid>/`。

    事先为空是硬约束：同路径已存在 = SuiteError（绝不复用旧目录，防旧
    generated 混入选择集）。
    """
    root = Path(project_root)
    token = run_uuid or uuid.uuid4().hex[:12]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", str(token)):
        raise SuiteError(
            f"private suite token must be a short path-safe name: {token!r}")
    d = root / "gherkin" / "generated" / f".gqt-run-{token}"
    if d.exists():
        raise SuiteError(
            f"private suite dir already exists (must be created empty): {d}")
    d.mkdir(parents=True)
    # 所有权标记：清理只删带此标记的目录（调用方目录永不被删）。
    # 所有权标记内容 = 本目录名（绑定身份）：复制到别处的 marker 内容不匹配
    # → 调用方目录永不被删（§SPEC-019 CRITICAL-1）。
    (d / SUITE_OWNER_MARKER).write_text(d.name + "\n", encoding="utf-8")
    return d


# SEE-1624：features 解析收口到 addon 单根（缺根/空集/未知 key 均具名
# fail-loud），此处不再持有旧布局常量。

def compile_keys_auto_suite(project_root: str | Path,
                            keys: list[str]) -> dict:
    """自动创建私有套件并编译选中 keys（§SPEC-016 / C1-R2）。

    纪律：键解析（单根 features）与 schema 纯解析握手**先于任何目录
    创建**；编译期任一异常回收本函数创建的目录后重抛——失败路径零
    `.gqt-run-*` 残留（QA DEF-1/1b/1c）。显式 --out-dir 场景由调用方
    持有目录，不走本函数。"""
    root = Path(project_root)
    selected = sorted({normalize_feature_key(k) for k in keys})
    if not selected:
        raise SuiteError("no keys parsed")
    compile_preflight(root)          # AddonError：schema 缺/损/漂移等
    for k in selected:
        resolve_feature(root, k)     # AddonError：未知 key / 缺根 / 空集
    suite_dir = create_suite_dir(root)
    try:
        return compile_keys_to_suite(root, selected, suite_dir)
    except BaseException:
        # 编译中失败（含 parse 错误、写入异常）：回收已建目录，零残留。
        try:
            _cleanup_suite_dir(suite_dir, root)
        except OSError:
            pass
        raise


def compile_keys_to_suite(project_root: str | Path, keys: list[str],
                          suite_dir: str | Path) -> dict:
    """把选中 keys 逐个编译进私有目录（out_dir 分支），返回选择集记录。

    编译期完成选择：只有选中 keys 的产物进目录——目录即选择集。
    """
    root = Path(project_root)
    suite_dir = Path(suite_dir)
    selected = sorted({normalize_feature_key(k) for k in keys})
    manifests: dict[str, dict] = {}
    for key in selected:
        f = resolve_feature(root, key)
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
    """主 gherkin/ir|generated 字节级快照（.gqt-run-* 私有目录除外）。"""
    snap: dict[str, str] = {}
    for d in (root / "gherkin" / "ir",
              root / "gherkin" / "generated"):
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if ".gqt-run-" in rel:
                continue
            snap[rel] = sha256_file(p)
    return snap


def reconcile(project_root: str | Path, suite_dir: str | Path,
              keys: list[str], gut_output: str, *,
              replay_ok: bool = False,
              main_snapshot: dict[str, str] | None = None) -> dict:
    """gherkin-suite/1 对账五项。纯检查无副作用（清理归 run_suite_dir）。"""
    root = Path(project_root)
    suite_dir = Path(suite_dir)
    # key 归一：编译产物按归一键命名（my-demo → my_demo），对账必须用同一
    # 口径读 manifest/生成物（§SPEC-019 HIGH-4：原按原 key 读 → 崩）。
    keys = sorted({normalize_feature_key(k) for k in keys})
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


def ensure_suite_dir_in_project(project_root: str | Path,
                                suite_dir: str | Path) -> Path:
    """入口 containment（§SPEC-019 DEF-2）：私有套件目录必须在**项目内**。

    在重放/validator/Godot 之前调用：项目外路径具名失败（SuiteError →
    CLI 单 JSON、无 traceback），不读取 caller 文件。返回解析后的路径。"""
    try:
        root = Path(project_root).resolve()
        sd = Path(suite_dir).resolve()
    except (OSError, RuntimeError) as e:
        # RuntimeError = symlink loop：具名域错误，不得裸逃逸破坏单 JSON 契约。
        raise SuiteError(
            f"--suite-dir/project root cannot be resolved "
            f"({type(e).__name__}: {e}) — symlink loops are rejected") from e
    try:
        sd.relative_to(root)
    except ValueError:
        raise SuiteError(
            f"--suite-dir is outside the project root: {sd} — a private "
            f"suite dir must live under {root} (caller-owned dirs outside "
            f"the project are never touched by gqt)") from None
    return sd


def _cleanup_suite_dir(suite_dir: Path, project_root: str | Path) -> str:
    sd = Path(suite_dir)
    if not is_owned_suite_dir(sd, project_root):
        # 调用方持有目录：永不清空/删除（可能含调用方文件）。
        return "kept-caller-owned"
    parent = sd.parent
    shutil.rmtree(sd)
    # 零残留：父目录（受管的 gherkin/generated）若已空 → 一并移除；
    # 主 generated 有既有产物（KOL 实树）则 rmdir 失败原样保留。
    try:
        parent.rmdir()
    except OSError:
        pass
    return "removed"


def run_suite_dir(project_root: str | Path, suite_dir: str | Path,
                  timeout_s: int = 120) -> dict:
    """私有目录全生命周期：重放(R5) → 整目录 GUT 执行 → 对账五项 → 清理。

    任一重放失配 = GherkinRunError（先于 GUT，不执行不可信产物）；对账
    不过 = ok=false 且私有目录保留现场；全过 = 清理零残留。
    SEE-1624 §SPEC-003：入口预检（addon validator 拉起一次）先于重放与
    业务执行。
    """
    # 入口 containment 必须先于一切读取与 Godot 拉起（DEF-2）：项目外
    # 路径具名失败，不 glob、不 replay、不拉起 validator。ensure 同时把
    # symlink loop 转成具名 SuiteError（不得裸 RuntimeError 逃逸）。
    suite_dir = ensure_suite_dir_in_project(project_root, suite_dir)
    root = Path(project_root).resolve()
    gen = suite_dir / "generated"
    keys = sorted(p.name.removesuffix(".manifest.json")
                  for p in gen.glob("*.manifest.json"))
    if not keys:
        raise SuiteError(f"private suite dir has no compiled manifests: {gen}")
    gut = root / "addons" / "gut" / "gut_cmdln.gd"
    if not gut.is_file():
        raise GherkinRunError(f"GUT runner not found: {gut}")
    # SEE-1624 §SPEC-003/016 次序：重放（含纯解析握手，零 Godot）先行，
    # validator 拉起紧随其后、业务 GUT 之前。
    snapshot = _snapshot_main(root)
    for k in keys:
        replay_check(root, k, suite_dir=suite_dir)
    try:
        run_validator_preflight(root)
    except AddonError as e:
        raise GherkinRunError(f"gherkin preflight failed: {e}") from e
    rel = suite_dir.relative_to(root).as_posix()
    try:
        # 一次性隔离环境（§SPEC-019 HIGH-3）：GUT 执行不得写真实用户目录。
        with isolated_godot_env() as iso_env:
            with GlobalGodotGate(str(root)):
                r = spawn_godot(
                    [godot_command(), "--headless", "--path", str(root),
                     "-s", "res://addons/gut/gut_cmdln.gd",
                     "-gdir=res://" + rel + "/generated", "-gexit"],
                    capture_output=True, text=True, timeout=timeout_s,
                    cwd=str(root), env=iso_env)
    except subprocess.TimeoutExpired as e:
        raise GherkinRunError(
            f"GUT timed out after {timeout_s}s on private suite dir") from e
    output = _strip_ansi((r.stdout or "") + "\n" + (r.stderr or ""))
    if _looks_like_godot_launch_failure(output):
        tail = "\n".join(output.splitlines()[-10:])
        raise GherkinRunError(
            f"godot launch failed on private suite dir: {tail}")
    record = reconcile(root, suite_dir, keys, output,
                       replay_ok=True, main_snapshot=snapshot)
    if record["ok"]:
        record["cleanup"] = _cleanup_suite_dir(suite_dir, root)
    else:
        record["cleanup"] = "kept-on-failure"
    return record


def run_gherkin_probe(project_root: str | Path, keys: list[str],
                         timeout_s: int = 120) -> dict:
    """W4 gherkin 成本探针：私有目录管线实测「编译 + 执行」墙钟。

    §SPEC-016 / C1-R2：编译前失败（缺根/未知 key/schema）零目录创建；
    编译后、业务 GUT 前的失败（重放/validator 红）回收本探针创建的私有
    目录——probe 入口与 measure 核心路径同纪律（QA DEF-2）。"""
    root = Path(project_root)
    selected = sorted({normalize_feature_key(k) for k in keys})
    t0 = time.monotonic()
    try:
        compile_rec = compile_keys_auto_suite(root, selected)
    except (AddonError, SuiteError) as e:
        # 诊断面具名降级（reason 落 error 键）：发现面违例不冒充测量
        # 成功，也不向 CLI 裸抛；目录未创建（helper 保证）或已回收。
        return {"probe_version": PROBE_VERSION, "keys": selected,
                "suite_ok": False, "error": str(e)}
    suite_dir = Path(compile_rec["suite_dir"])
    compile_s = time.monotonic() - t0
    t1 = time.monotonic()
    try:
        record = run_suite_dir(root, suite_dir, timeout_s)
    except (SuiteError, GherkinRunError):
        # 业务 GUT 前失败（重放/预检）：探针创建的目录零残留后按原
        # 异常路径上抛（CLI JSON 出口具名披露）。
        try:
            _cleanup_suite_dir(suite_dir, root)
        except OSError:
            pass
        raise
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
