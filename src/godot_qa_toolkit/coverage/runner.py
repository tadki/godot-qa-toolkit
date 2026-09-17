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

# 与 mutation runner 同一套判定：godot 进程级失败（-s 脚本没加载起来）≠
# 测试结果。res:// 是唯一可靠的 -s 形式（Revy QA FAIL 实证）。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)


def _looks_like_godot_launch_failure(output: str) -> bool:
    low = output.lower()
    return any(marker in low for marker in _GODOT_LAUNCH_ERROR_MARKERS)

# user:// 优先：win64@WSL 下项目 res:// 走 UNC 映射（//wsl.localhost/...），
# FileAccess 对该路径的写入可见性/时序脆弱。user:// 是 Godot 管理的应用数据
# 目录，两侧 OS 均为本地可靠文件系统。runner 侧按 user:// 目录约定读取。
_SINK_DIR_MARKER = "user://"

_PROBE_PREFIX = "__qa_cov_probe_"

# 可执行语句的规则/树形节点名（插桩目标）——语句级，不深挖表达式。
_EXECUTABLE_NODE_NAMES = {
    "if_branch",
    "elif_branch",
    "else_branch",
    "for_stmt",
    "while_stmt",
    "return_stmt",
    "func_var_stmt",
    "func_var_inf",
    "expr_stmt",
    "match_stmt",
    "break_stmt",
    "continue_stmt",
    "pass_stmt",
}

# 分支覆盖 v1 推导用节点（if_stmt 含 elif/else 子分支；match_stmt 含各 case）。
_BRANCH_GROUP_NODES = {"if_stmt", "match_stmt"}


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


def _tree_depth_walk(node, callback, depth=0):
    callback(node, depth)
    for child in getattr(node, "children", []):
        _tree_depth_walk(child, callback, depth + 1)


def _collect_executable_lines(src: str) -> list[ExecutableLine]:
    """用 gdtoolkit lark（gather_metadata）找出所有可执行语句的【起始行】。

    语句跨多行时只取节点起始行——探针插在语句前，绝不落进表达式内部
    （SEE-1308 实测：探针插进跨行分组表达式内部 → Parse Error）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    lines: list[ExecutableLine] = []

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data in _EXECUTABLE_NODE_NAMES:
            meta = getattr(node, "meta", None)
            if meta is not None and not meta.empty and meta.line > 0:
                lines.append(ExecutableLine(line=meta.line, node=node.data))

    _tree_depth_walk(tree, visit)
    seen = set()
    unique = []
    for l in lines:
        if l.line not in seen:
            seen.add(l.line)
            unique.append(l)
    unique.sort(key=lambda x: x.line)
    return unique


def _derive_branches(src: str) -> BranchSet:
    """SPEC-007：分支覆盖 v1 纯推导——行探针命中 + AST 分支结构，零新探针。

    if_stmt 的每个子分支（if/elif/else）与 match_stmt 的每个 match_branch
    各计 1 分支；分支体首条语句行命中对应探针即视为分支覆盖。
    observational：不参与 ok 判定（探针只证明「执行过」，不证明「被断言守护」）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    items: list[BranchItem] = []

    def first_stmt_line(node) -> int | None:
        meta = getattr(node, "meta", None)
        if meta is not None and not meta.empty:
            return meta.line
        return None

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data == "if_stmt":
            for child in node.children:
                if hasattr(child, "data") and child.data in (
                    "if_branch", "elif_branch", "else_branch",
                ):
                    items.append(BranchItem(line=first_stmt_line(child)))
        elif node.data == "match_stmt":
            for child in node.children:
                if hasattr(child, "data") and child.data == "match_branch":
                    items.append(BranchItem(line=first_stmt_line(child)))
        # 子节点遍历由 _tree_depth_walk 统一处理——visit 内不再手动递归
        # （否则 if_stmt 会被双重遍历，分支重复计数）。

    _tree_depth_walk(tree, visit)
    return BranchSet(total=len(items), items=tuple(items))


def _build_manifest(executable_lines: list[ExecutableLine]) -> list[int]:
    """probe_id（列表下标）→ 源文件行号。ID-keyed 数据面的 runner 侧映射表。"""
    return [l.line for l in executable_lines]


def _instrument(src: str, sink_name: str) -> tuple[str, list[int]]:
    """AST 语句级插桩：每个可执行语句前插一行探针调用（整数 probe_id），
    文件尾追加探针函数。返回 (插桩源码, probe_id→行号 manifest)。

    探针函数用 user:// 唯一 sink 追加写（JSON 行：每次命中一行 probe_id）。
    GDScript 侧无环境读取——sink 名与 user:// 前缀在生成期以字面量嵌入。
    """
    executable_lines = _collect_executable_lines(src)
    manifest = _build_manifest(executable_lines)
    line_to_id = {line: i for i, line in enumerate(manifest)}

    out_lines = []
    for idx, line_text in enumerate(src.splitlines(keepends=True), start=1):
        if idx in line_to_id:
            indent = re.match(r"^(\s*)", line_text).group(1)
            out_lines.append(f"{indent}{_PROBE_PREFIX}{line_to_id[idx]}()\n")
        out_lines.append(line_text)

    # 追加写语义：READ_WRITE + seek_end——裸 WRITE 每次命中重开会截断此前
    # 全部命中（SEE-1312 代码走查实测缺陷）。
    probe_func = (
        f"\n\n# qa-toolkit coverage probe (auto-instrumented, never in production)\n"
        f"func {_PROBE_PREFIX}wrapper(probe_id):\n"
        f"\tvar f = FileAccess.open(\"{_SINK_DIR_MARKER}{sink_name}\", FileAccess.READ_WRITE)\n"
        f"\tif f == null:\n"
        f"\t\tf = FileAccess.open(\"{_SINK_DIR_MARKER}{sink_name}\", FileAccess.WRITE)\n"
        f"\telse:\n"
        f"\t\tf.seek_end()\n"
        f"\tif f:\n"
        f"\t\tf.store_line(str(probe_id))\n"
        f"\t\tf.close()\n\n"
    )
    for i in range(len(manifest)):
        probe_func += (
            f"func {_PROBE_PREFIX}{i}():\n"
            f"\t{_PROBE_PREFIX}wrapper({i})\n"
        )
    instrumented = "".join(out_lines) + probe_func
    return instrumented, manifest


def run_coverage(
    file_path: str,
    project_root: str,
    min_percent: float = 80.0,
    timeout_s: int = 120,
) -> dict:
    """对单个 .gd 文件算行覆盖（跑 GUT 后统计命中行）并出统一 JSON 契约。

    min_percent 阈值 gate（P0' 机器判定：覆盖率≥阈值=ok）。
    返回 {"tool":"coverage", "ok", "summary", "failures"}；
    ok=True 表示覆盖率达标。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"coverage target not found: {file_path}")

    original_src = path.read_text(encoding="utf-8")
    backup = original_src

    executable_lines = _collect_executable_lines(original_src)
    total_lines = len(executable_lines)

    if total_lines == 0:
        return {
            "tool": "coverage",
            "ok": True,
            "summary": {"file": str(path), "total_lines": 0, "covered_lines": 0,
                        "coverage_percent": 100.0, "min_percent": min_percent,
                        "vacuous": True,
                        "note": "no executable lines"},
            "failures": [],
        }

    # run 唯一 sink：跨 run 不残留、并发 run 不互踩（SEE-1152 并发覆写同款教训）。
    sink_name = f"qa-coverage-hits-{os.getpid()}.txt"
    instrumented, manifest = _instrument(original_src, sink_name)

    # 生成期 re-parse 预检：插桩产物不合法 = 插桩器 bug → 显式 run_error，
    # 拒绝落盘跑假数据（把 parse error 从静默假 0% 变成生成期失败）。
    try:
        gdtoolkit_parser.parse(instrumented)
    except Exception as e:
        return {
            "tool": "coverage",
            "ok": False,
            "summary": {"file": str(path), "total_lines": total_lines,
                        "covered_lines": 0, "coverage_percent": 0.0,
                        "min_percent": min_percent,
                        "run_error": True,
                        "error": f"instrumented source failed to parse: {e}"},
            "failures": [{"reason": (
                f"instrumentation produced unparseable source "
                f"(instrumenter bug, no GUT run attempted): {e}"
            )}],
        }

    hits: set[int] = set()
    probes_fired = 0

    with tempfile.TemporaryDirectory() as workdir:
        backup_path = os.path.join(workdir, "original.gd")
        shutil.copy2(str(path), backup_path)

        # user:// sink 读取路径：Linux/macOS 为 ~/.local/share/godot/app_userdata/
        # <project_name>/；win64 为 %APPDATA%\Godot\app_userdata\<project_name>/。
        # project_name 取自 project.godot（无则回退项目目录名）。
        user_sink = _resolve_user_sink(project_root, sink_name)

        path.write_text(instrumented, encoding="utf-8")
        gut_tail = ""
        try:
            gut_fs = os.path.join(project_root, "addons", "gut", "gut_cmdln.gd")
            if not os.path.isfile(gut_fs):
                raise FileNotFoundError(f"GUT runner not found: {gut_fs}")
            try:
                r = subprocess.run(
                    ["godot", "--headless", "--path", project_root,
                     "-s", GUT_SCRIPT_RES_PATH, "-gdir=res://tests/", "-gexit"],
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                gut_tail = (r.stdout + r.stderr)[-1500:]
            except subprocess.TimeoutExpired:
                return {
                    "tool": "coverage",
                    "ok": False,
                    "summary": {"file": str(path), "total_lines": total_lines,
                                "covered_lines": 0, "coverage_percent": 0.0,
                                "min_percent": min_percent, "error": "GUT timed out"},
                    "failures": [{"reason": f"GUT timed out after {timeout_s}s"}],
                }
            finally:
                path.write_text(backup, encoding="utf-8")
        except Exception:
            path.write_text(backup, encoding="utf-8")
            raise

        if r.returncode != 0 and _looks_like_godot_launch_failure(gut_tail):
            return {
                "tool": "coverage",
                "ok": False,
                "summary": {"file": str(path), "total_lines": total_lines,
                            "covered_lines": 0, "coverage_percent": 0.0,
                            "min_percent": min_percent,
                            "run_error": True,
                            "error": "godot launch failed — coverage data untrustworthy"},
                "failures": [{"reason": (
                    "godot launch failed (tests did not run): "
                    f"{gut_tail.strip()[:200]}"
                )}],
            }

        # 读取命中：优先项目根（兼容非 user:// 环境），其次 user:// sink。
        hits_raw: list[int] = []
        for sink in (os.path.join(project_root, sink_name), user_sink):
            if sink and os.path.isfile(sink):
                with open(sink, "r", encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln.isdigit():
                            hits_raw.append(int(ln))
                break

        probes_fired = len(hits_raw)
        # 哨兵：GUT 正常退出但探针从未点火 ≠ 真实 0% 覆盖——数据不可信。
        if probes_fired == 0:
            return {
                "tool": "coverage",
                "ok": False,
                "summary": {"file": str(path), "total_lines": total_lines,
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

        # ID-keyed 数据面：probe_id → manifest 行号；重复 id 幂等去重。
        valid_ids = {i for i in range(len(manifest))}
        for pid in hits_raw:
            if pid in valid_ids:
                hits.add(manifest[pid])

    path.write_text(backup, encoding="utf-8")

    covered = len(hits)
    pct = round(100.0 * covered / total_lines, 2) if total_lines else 100.0
    ok = pct >= min_percent

    uncovered_lines = sorted({l.line for l in executable_lines} - hits)

    failures = []
    if not ok:
        failures.append({
            "reason": f"coverage {pct}% below threshold {min_percent}%",
            "covered": covered,
            "total": total_lines,
            "uncovered_lines": uncovered_lines[:20],  # 截断展示
        })

    # 分支覆盖 v1 推导层（observational——不进 ok 判定）。
    branches = _derive_branches(original_src)
    branch_hit_lines = hits
    branches_covered = sum(
        1 for b in branches.items if b.line is not None and b.line in branch_hit_lines
    )
    branch_pct = (
        round(100.0 * branches_covered / branches.total, 2) if branches.total else 100.0
    )

    return {
        "tool": "coverage",
        "ok": ok,
        "summary": {
            "file": str(path),
            "total_lines": total_lines,
            "covered_lines": covered,
            "coverage_percent": pct,
            "min_percent": min_percent,
            "probes_fired": probes_fired,
            "branch_total": branches.total,
            "branch_covered": branches_covered,
            "branch_coverage_percent": branch_pct,
        },
        "failures": failures,
    }


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
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, sink_name)):
            return os.path.join(c, sink_name)
    return None
