"""Coverage runner: GDScript 行覆盖 + 分支覆盖推导（SEE-1268 M2 / SEE-1312 增强）.

自研插桩方案（GUT 无 coverage API）。SEE-1312 增强：
- 插桩改为 AST 语句节点级（gather_metadata 行段），跨行分组表达式不再 parse error；
- 生成期 re-parse 预检：插桩产物不合法 → run_error 拒绝落盘（不再静默跑假数据）；
- 探针通道健壮化：ID-keyed 数据面（探针写整数 probe_id，路径移出数据面）+ run 唯一
  sink + user:// 优先（win64@WSL 下绕开 9p/UNC 路径脆弱性）+ 追加写语义；
- 哨兵：probes_fired / vacuous——探针未点火或无可执行行显式化，静默 0% 根除；
- 分支覆盖 v1 = 纯推导层（行探针命中 + AST 分支结构），零新探针，observational。

P0' 判定原则落点：机器行覆盖百分比 + 阈值 gate；exit 0=达标 1=未达标。
kill rate 与覆盖率均不进验收证据（owner-order L5 条文）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from gdtoolkit.parser import parser as gdtoolkit_parser

from ..timing import BUDGET_CAP_S, explicit_timing_meta, resolve_budget, suggested_action
# §SPEC-1525-001/002：coverage 启动点与 --path 形态接入 mutation.inject
# 单一解析源（godot_command / godot_project_path）——原硬编码 "godot" 使
# GQT_GODOT_BIN 对 coverage 无效，且路径转换与二进制类型脱钩。
from ..mutation.inject import godot_command
from ..mutation.inject import godot_project_path as _unified_godot_project_path
from ..registry.lines import (
    EXECUTABLE_NODE_NAMES as _EXECUTABLE_NODE_NAMES,
    IF_BRANCH_NODES as _IF_BRANCH_NODES,
    ExecutableLine,
    collect_executable_lines as _collect_executable_lines,
    node_line as _node_line,
    paren_depths as _paren_depths,
    tree_depth_walk as _tree_depth_walk,
)

# 与 mutation runner 同一套判定：godot 进程级失败（-s 脚本没加载起来）≠
# 测试结果。res:// 是唯一可靠的 -s 形式（Revy QA FAIL 实证）。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"

# §SPEC-1420-21：中止判定收紧后错误 marker 不再是必要条件——marker 常量
# 保留供诊断报告引用（abort reason 拼接），判定面唯一依据 = 证据正则。
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)

# 与 mutation runner 同族 token（§SPEC-012）：GUT 测试流证据——运行流
# `* test_x` / 汇总段 `- test_x` / 逐测试 [passed]/[Failed]/[Pending]/[Risky]。
_GUT_RUN_EVIDENCE = re.compile(
    r"(?mi)^[*-]\s+test_\S+|\[(?:passed|failed|pending|risky)\]")


def _looks_like_godot_launch_failure(output: str) -> bool:
    """coverage 侧中止判定（§SPEC-1420-21 收紧）。

    旧口径（marker ∧ 无证据）放行了「godot 加载早期退出」形态：rc≠0、
    快速返回、零 GUT 输出、无任何 marker——被归为 probes_fired=0 哨兵，
    中止原因失真（Archi 实机取证）。新口径：**rc≠0 ∧ 无 GUT 测试流证据**
    → 一律 launch failure（错误 marker 不再是必要条件）；资源告警在场但
    GUT 正常跑（证据正则命中）的场景保持正常分类——保护面不回归。
    """
    return _GUT_RUN_EVIDENCE.search(output) is None


def _godot_project_path(project_root: str) -> str:
    """--path 语义随 detect_binary_kind() 同源（§SPEC-1525-002）：委托
    inject.godot_project_path 统一原语——win64 通道保留既有 /mnt/→Windows
    转换（af728f5 行为逐字节不变）、linux 通道原生不转换；与 mutation
    runner 共用同一份转换原语，勿在此模块再分叉。"""
    return _unified_godot_project_path(project_root)


# user:// 优先：win64@WSL 下项目 res:// 走 UNC 映射（//wsl.localhost/...），
# FileAccess 对该路径的写入可见性/时序脆弱。user:// 是 Godot 管理的应用数据
# 目录，两侧 OS 均为本地可靠文件系统。runner 侧按 user:// 目录约定读取。
_SINK_DIR_MARKER = "user://"

_PROBE_PREFIX = "__qa_cov_probe_"

# 可执行语句的规则/树形节点名（插桩目标）——语句级，不深挖表达式。
# 注意：else_branch/elif_branch 不在此列——它们的头行是 if 结构的一部分
# （`else:` / `elif cond:` 不是独立语句），在其前插同缩进探针会产出语法非法
# 的插桩文件（硬化 反例实证：含 else 的文件 coverage 全体 run_error）。
# 分支执行语义由 _derive_branches 的体首语句行锚定承担。


@dataclass(frozen=True)
class ExecutableLine:
    line: int
    node: str  # node.data（行所属结构，便于诊断）


@dataclass(frozen=True)
class BranchItem:
    line: int | None  # 分支体首条可执行语句行（映射到行探针 id）


@dataclass(frozen=True)
class BranchSet:
    total: int
    items: tuple[BranchItem, ...] = field(default_factory=tuple)


def _tree_depth_walk(node, callback, depth=0, prune=None):
    callback(node, depth)
    if prune is not None and prune(node):
        return
    for child in getattr(node, "children", []):
        _tree_depth_walk(child, callback, depth + 1, prune)


def _node_line(node) -> int | None:
    """gather_metadata 模式下 Tree 的起始行；无元数据（Token/空 meta）返回 None。"""
    meta = getattr(node, "meta", None)
    if meta is not None and not meta.empty and meta.line > 0:
        return meta.line
    return None


def _dedupe_sorted(lines: list[ExecutableLine]) -> list[ExecutableLine]:
    seen = set()
    unique = []
    for l in lines:
        if l.line not in seen:
            seen.add(l.line)
            unique.append(l)
    unique.sort(key=lambda x: x.line)
    return unique


def _paren_depths(src: str) -> list[int]:
    """每行开始时的括号深度（()[]{}，字符串字面量已屏蔽）。

    探针只允许插在【深度==0】的行——深度>0 意味着该行处于括号/参数列表内部，
    在其前插探针会破坏语法（硬化 实机实证：save_manager.gd 的跨行分组
    `(
        result
        . append(...)  ← 该行插探针 → Parse Error
    )` 与 sort_custom( 的 lambda 实参行同炸）。
    """
    depths = []
    depth = 0
    for line in src.splitlines():
        depths.append(depth)
        cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
        cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", "''", cleaned)
        cleaned = re.sub(r"#.*", "", cleaned)
        depth += (cleaned.count("(") + cleaned.count("[") + cleaned.count("{")
                  - cleaned.count(")") - cleaned.count("]") - cleaned.count("}"))
    return depths


def _collect_executable_lines(src: str) -> list[ExecutableLine]:
    """用 gdtoolkit lark（gather_metadata）找出所有可执行语句的【起始行】。

    语句跨多行时只取节点起始行——探针插在语句前，绝不落进表达式内部
    （SEE-1308 实测：探针插进跨行分组表达式内部 → Parse Error）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    lines: list[ExecutableLine] = []
    depths = _paren_depths(src)

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data in _EXECUTABLE_NODE_NAMES:
            ln = _node_line(node)
            if ln and ln <= len(depths) and depths[ln - 1] == 0:
                lines.append(ExecutableLine(line=ln, node=node.data))
            # 括号内部行（depths>0）不是语句起始位置——即使 AST 报告该行为
            # 语句行（如跨行分组表达式 / lambda 实参行），也绝不插桩。

    # 内部类（class_def）整体不插桩（SEE-1367 §SPEC-017 硬化 反例实证）：
    # 探针函数追加在外层类作用域，内部类方法的 self 看不到它们——
    # Godot Parse Error "Function __qa_cov_probe_N() not found in base self"，
    # 目标文件整体拒载（GUT 侧表现为 "does not extend GutTest"），零探针假象。
    # 代价：内部类体（通常为测试桩）不计量——在套件自测量场景可接受。
    _tree_depth_walk(tree, visit, prune=lambda n: getattr(n, "data", None) == "class_def")
    return _dedupe_sorted(lines)


def _derive_branches(src: str) -> BranchSet:
    """SPEC-007：分支覆盖 v1 纯推导——行探针命中 + AST 分支结构，零新探针。

    if_stmt 的每个子分支（if/elif/else）与 match_stmt 的每个 match_branch
    各计 1 分支；分支判定锚定【分支体首条可执行语句行】（硬化 反例实证：
    分支头行探针在条件求值时即点火，false 路径下 if 分支会被误判 covered）。
    分支体无语句（pass/空）时回退分支头行。
    observational：不参与 ok 判定（探针只证明「执行过」，不证明「被断言守护」）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    items: list[BranchItem] = []

    def _branch_body_line(branch_node) -> int | None:
        head = _node_line(branch_node)
        body_first = None
        for desc in _branch_body_nodes(branch_node):
            ln = _node_line(desc)
            if ln is not None and (body_first is None or ln < body_first):
                body_first = ln
        # 体首行若与头行相同（单行体），二者等价；否则用体首行
        return body_first if body_first is not None else head

    def _branch_body_nodes(branch_node):
        children = [c for c in getattr(branch_node, "children", []) if hasattr(c, "data")]
        # 分支头行污染层：if_branch/elif_branch 的 children[0] 是条件表达式、
        # match_branch 的 children[0] 是 pattern——meta.line 均等于分支头行。
        # 不跳过会把「条件求值/模式匹配行」当分支体首行（false 路径下 if 分支
        # 被误判 covered，硬化 反例实证）。
        # else_branch 无该层，全收。
        skip_first = branch_node.data in ("if_branch", "elif_branch", "match_branch")
        body = children[1:] if skip_first else children
        for c in body:
            yield c
            yield from _branch_body_nodes(c)

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data == "if_stmt":
            branch_nodes = (c for c in node.children
                            if hasattr(c, "data") and c.data in _IF_BRANCH_NODES)
        elif node.data == "match_stmt":
            branch_nodes = (c for c in node.children
                            if hasattr(c, "data") and c.data == "match_branch")
        else:
            branch_nodes = ()
        for child in branch_nodes:
            items.append(BranchItem(line=_branch_body_line(child)))
        # 子节点遍历由 _tree_depth_walk 统一处理——visit 内不再手动递归
        # （否则 if_stmt 会被双重遍历，分支重复计数）。

    # 同款 class_def 剪枝：内部类分支体行不在探针面内，计入 branch_total 会
    # 制造结构性永假分支（与探针面不一致）。
    _tree_depth_walk(tree, visit, prune=lambda n: getattr(n, "data", None) == "class_def")
    return BranchSet(total=len(items), items=tuple(items))


def _build_manifest(executable_lines: list[ExecutableLine]) -> list[int]:
    """probe_id（列表下标）→ 源文件行号。ID-keyed 数据面的 runner 侧映射表。"""
    return [l.line for l in executable_lines]


def _probe_calls(src: str, line_to_id: dict[int, int]) -> list[str]:
    """每个可执行语句前插一行探针调用（整数 probe_id）。"""
    out = []
    for idx, line_text in enumerate(src.splitlines(keepends=True), start=1):
        if idx in line_to_id:
            indent = re.match(r"^(\s*)", line_text).group(1)
            out.append(f"{indent}{_PROBE_PREFIX}{line_to_id[idx]}()\n")
        out.append(line_text)
    return out


def _probe_func(sink_name: str, count: int) -> str:
    """文件尾探针函数：wrapper 用 user:// 唯一 sink 追加写（GDScript 侧无环境
    读取——sink 名与 user:// 前缀在生成期以字面量嵌入）。

    探针一律声明为 static：被插桩文件的 static 函数体内不允许调用实例方法
    （GDScript 4 Parse Error "Cannot call non-static function"，SEE-1316
    NumberCompactFormat 全 static util 实测——探针非 static 时整个脚本
    解析失败，probes_fired 恒 0）。static 探针在实例/静态两种语境下均合法。
    """
    # 追加写语义：READ_WRITE + seek_end——裸 WRITE 每次命中重开会截断此前
    # 全部命中（SEE-1312 代码走查实测缺陷）。
    code = (
        f"\n\n# qa-toolkit coverage probe (auto-instrumented, never in production)\n"
        f"static func {_PROBE_PREFIX}wrapper(probe_id):\n"
        f"\tvar f = FileAccess.open(\"{_SINK_DIR_MARKER}{sink_name}\", FileAccess.READ_WRITE)\n"
        f"\tif f == null:\n"
        f"\t\tf = FileAccess.open(\"{_SINK_DIR_MARKER}{sink_name}\", FileAccess.WRITE)\n"
        f"\telse:\n"
        f"\t\tf.seek_end()\n"
        f"\tif f:\n"
        f"\t\tf.store_line(str(probe_id))\n"
        f"\t\tf.close()\n\n"
    )
    for i in range(count):
        code += (
            f"static func {_PROBE_PREFIX}{i}():\n"
            f"\t{_PROBE_PREFIX}wrapper({i})\n"
        )
    return code


def _instrument(src: str, sink_name: str) -> tuple[str, list[int]]:
    """AST 语句级插桩：每个可执行语句前插一行探针调用（整数 probe_id），
    文件尾追加探针函数。返回 (插桩源码, probe_id→行号 manifest)。"""
    manifest = _build_manifest(_collect_executable_lines(src))
    line_to_id = {line: i for i, line in enumerate(manifest)}
    body = "".join(_probe_calls(src, line_to_id))
    return body + _probe_func(sink_name, len(manifest)), manifest


def run_coverage(
    file_path: str,
    project_root: str,
    min_percent: float = 80.0,
    timeout_s: int = 120,
    timing_store=None,
    tests_glob: str | None = None,
    evidence_out: str | None = None,
) -> dict:
    """对单个 .gd 文件算行覆盖（跑 GUT 后统计命中行）并出统一 JSON 契约。

    min_percent 阈值 gate（P0' 机器判定：覆盖率≥阈值=ok）。
    返回 {"tool":"coverage", "ok", "summary", "failures"}；
    ok=True 表示覆盖率达标。`evidence_out`（§SPEC-025/026，可选）开启
    coverage-evidence/2 生命周期：per-run UUID sink + run 记录 + bundle
    落盘即清 sink；不传则 stdout 契约与 011b87d 逐字节一致（零漂移）。
    """
    from ..source_io import anchor_to_project_root, read_gd_source

    # §SPEC-610（SEE-1576 R-5）：与 mutation 两处检查点同口径——相对目标
    # 锚定 --project-root（绝对路径直过）。
    path = Path(anchor_to_project_root(file_path, project_root))
    if not path.is_file():
        raise FileNotFoundError(
            f"coverage target not found: {file_path} (anchored: {path})")

    from .evidence import (
        build_bundle,
        build_run_record,
        clear_sink,
        evidence_dir_for,
        make_run_uuid,
        sink_name_for,
        update_run_record,
        write_bundle,
        write_run_record,
    )
    original_src, read_error = read_gd_source(str(path))
    if read_error is not None:
        # §SPEC-028: single-file reads fail structurally, never crash
        return {
            "tool": "coverage",
            "ok": False,
            "summary": {
                "file": str(path),
                "run_error": True,
                "error": read_error,
            },
            "failures": [{"reason": read_error}],
        }
    backup = original_src

    executable_lines = _collect_executable_lines(original_src)
    total_lines = len(executable_lines)

    if total_lines == 0:
        # §SPEC-026: vacuous bundles land too (no sink was created yet —
        # zero executable lines means zero probes, nothing to recover)
        if evidence_out:
            uuid0 = make_run_uuid()
            evidence_dir_for(evidence_out).mkdir(parents=True, exist_ok=True)
            write_bundle(
                evidence_dir_for(evidence_out),
                build_bundle(
                    run_uuid=uuid0, file_path=str(path), gd_source=original_src,
                    tests_glob=tests_glob, hits_lines=[], probes_fired=0,
                    gut_ok=True, run_error=False, timeout=False, zero_tests=False,
                    vacuous=True,
                ),
            )
        return _vacuous_result(str(path), min_percent)

    # SEE-1356 L1：coverage 同 timing store 接入——缓存只喂预算锚，GUT 永远
    # 实测；实测耗时回写采样。
    timing_meta = explicit_timing_meta()
    effective_timeout = timeout_s
    if timing_store is not None:
        hist = timing_store.read()
        effective_timeout, timing_meta["timing_source"] = resolve_budget(
            timeout_s, hist["last"] if hist else None)
        timing_meta["samples_n"] = hist["samples_n"] if hist else 0
        timing_meta["baseline_p90"] = hist["p90"] if hist else None

    # run 唯一 sink（§SPEC-025）：per-run UUID——同进程顺序 run、并发进程、
    # CLI 与 risk 内嵌路径全部天然互斥（修 D-候选-1：PID sink 在同进程
    # 顺序 run 间累积命中，窄 glob 结果被上一 run 污染）。
    run_uuid = make_run_uuid()
    sink_name = sink_name_for(run_uuid)
    evidence_dir = evidence_dir_for(evidence_out) if evidence_out else None
    # §SPEC-030 (D-性能-1): build the registry + probe-manifest/2 ONCE on the
    # evidence path — run record, bundle and recover all consume this single
    # parse; build_bundle receives them instead of re-deriving (which doubled
    # the lark parse cost per evidence run)
    evidence_registry = None
    evidence_probe_manifest = None
    if evidence_dir is not None:
        from ..registry.functions import build_file_registry
        from ..registry.manifest import build_probe_manifest_v2

        evidence_registry = build_file_registry(original_src, file_label=str(path))
        evidence_probe_manifest = build_probe_manifest_v2(original_src, evidence_registry)
    instrumented, manifest = _instrument(original_src, sink_name)

    # 生成期 re-parse 预检：插桩产物不合法 = 插桩器 bug → 显式 run_error，
    # 拒绝落盘跑假数据（把 parse error 从静默假 0% 变成生成期失败）。
    try:
        gdtoolkit_parser.parse(instrumented)
    except Exception as e:
        return _reparse_error_result(str(path), total_lines, min_percent, e)

    if evidence_dir is not None:
        from .evidence import sha256_text
        run_record = build_run_record(
            run_uuid=run_uuid,
            file_path=str(path),
            gd_source=original_src,
            tests_glob=tests_glob,
            probe_manifest=evidence_probe_manifest,
        )
        run_record["sink_path"] = str(
            evidence_dir / sink_name
        )  # record BOTH the sink name and a recovery-side path candidate
        run_record["probe_plan_inline_note"] = (
            "probe manifest inline carries fid materialization; the recovery "
            "path re-attributes raw probe ids exactly like the online path"
        )
        write_run_record(evidence_dir, run_record)

    t0 = time.monotonic()
    gut_outcome = _run_gut_for_coverage(path, project_root, backup, instrumented,
                                        total_lines, min_percent, effective_timeout,
                                        tests_glob=tests_glob)
    gut_elapsed = time.monotonic() - t0
    if gut_outcome is not None:
        # §SPEC-026: failure bundles land too (sink KEPT for --recover), with
        # the abort class mapped into the sentinels
        if evidence_dir is not None:
            summary = gut_outcome.get("summary", {})
            bundle = build_bundle(
                run_uuid=run_uuid,
                file_path=str(path),
                gd_source=original_src,
                tests_glob=tests_glob,
                registry=evidence_registry,
                probe_manifest=evidence_probe_manifest,
                hits_lines=[],
                probes_fired=0,
                gut_ok=False,
                run_error=bool(summary.get("run_error")),
                timeout=bool(summary.get("timeout")),
                zero_tests=False,
                vacuous=False,
            )
            write_bundle(evidence_dir, bundle)
            update_run_record(
                evidence_dir, run_uuid,
                status="run_error" if summary.get("run_error") else "timeout",
            )
        return gut_outcome
    if timing_store is not None:
        timing_store.record(gut_elapsed)

    hits, probes_fired = _read_hits(project_root, sink_name, manifest)

    if evidence_dir is not None:
        bundle = build_bundle(
            run_uuid=run_uuid,
            file_path=str(path),
            gd_source=original_src,
            tests_glob=tests_glob,
            registry=evidence_registry,
            probe_manifest=evidence_probe_manifest,
            hits_lines=sorted(hits),
            probes_fired=probes_fired,
            gut_ok=True,
            run_error=False,
            timeout=False,
            zero_tests=False,
            vacuous=False,
        )
        write_bundle(evidence_dir, bundle)
        update_run_record(evidence_dir, run_uuid, status="bundle_written")
        clear_sink(None, project_root, sink_name)

    # 哨兵：GUT 正常退出但探针从未点火 ≠ 真实 0% 覆盖——数据不可信。
    if probes_fired == 0:
        # §SPEC-026: no-probe bundles land (zero_tests/probe-transport failure
        # is the consumer's lane call); sink KEPT — the hits file is the only
        # forensic evidence for why probes never fired
        if evidence_dir is not None:
            bundle = build_bundle(
                run_uuid=run_uuid,
                file_path=str(path),
                gd_source=original_src,
                tests_glob=tests_glob,
                registry=evidence_registry,
                probe_manifest=evidence_probe_manifest,
                hits_lines=sorted(hits),
                probes_fired=0,
                gut_ok=True,
                run_error=False,
                timeout=False,
                zero_tests=False,
                vacuous=False,
            )
            write_bundle(evidence_dir, bundle)
            update_run_record(evidence_dir, run_uuid,
                              status="no_probe", zero_tests=False)
        return _no_probe_result(str(path), total_lines, min_percent)

    # SEE-1420 P2（L3/D7）：成功 run 的行级事实落盘（自家 JSON 超集，
    # stdout 契约不动）——mutation --reuse-coverage 的唯一数据源。
    from ..mutation.coverlink import save_coverage
    from ..mutation.identity import identity_pack
    from ..mutation.inject import detect_binary_kind
    covered_pct = 100.0 * len(hits) / total_lines if total_lines else 100.0
    # D3：可插桩行集随行级事实同盘落盘——mutation 前置过滤的唯一合法跳过
    # 锚（不可插桩行 ≠ uncovered）。
    # SEE-1550 L1：身份包随行级事实入档——mutation prefilter 要求
    # store fresh ∧ 身份包全匹配。
    save_coverage(hits, total_lines, project_root, str(path), tests_glob,
                  coverage_percent=round(covered_pct, 2),
                  instrumentable_lines={l.line for l in executable_lines},
                  identity=identity_pack(project_root, detect_binary_kind()))

    return _coverage_report(str(path), original_src, executable_lines, hits,
                            probes_fired, min_percent, timing_meta)


def _vacuous_result(file: str, min_percent: float) -> dict:
    """零可执行行：空集覆盖不是满分证据——显式 vacuous 标记。"""
    return {
        "tool": "coverage",
        "ok": True,
        "summary": {"file": file, "total_lines": 0, "covered_lines": 0,
                    "coverage_percent": 100.0, "min_percent": min_percent,
                    "vacuous": True,
                    "note": "no executable lines"},
        "failures": [],
    }


def _no_probe_result(file: str, total_lines: int, min_percent: float) -> dict:
    """哨兵中止契约：探针未点火（sink 缺失/空）→ 数据不可信。"""
    return {
        "tool": "coverage",
        "ok": False,
        "summary": {"file": file, "total_lines": total_lines,
                    "covered_lines": 0, "coverage_percent": 0.0,
                    "min_percent": min_percent,
                    "probes_fired": 0,
                    "run_error": True,
                    "error": "no coverage probe fired — sink missing/empty"},
        "failures": [{"reason": (
            "coverage probe never fired (hits sink missing or empty) — "
            "instrumentation or sink transport failed; data untrustworthy"
        )}],
    }


def _reparse_error_result(file: str, total_lines: int, min_percent: float, err) -> dict:
    return {
        "tool": "coverage",
        "ok": False,
        "summary": {"file": file, "total_lines": total_lines,
                    "covered_lines": 0, "coverage_percent": 0.0,
                    "min_percent": min_percent,
                    "run_error": True,
                    "error": f"instrumented source failed to parse: {err}"},
        "failures": [{"reason": (
            f"instrumentation produced unparseable source "
            f"(instrumenter bug, no GUT run attempted): {err}"
        )}],
    }


def _gut_abort_result(path, total_lines: int, min_percent: float,
                      error: str, reason: str, run_error: bool = False,
                      hint: str | None = None) -> dict:
    err_summary = {"file": str(path), "total_lines": total_lines,
                   "covered_lines": 0, "coverage_percent": 0.0,
                   "min_percent": min_percent,
                   "run_error": True,
                   "error": error}
    if hint:
        err_summary["suggested_action"] = hint
    return {
        "tool": "coverage",
        "ok": False,
        "summary": err_summary if run_error else {
                   "file": str(path), "total_lines": total_lines,
                   "covered_lines": 0, "coverage_percent": 0.0,
                   "min_percent": min_percent, "error": error},
        "failures": [{"reason": reason}],
    }


def _gut_selection_args(tests_glob: str | None) -> list[str]:
    """tests_glob（SEE-1367 §SPEC-017）：受影响测试子集，替代全量 -gdir 以支持
    逐套件归因度量。目录走 -gdir；.gd 文件走 -gselect（GUT 9.6 实测 -gtest
    会全量展开，只有 -gselect 能精确过滤到单脚本，SEE-1316 同款结论）。"""
    gdir = tests_glob if tests_glob else "res://tests/"
    if gdir.endswith(".gd"):
        return ["-gdir=res://tests/", f"-gselect={Path(gdir).stem}"]
    return [f"-gdir={gdir}"]


def _run_gut_for_coverage(path, project_root: str, backup: str, instrumented: str,
                          total_lines: int, min_percent: float, timeout_s: int,
                          tests_glob: str | None = None) -> dict | None:
    """落盘插桩文件 → headless GUT → 恢复原文件。返回 None 表示可继续统计命中；
    否则返回已完成的中止契约（timeout / 启动失败 / GUT runner 缺失）。"""
    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        path.write_text(instrumented, encoding="utf-8")
        gut_tail = ""
        try:
            gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
            if not os.path.isfile(gut_fs):
                raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
            try:
                # SEE-1400 §SPEC-003：coverage 与 mutation 同受跨进程槽位闸。
                from ..mutation.godot_gate import GlobalGodotGate
                with GlobalGodotGate(project_root):
                    r = subprocess.run(
                        [godot_command(), "--headless", "--path", _godot_project_path(project_root),
                         "-s", GUT_SCRIPT_RES_PATH, *_gut_selection_args(tests_glob), "-gexit"],
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                        cwd=project_root,
                    )
                gut_tail = (r.stdout + r.stderr)[-1500:]
            except subprocess.TimeoutExpired:
                # SEE-1356 L1 (§SPEC-L1-03)：预算超时附机器可执行补救指令。
                return _gut_abort_result(
                    path, total_lines, min_percent, "GUT timed out",
                    f"GUT timed out after {timeout_s}s",
                    run_error=True,
                    hint=suggested_action(BUDGET_CAP_S))
            finally:
                path.write_text(backup, encoding="utf-8")
        except Exception:
            path.write_text(backup, encoding="utf-8")
            raise

        if r.returncode != 0 and _looks_like_godot_launch_failure(gut_tail):
            return _gut_abort_result(
                path, total_lines, min_percent,
                "godot launch failed — coverage data untrustworthy",
                "godot launch failed (tests did not run): "
                f"{gut_tail.strip()[:200]}",
                run_error=True)
    return None


def _read_hits(project_root: str, sink_name: str, manifest: list[int]) -> tuple[set[int], int]:
    """读取命中：user:// sink 优先（探针实际写入侧），项目根仅作非 user:// 环境回退。

    顺序不可反——win64@WSL 下项目根在 9p/UNC 上，且 pid 复用时残留的陈旧项目根
    sink 会遮蔽新鲜 user:// 数据（硬化 反例实证：probes_fired 哨兵被陈旧计数骗过）。

    返回 (命中行号集, probes_fired)。ID-keyed 数据面：probe_id → manifest 行号；
    重复 id 幂等去重，越界 id 忽略且【不计入 probes_fired】——越界 id 是传输错误
    信号，不能充当「探针点火」证据。
    """
    user_sink = _resolve_user_sink(project_root, sink_name)
    hits_raw: list[int] = []
    for sink in (user_sink, os.path.join(project_root, sink_name)):
        if sink and os.path.isfile(sink):
            with open(sink, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if ln.isdigit():
                        hits_raw.append(int(ln))
            break

    valid_ids = set(range(len(manifest)))
    hits = {manifest[pid] for pid in hits_raw if pid in valid_ids}
    return hits, sum(1 for pid in hits_raw if pid in valid_ids)


def _coverage_report(file: str, original_src: str, executable_lines, hits: set[int],
                     probes_fired: int, min_percent: float,
                     timing_meta: dict | None = None) -> dict:
    total_lines = len(executable_lines)
    covered = len(hits)
    pct = round(100.0 * covered / total_lines, 2) if total_lines else 100.0
    ok = pct >= min_percent

    failures = []
    if not ok:
        uncovered_lines = sorted({l.line for l in executable_lines} - hits)
        failures.append({
            "reason": f"coverage {pct}% below threshold {min_percent}%",
            "covered": covered,
            "total": total_lines,
            "uncovered_lines": uncovered_lines[:20],  # 截断展示
        })

    # 分支覆盖 v1 推导层（observational——不进 ok 判定）。
    branches = _derive_branches(original_src)
    branches_covered = sum(
        1 for b in branches.items if b.line is not None and b.line in hits
    )
    branch_pct = (
        round(100.0 * branches_covered / branches.total, 2) if branches.total else 100.0
    )

    summary_extra = timing_meta or explicit_timing_meta()
    return {
        "tool": "coverage",
        "ok": ok,
        "summary": {
            "file": file,
            "total_lines": total_lines,
            "covered_lines": covered,
            "coverage_percent": pct,
            "min_percent": min_percent,
            "probes_fired": probes_fired,
            "branch_total": branches.total,
            "branch_covered": branches_covered,
            "branch_coverage_percent": branch_pct,
            # SEE-1546 M2: per-line hits are the only source for per-function
            # attribution in the risk shadow report — exposing them keeps the
            # risk layer from re-running the same instrumentation twice.
            "hits_lines": sorted(hits),
            **summary_extra,
        },
        "failures": failures,
    }


def _windows_appdata_from_wsl() -> str | None:
    """WSL 下探测 Windows APPDATA 挂载点（win64 godot 的 user:// 落点）。

    返回 /mnt/<drive>/Users/<user>/AppData/Roaming 形式路径；非 WSL 环境返回 None。
    """
    for drive in ("c", "d", "e"):
        base = Path(f"/mnt/{drive}/Users")
        if not base.is_dir():
            continue
        try:
            users = [p for p in base.iterdir()
                     if p.is_dir() and (p / "AppData" / "Roaming" / "Godot").is_dir()]
        except OSError:
            continue
        if users:
            return str(users[0] / "AppData" / "Roaming")
    return None


def _resolve_user_sink_dir(project_root: str) -> str | None:
    """user:// sink DIRECTORY for the project (prune-sinks sweeps here)."""
    sink = _resolve_user_sink(project_root, "qa-coverage-hits-probe.txt")
    if sink:
        from pathlib import Path as _P
        return str(_P(sink).parent)
    return None


def _resolve_user_sink(project_root: str, sink_name: str) -> str | None:
    """定位 user:// sink 的 OS 路径（win64/Linux 两侧均可靠）。

    project 名取自 project.godot 的 config/name（缺省回退目录名）。
    找不到 app_userdata 目录时返回 None（读取端回退项目根 sink）。
    """
    project_name = None
    pg = Path(project_root) / "project.godot"
    if pg.is_file():
        m = re.search(r'config/name\s*=\s*"([^"]+)"', pg.read_text(encoding="utf-8"))
        if m:
            project_name = m.group(1)
    if not project_name:
        project_name = Path(project_root).resolve().name

    candidates = [
        os.path.expanduser(f"~/.local/share/godot/app_userdata/{project_name}"),
        os.path.expanduser(f"~/Library/Application Support/Godot/app_userdata/{project_name}"),
        os.path.join(os.environ.get("APPDATA", ""), "Godot", "app_userdata", project_name),
    ]
    # win64 godot 跑在 WSL 侧：探针写进 Windows APPDATA（/mnt/c/Users/<user>/...），
    # Linux 侧 runner 必须跨过去读——否则 sink 永远"不存在"，探针点火被误判为 0
    # （硬化 实机实证：sink 写满 23940 行命中却读不到）。
    appdata_win = _windows_appdata_from_wsl()
    if appdata_win:
        candidates.append(os.path.join(
            appdata_win, "Godot", "app_userdata", project_name))
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, sink_name)):
            return os.path.join(c, sink_name)
    return None
