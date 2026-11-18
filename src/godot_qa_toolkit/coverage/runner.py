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

import hashlib
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
from ..mutation.inject import godot_command, spawn_godot
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

from .instrument import (
    _attr_probe_block_text,
    _attr_sink_name,
    _attr_var_name,
    _blocks_from_execs,
    _build_manifest,
    _call_arg_span,
    _call_receiver,
    _collect_executable_lines,
    _dedupe_sorted,
    _derive_branches,
    _find_call_paren,
    _flag_probe_block,
    _flag_var_name,
    _flag_var_suffix,
    _instrument,
    _instrument_blocks,
    _match_paren,
    _node_line,
    _paren_depths,
    _probe_calls,
    _probe_func,
    _rewrite_call_sites,
    _tree_depth_walk,
)

def _err_envelope(file: str, message: str, *,
                  min_percent: float = 80.0) -> dict:
    """失败信封：保留文档化字段（total/covered/pct/min_percent/probes），
    全部为“无可信数据”的零值 + `run_error=True`，不伪造成功数据
    （§SPEC-022/025 错误契约）。"""
    return {"tool": "measure.coverage", "ok": False,
            "summary": {"file": file, "total_lines": 0, "covered_lines": 0,
                        "coverage_percent": 0.0, "min_percent": min_percent,
                        "probes_fired": 0, "probes_source": "none",
                        "run_error": True, "error": message},
            "failures": [{"reason": message}]}

def _write_failure_evidence(evidence_out, file_path, gd_source, tests_glob,
                            message) -> "str | None":
    """失败也产出 coverage-evidence/2（gut_ok=False / run_error=True /
    hits_lines=[]），供 `measure risk` 逐文件降级为 declared `run_error`，
    不整批中止（§SPEC-025/027）。写失败返回具名原因。"""
    if not evidence_out:
        return None
    try:
        from .evidence import (build_bundle, evidence_dir_for, make_run_uuid,
                               write_bundle)
        d = evidence_dir_for(evidence_out)
        d.mkdir(parents=True, exist_ok=True)
        try:
            bundle = build_bundle(
                run_uuid=make_run_uuid(), file_path=file_path,
                gd_source=gd_source, tests_glob=tests_glob, hits_lines=[],
                probes_fired=0, gut_ok=False, run_error=True, timeout=False,
                zero_tests=False, vacuous=False)
        except Exception:
            # gd_source 本身不可读/不可解析（read-error 等）时，退化为最小
            # 失败 bundle——仍携带 risk 消费所需的 C/I/sentinels/file_state。
            import hashlib
            uuid = make_run_uuid()
            bundle = {
                "run_uuid": uuid, "file_state": "bundle_only",
                "recovered": False,
                "target": {"path": file_path,
                           "sha256": hashlib.sha256(
                               (gd_source or "").encode()).hexdigest()},
                "sentinels": {"gut_ok": False, "run_error": True,
                              "timeout": False, "zero_tests": False,
                              "vacuous": False, "probes_fired": 0,
                              "transport_anomaly": 0},
                "C": [], "I": [], "T": [], "entry_states": [],
                "probe_manifest": {"inline": {"probe_plan": [],
                                              "per_function": []}},
            }
        write_bundle(d, bundle)
        return None
    except Exception as e:  # noqa: BLE001 — 具名失败，不静默
        return f"failure-evidence write failed: {type(e).__name__}: {e}"

def _write_mapping_evidence(evidence_out, file_path, gd_source, tests_glob,
                            covered, total, probes) -> "str | None":
    """由行级映射投影生成 coverage-evidence/2（§SPEC-025/027 A15）。

    无第二次 GUT/插桩：hits_lines 来自映射投影；执行证据 probes_fired 取自
    映射 arms_state（不得伪造）。写失败返回具名原因（调用方 fail-loud）。"""
    try:
        from .evidence import (build_bundle, evidence_dir_for, make_run_uuid,
                               write_bundle)
        d = evidence_dir_for(evidence_out)
        d.mkdir(parents=True, exist_ok=True)
        bundle = build_bundle(
            run_uuid=make_run_uuid(), file_path=file_path, gd_source=gd_source,
            tests_glob=tests_glob, hits_lines=sorted(covered),
            probes_fired=int(probes), gut_ok=True, run_error=False,
            timeout=False, zero_tests=False, vacuous=(total == 0))
        write_bundle(d, bundle)
        return None
    except Exception as e:  # noqa: BLE001 — 具名失败，不静默
        return f"evidence write failed: {type(e).__name__}: {e}"

def _cov_fail(path, src, tests_glob, evidence_out, msg,
              min_percent: float) -> dict:
    """统一失败出口：失败 bundle 落盘 + run_error 域 JSON。"""
    if evidence_out:
        _write_failure_evidence(evidence_out, str(path), src, tests_glob, msg)
    return _err_envelope(str(path), msg, min_percent=min_percent)

def _cov_precheck(file_path: str, project_root: str, tests_glob,
                  evidence_out, min_percent: float) -> tuple:
    """目标锚定 + 读源 + 已插桩残留守卫。返回 (path, src) 或
    (None, err_envelope)。"""
    from ..source_io import anchor_to_project_root, read_gd_source

    path = Path(anchor_to_project_root(file_path, project_root))
    if not path.is_file():
        raise FileNotFoundError(
            f"coverage target not found: {file_path} (anchored: {path})")
    src, read_error = read_gd_source(str(path))
    if read_error is not None:
        return None, _cov_fail(path, src, tests_glob, evidence_out,
                               read_error, min_percent)
    if _PROBE_PREFIX in src:
        msg = (f"target source appears already instrumented (contains "
               f"'{_PROBE_PREFIX}'); restore from version control first "
               f"(legacy direct residue; new path never writes the real file)")
        return None, _cov_fail(path, src, tests_glob, evidence_out, msg,
                               min_percent)
    return path, src

def _cov_vacuous(path, src, tests_glob, evidence_out, min_percent: float,
                 proj: dict) -> dict:
    """vacuous 出口：无可执行行 → 满分 vacuous（无行事实可言）。"""
    summary = {
        "file": str(path), "total_lines": 0, "covered_lines": 0,
        "coverage_percent": 100.0, "min_percent": min_percent,
        "probes_fired": 0, "probes_source": "none",
        "branch_total": 0, "branch_covered": 0,
        "branch_coverage_percent": 100.0, "hits_lines": [],
        "coverage_source": "line_mapping_projection",
        "attribution_map": proj.get("map_path"),
        "selection_scripts": 0, "vacuous": True,
        "note": "no executable lines",
    }
    summary.update(explicit_timing_meta())
    if evidence_out:
        why = _write_mapping_evidence(evidence_out, str(path), src,
                                      tests_glob, set(), 0, 0)
        if why is not None:
            return _cov_fail(path, src, tests_glob, evidence_out, why,
                             min_percent)
    return {"tool": "measure.coverage", "ok": True, "summary": summary,
            "failures": []}

def _cov_probes_evidence(path, src, tests_glob, evidence_out, min_percent,
                         proj: dict) -> tuple:
    """执行证据链（probes_fired 取自映射 arms_state，不伪造）：缺失/畸形/
    无效 → 具名 run_error。返回 ((arms_ok, probes), None) 或 (None, err)。"""
    from ..mutation import truth_select as _truth

    try:
        doc = _truth.load_map(proj["map_path"])
    except Exception as e:  # noqa: BLE001
        return None, _cov_fail(path, src, tests_glob, evidence_out,
                               f"attribution map unusable: {e}", min_percent)
    try:
        arms_state = doc.get("arms_state") or {}
        arms_ok = _truth.arms_valid(arms_state)
        probes_raw = (arms_state.get("gut_full") or {}).get("probes_fired")
    except (AttributeError, TypeError, ValueError) as e:
        msg = (f"attribution map execution evidence malformed "
               f"({type(e).__name__}: {e}); refusing to fabricate")
        return None, _cov_fail(path, src, tests_glob, evidence_out, msg,
                               min_percent)
    if not arms_ok:
        msg = ("attribution map execution evidence invalid (arms_state); "
               "line facts untrustworthy")
        return None, _cov_fail(path, src, tests_glob, evidence_out, msg,
                               min_percent)
    if probes_raw is None:
        msg = ("attribution map lacks execution evidence (probes_fired "
               "missing); refusing to fabricate a trusted score")
        return None, _cov_fail(path, src, tests_glob, evidence_out, msg,
                               min_percent)
    try:
        probes = int(probes_raw)
    except (TypeError, ValueError) as e:
        msg = (f"attribution map probes_fired malformed "
               f"({probes_raw!r}); refusing to fabricate")
        return None, _cov_fail(path, src, tests_glob, evidence_out, msg,
                               min_percent)
    return (arms_ok, probes), None

def _cov_report_build(path, src, tests_glob, evidence_out, min_percent,
                      proj: dict, probes: int, inst: set, covered: set,
                      total: int) -> dict:
    """分数/分支/汇总组装 + 映射投影 evidence 落盘 + 阈值门。"""
    # 执行证据哨兵：映射构建期 probes_fired==0 且存在有效行事实 → 行事实
    # 不可信，不得产出可信分数（fail-loud；非 vacuous 不豁免）。
    if probes == 0 and total > 0:
        return _cov_fail(path, src, tests_glob, evidence_out,
                         ("attribution map has probes_fired=0 with a "
                          "non-empty instrumentable surface — mapped line "
                          "facts are untrustworthy (map build timed out or "
                          "Godot never executed); refusing to produce a "
                          "trusted coverage score"), min_percent)
    cov = len(covered)
    pct = round(100.0 * cov / total, 2) if total else 100.0
    ok = pct >= min_percent
    failures = []
    if not ok:
        failures.append({
            "reason": f"coverage {pct}% below threshold {min_percent}%",
            "covered": cov, "total": total,
            "uncovered_lines": sorted(inst - covered)[:20]})
    branches = _derive_branches(src)
    b_total = branches.total
    b_cov = sum(1 for b in branches.items
                if b.line is not None and b.line in covered)
    b_pct = round(100.0 * b_cov / b_total, 2) if b_total else 100.0
    summary = {
        "file": str(path), "total_lines": total, "covered_lines": cov,
        "coverage_percent": pct, "min_percent": min_percent,
        "probes_fired": probes, "probes_source": "map_arms_state",
        "branch_total": b_total, "branch_covered": b_cov,
        "branch_coverage_percent": b_pct,
        "hits_lines": sorted(covered),
        "coverage_source": "line_mapping_projection",
        "attribution_map": proj["map_path"],
        "selection_scripts": len(proj.get("scripts") or []),
        "vacuous": total == 0,
    }
    if total == 0:
        summary["note"] = "no executable lines"
    summary.update(explicit_timing_meta())
    if evidence_out:
        why = _write_mapping_evidence(evidence_out, str(path), src, tests_glob,
                                      covered, total, probes)
        if why is not None:
            return _cov_fail(path, src, tests_glob, evidence_out, why,
                             min_percent)
    return {"tool": "measure.coverage", "ok": ok, "summary": summary,
            "failures": failures}

def _cov_project(path, project_root, tests_glob, timeout_s, src,
                 evidence_out, min_percent) -> tuple:
    """映射投影（CoverageMappingError/ValueError/OSError → 具名失败出口）。
    返回 (proj, None) 或 (None, err_envelope)。"""
    from .mapping import CoverageMappingError, project_file_coverage

    try:
        proj = project_file_coverage(project_root, str(path), tests_glob,
                                     timeout_s=timeout_s)
    except CoverageMappingError as e:
        return None, _cov_fail(path, src, tests_glob, evidence_out, str(e),
                               min_percent)
    except (ValueError, OSError) as e:
        return None, _cov_fail(path, src, tests_glob, evidence_out,
                               f"{type(e).__name__}: {e}", min_percent)
    return proj, None

def run_coverage(
    file_path: str,
    project_root: str,
    min_percent: float = 80.0,
    timeout_s: int | None = None,
    tests_glob: str | None = None,
    evidence_out: str | None = None,
) -> dict:
    """单文件行覆盖——映射投影（§SPEC-025 唯一真源），零 GUT。"""
    path, src = _cov_precheck(file_path, project_root, tests_glob,
                              evidence_out, min_percent)
    if path is None:
        return src
    proj, err = _cov_project(path, project_root, tests_glob, timeout_s, src,
                             evidence_out, min_percent)
    if proj is None:
        return err
    inst = set(proj["instrumentable_lines"])
    covered = (set(proj["covered_lines"]) & inst) if inst \
        else set(proj["covered_lines"])
    total = len(inst)
    # vacuous：无可执行行 → 无行事实可言，无需映射/无需执行即满分 vacuous。
    if total == 0:
        return _cov_vacuous(path, src, tests_glob, evidence_out, min_percent,
                            proj)
    checked, err = _cov_probes_evidence(path, src, tests_glob, evidence_out,
                                        min_percent, proj)
    if checked is None:
        return err
    _arms_ok, probes = checked
    try:
        inst = {int(l) for l in
                (proj["instrumentable_lines"] or [])}
        covered = {int(l) for l in
                   (proj["covered_lines"] or [])}
    except (TypeError, ValueError) as e:
        return _cov_fail(path, src, tests_glob, evidence_out,
                         f"projection covered/instrumentable lines "
                         f"malformed: {e}", min_percent)
    total = len(inst)
    return _cov_report_build(path, src, tests_glob, evidence_out,
                             min_percent, proj, probes, inst, covered, total)

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
        "tool": "measure.coverage",
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
