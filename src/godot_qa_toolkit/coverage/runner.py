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


_CAUSAL_METHODS = ("call_deferred", "create_timer")


def _find_call_paren(text: str, method: str) -> tuple[int, int] | None:
    """定位最后一次 `<method>` 调用（允许方法名与左括号间有空白）。

    返回 (方法名起始下标, 左括号下标)；无调用返回 None。"""
    idx = -1
    pos = 0
    while True:
        found = text.find(method, pos)
        if found < 0:
            break
        after = found + len(method)
        j = after
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j < len(text) and text[j] == "(":
            idx = found
        pos = after
    if idx < 0:
        return None
    open_idx = idx + len(method)
    while open_idx < len(text) and text[open_idx] != "(":
        open_idx += 1
    return idx, open_idx


def _match_paren(text: str, open_idx: int) -> int | None:
    """从左括号起按括号深度与字符串状态找到配对的右括号下标；未闭合 None。"""
    depth = 0
    quote = ""
    i = open_idx
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return i if depth == 0 else None


def _call_receiver(text: str, idx: int) -> str | None:
    """调用点前缀 → 接收者文本：裸调用 = 隐式 self；点号前表达式取原文。"""
    head = text[:idx].rstrip()
    if head == "":
        return "self"
    if head.endswith("."):
        return head[:-1].strip() or None
    return None


def _call_arg_span(text: str, method: str) -> tuple[int, int, str] | None:
    """在调用点文本中定位 `<method>(<args>)`：返回 (开括号下标, 闭括号
    下标, 接收者文本)。接收者为 `self`（裸调用）或点号前的表达式文本；
    字符串/注释内的同名片段不参与匹配（本函数只处理 AST 已确认的调用点，
    文本扫描仅用于取实参原文，按括号深度与字符串状态推进）。"""
    found = _find_call_paren(text, method)
    if found is None:
        return None
    idx, open_idx = found
    close_idx = _match_paren(text, open_idx)
    if close_idx is None:
        return None
    recv = _call_receiver(text, idx)
    if recv is None:
        return None
    return open_idx, close_idx, recv


def _rewrite_call_sites(src: str, key_suffix: str) -> tuple[str, int]:
    """§SPEC-083 R2：把插桩文件内的 `call_deferred(...)` / `create_timer(...)`
    调用点改写为创建点包装（捕获当前上下文 tag，回调触发时按 tag 因果归属）。

    - `recv.call_deferred(a, b)` → `<ctx>.call_deferred_tagged(recv, [a, b])`
    - `recv.create_timer(a, b)`  → `<ctx>.create_timer_tagged(recv, a, b)`
    - 裸调用（隐式 self）同样处理。只改 AST 确认的调用点，字符串/注释内
      文本不受影响；替换不插入换行，行号与探针 manifest 保持对齐。
    返回 (改写后源码, 改写处数)。"""
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def _off(line: int, column: int) -> int:
        return starts[line - 1] + (column - 1)

    edits: list[tuple[int, int, str]] = []

    def _visit(node) -> None:
        data = getattr(node, "data", None)
        if data in ("standalone_call", "getattr_call"):
            meta = node.meta
            start = _off(meta.line, meta.column)
            end = _off(meta.end_line, meta.end_column)
            text = src[start:end]
            for method in _CAUSAL_METHODS:
                if method not in text:
                    continue
                span = _call_arg_span(text, method)
                if span is None:
                    continue
                open_idx, close_idx, recv = span
                args = text[open_idx + 1:close_idx]
                ctx = f"__qgta_{key_suffix}"
                if method == "call_deferred":
                    if not args.strip():
                        continue
                    repl = (f"{ctx}.call_deferred_tagged({recv}, "
                            f"[{args}])")
                elif args.strip():
                    repl = f"{ctx}.create_timer_tagged({recv}, {args})"
                else:
                    repl = f"{ctx}.create_timer_tagged({recv})"
                # 只替换「接收者起点 → 本次调用的右括号」，调用链后缀
                #（如 `.timeout.connect(...)`）原样保留；嵌套 getattr_call
                # 会重复命中同一段，按 (start, end) 去重。
                edits.append((start, start + close_idx + 1, repl))
                break
        for child in getattr(node, "children", []):
            _visit(child)

    _visit(tree)
    if not edits:
        return src, 0
    unique: dict[tuple[int, int], str] = {}
    for start, end, repl in edits:
        unique.setdefault((start, end), repl)
    out = src
    for (start, end), repl in sorted(unique.items(), key=lambda e: -e[0][0]):
        out = out[:start] + repl + out[end:]
    return out, len(unique)


def _attr_sink_name(sink_name: str) -> str:
    """命中 sink → 归属 sink 名（同 run_uuid/key8，前缀改 attr）。"""
    hits_prefix = "qa-coverage-hits-"
    if sink_name.startswith(hits_prefix):
        return "qa-coverage-attr-" + sink_name[len(hits_prefix):]
    return "qa-coverage-attr-" + sink_name


def _attr_var_name(sink_name: str) -> str:
    return f"{_PROBE_PREFIX}a_{_flag_var_suffix(sink_name)}"


def _probe_calls(src: str, line_to_id: dict[int, int],
                 flags: bool = False, sink_name: str = "",
                 attribution: bool = False,
                 attr_probe_ids: "frozenset[int] | None" = None) -> list[str]:
    """每个可执行语句前插一行探针调用（整数 probe_id）。

    flags 形态（§SPEC-046）：语句前只做内存 flag 标记（预置尺寸静态 Array
    的索引写）——零 I/O、零函数调用、零 append，写入（落盘）次数由统一
    收尾决定、不随事件数增长。direct 形态保持既有逐 id 函数形态，单文件/
    risk 路径字节不变。

    attribution=True（§SPEC-083 试验，默认关闭）：在 flag 标记之外追加
    「当前上下文身份」归属写——Engine meta `gqt_cov_slot`（钩子维护的
    上下文槽位，0 = 无主）。两种粒度：
    - attr_probe_ids=None：逐命中归属（hit 级，[seen_test, seen_script,
      unattr, counts6] 结构，ctx 正负编码测试/脚本车道）；
    - attr_probe_ids=函数入口 probe 集合（方向②）：只有这些位点带归属块
      （fn 级，[seen, cnt, fctx, unattr, phase4] 结构，槽位统一正数编码，
      fctx 记 (probe, slot)→1 的函数级归属），行覆盖 flag 照旧全量。"""
    out = []
    attr = _attr_var_name(sink_name)
    fn_mode = attr_probe_ids is not None
    for idx, line_text in enumerate(src.splitlines(keepends=True), start=1):
        if idx in line_to_id:
            indent = re.match(r"^(\s*)", line_text).group(1)
            pid = line_to_id[idx]
            if flags:
                out.append(
                    f"{indent}{_flag_var_name(sink_name)}"
                    f"[{pid}] = 1\n")
                if attribution and (fn_mode is False
                                    or pid in attr_probe_ids):
                    out.append(_attr_probe_block_text(
                        attr, pid, fn_mode, indent))
            else:
                out.append(f"{indent}{_PROBE_PREFIX}{pid}()\n")
        out.append(line_text)
    return out


def _attr_probe_block_text(attr: str, pid: int, fn_mode: bool,
                           indent: str) -> str:
    """归属探针块（方向②）：fn_mode=True 生成函数入口级块——Engine meta
    `gqt_cov_slot` 统一正数编码上下文槽位（0 = 无主），热路径只有一次
    meta 读 + 一次比较 + 两次数组写；fn_mode=False 保留 hit 级三车道块
    （上轮形态，供单测对照）。计数用显式赋值而非 `+=`：被测仓存在
    「断言源码无 +=」的源文本测试（KOL test_see1116）。"""
    t = f"{_PROBE_PREFIX}t{pid}"
    if not fn_mode:
        return (
            f"{indent}var {t} = Engine.get_meta(\"gqt_cov_ctx\", 0)\n"
            f"{indent}if {t} > 0:\n"
            f"{indent}\tvar {_PROBE_PREFIX}s{pid} = {attr}[0]\n"
            f"{indent}\tif {t} > {_PROBE_PREFIX}s{pid}.size():\n"
            f"{indent}\t\t{_PROBE_PREFIX}s{pid}.resize({t})\n"
            f"{indent}\t{_PROBE_PREFIX}s{pid}[{t} - 1] = 1\n"
            f"{indent}\t{attr}[3][0] = {attr}[3][0] + 1\n"
            f"{indent}elif {t} < 0:\n"
            f"{indent}\tvar {_PROBE_PREFIX}c{pid} = {attr}[1]\n"
            f"{indent}\tif -{t} > {_PROBE_PREFIX}c{pid}.size():\n"
            f"{indent}\t\t{_PROBE_PREFIX}c{pid}.resize(-{t})\n"
            f"{indent}\t{_PROBE_PREFIX}c{pid}[-{t} - 1] = 1\n"
            f"{indent}\t{attr}[3][1] = {attr}[3][1] + 1\n"
            f"{indent}else:\n"
            # 方向③：无主命中记 probe 热点与阶段桶
            # （0 bootstrap / 1 script_gap / 2 shutdown / 3 other）
            f"{indent}\t{attr}[2][{pid}] = {attr}[2].get({pid}, 0) + 1\n"
            f"{indent}\tvar {_PROBE_PREFIX}p{pid} = "
            f"Engine.get_meta(\"gqt_cov_phase\", 0)\n"
            f"{indent}\tif {_PROBE_PREFIX}p{pid} >= 0 "
            f"and {_PROBE_PREFIX}p{pid} <= 3:\n"
            f"{indent}\t\t{attr}[3][{_PROBE_PREFIX}p{pid} + 2]"
            f" = {attr}[3][{_PROBE_PREFIX}p{pid} + 2] + 1\n")
    p = f"{_PROBE_PREFIX}p{pid}"
    return (
        f"{indent}var {t} = Engine.get_meta(\"gqt_cov_slot\", 0)\n"
        f"{indent}if {t} > 0:\n"
        f"{indent}\t{attr}[0][{t} - 1] = 1\n"
        f"{indent}\t{attr}[1][{t} - 1] = {attr}[1][{t} - 1] + 1\n"
        f"{indent}\tvar {_PROBE_PREFIX}k{pid} = {attr}[2].get_or_add({pid}, {{}})\n"
        f"{indent}\t{_PROBE_PREFIX}k{pid}[{t} - 1] = 1\n"
        f"{indent}else:\n"
        # 无主分支（方向③口径不变）：probe 热点 + 阶段桶
        f"{indent}\t{attr}[3][{pid}] = {attr}[3].get({pid}, 0) + 1\n"
        f"{indent}\tvar {p} = Engine.get_meta(\"gqt_cov_phase\", 0)\n"
        f"{indent}\tif {p} >= 0 and {p} <= 3:\n"
        f"{indent}\t\t{attr}[4][{p}] = {attr}[4][{p}] + 1\n")


def _flag_var_suffix(sink_name: str) -> str:
    """从 sink 名提取 per-file key8（`...-<key8>.txt`），用于静态变量名
    唯一化——父/子类脚本同时插桩时同名 static var 触发 GDScript
    "member already exists in parent class" Parse Error，且子类探针经继承
    解析到父类数组造成索引越界（R5 真机实证缺陷）。"""
    m = re.search(r"-([0-9a-f]{8})\.txt$", sink_name)
    return m.group(1) if m else hashlib.sha1(sink_name.encode()).hexdigest()[:8]


def _flag_var_name(sink_name: str) -> str:
    return f"{_PROBE_PREFIX}m_{_flag_var_suffix(sink_name)}"


def _flag_probe_block(sink_name: str, count: int,
                      runtime_sink: bool = False,
                      attribution: bool = False,
                      causal: bool = False,
                      fn_mode: bool = False) -> str:
    """flag 标记注册块：per-file 预置尺寸静态 Array（变量名带文件 key8
    去继承冲突），经 Engine meta 注册（Godot 4.6 实证：脚本 reload 重跑
    不发生但 static Array 身份保留——跨 reload 合并由共享引用自然成立）。
    落盘由注入的 finalizer 在 suite 结束统一完成：写次数 = 文件数，与
    命中事件数无关。

    runtime_sink=True（measurement 并行分片臂）：sink 名拼接进程 PID——
    同一棵插桩树可被多个 GUT 进程并发执行而互不写串（每进程独立 sink，
    读取侧按 run_uuid+key8 模式合并）。

    attribution=True（§SPEC-083 试验，默认关闭）：追加归属注册块——
    hit 级（fn_mode=False）`[seen_test, seen_script, unattr_counts(字典),
    counts6]`；fn 级（fn_mode=True，方向②）`[seen, cnt, fctx, unattr_counts,
    phase4]`，seen/cnt 由注册函数按 `gqt_cov_slot_max` 预置尺寸（钩子在
    新槽位注册时同步扩容并零填充）。结构经 Engine meta `gqt_cov_attr`
    按 attr sink 名登记，finalizer 收尾时按 attr sink 落盘。默认路径不
    生成任何新代码。"""
    var = _flag_var_name(sink_name)
    key8 = _flag_var_suffix(sink_name)
    reg = f"{_PROBE_PREFIX}reg_{key8}"
    if runtime_sink:
        sink_var = f"{_PROBE_PREFIX}sink_{key8}"
        head = sink_name.rsplit("-", 1)
        sink_expr = (f"\"{head[0]}-\" + str(OS.get_process_id()) + "
                     f"\"-{key8}.txt\"")
        sink_decl = f"static var {sink_var}: String = {sink_expr}\n"
        sink_ref = sink_var
    else:
        sink_decl = ""
        sink_ref = f"\"{sink_name}\""
    attr_decl = ""
    if causal:
        # 创建点包装的上下文脚本（同目录注入，随备份/恢复清理）
        attr_decl += (f"const __qgta_{key8} = "
                      f"preload(\"res://addons/gqt_cov_ctx.gd\")\n")
    if attribution:
        attr = _attr_var_name(sink_name)
        areg = f"{_PROBE_PREFIX}areg_{key8}"
        attr_name = _attr_sink_name(sink_name)
        if runtime_sink:
            attr_head = attr_name.rsplit("-", 1)
            attr_ref = (f"\"{attr_head[0]}-\" + str(OS.get_process_id()) + "
                        f"\"-{key8}.txt\"")
        else:
            attr_ref = f"\"{attr_name}\""
        if fn_mode:
            holder = ("[[], [], {}, {}, [0, 0, 0, 0]]")
            sizing = (
                f"\tvar n: int = Engine.get_meta(\"gqt_cov_slot_max\", 0)\n"
                f"\th[0].resize(n)\n"
                f"\th[1].resize(n)\n"
                f"\tvar i: int = 0\n"
                f"\twhile i < n:\n"
                f"\t\th[0][i] = 0\n"
                f"\t\th[1][i] = 0\n"
                f"\t\ti += 1\n")
        else:
            holder = "[[], [], {}, [0, 0, 0, 0, 0, 0]]"
            sizing = ""
        attr_decl += (
            f"static var {attr}: Array = {areg}()\n"
            f"\n"
            f"static func {areg}() -> Array:\n"
            f"\tvar d: Dictionary = Engine.get_meta(\"gqt_cov_attr\")"
            f" if Engine.has_meta(\"gqt_cov_attr\") else {{}}\n"
            f"\tvar h: Array = d.get({attr_ref}, [])\n"
            f"\tif h.is_empty():\n"
            f"\t\th = {holder}\n"
            f"\t\td[{attr_ref}] = h\n"
            f"\t\tEngine.set_meta(\"gqt_cov_attr\", d)\n"
            f"{sizing}"
            f"\treturn h\n"
        )
    return (
        f"\n\n# qa-toolkit coverage probe (auto-instrumented, never in production)\n"
        f"{sink_decl}"
        f"static var {var}: Array = {reg}()\n"
        f"{attr_decl}"
        f"\n"
        f"static func {reg}() -> Array:\n"
        f"\tvar d: Dictionary = Engine.get_meta(\"gqt_cov_marks\")"
        f" if Engine.has_meta(\"gqt_cov_marks\") else {{}}\n"
        f"\tvar arr: Array = d.get({sink_ref}, [])\n"
        f"\tarr.resize({count})\n"
        f"\td[{sink_ref}] = arr\n"
        f"\tEngine.set_meta(\"gqt_cov_marks\", d)\n"
        f"\treturn arr\n"
    )


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


# §SPEC-055/070 FR-4：基本块压缩——控制转移/控制结构/缩进变化处切块。
# 块入口探针命中会展开为块内全部语句起始行计入覆盖——这是行覆盖**近似**
# 而非逐语句精确测量：运行期错误中止块内执行时，后继语句仍会被计为已
# 覆盖（measurement 报告 coverage_semantics 显式披露该近似，不冒充精确
# statement_start_line）。
_BLOCK_BREAKER_NODES = frozenset(
    {"return_stmt", "break_stmt", "continue_stmt"})
_BLOCK_COMPOUND_NODES = frozenset(
    {"if_branch", "elif_branch", "else_branch", "for_stmt", "while_stmt",
     "match_stmt", "match_branch"})


def _blocks_from_execs(execs, src: str) -> dict[int, list[int]]:
    lines = src.splitlines()
    blocks: dict[int, list[int]] = {}
    run: list[int] = []

    def _flush() -> None:
        nonlocal run
        if run:
            blocks[run[0]] = run
            run = []

    prev = None
    for e in sorted(execs, key=lambda x: x.line):
        if not (0 < e.line <= len(lines)):
            continue
        indent = len(re.match(r"^(\s*)", lines[e.line - 1]).group(1))
        start_new = prev is None
        if not start_new:
            p_line, p_node, p_indent = prev
            if e.line != p_line + 1:
                start_new = True
            elif e.node in _BLOCK_COMPOUND_NODES:
                start_new = True
            elif indent != p_indent:
                start_new = True
            elif p_node in _BLOCK_BREAKER_NODES:
                start_new = True
        if start_new:
            _flush()
        run.append(e.line)
        if e.node in _BLOCK_BREAKER_NODES:
            _flush()
        prev = (e.line, e.node, indent)
    _flush()
    return blocks


def _instrument_blocks(src: str, sink_name: str,
                       runtime_sink: bool = False,
                       attribution: bool = False,
                       causal: bool = False,
                       attr_probe_ids: "frozenset[int] | None" = None
                       ) -> tuple[str, list[int], dict[int, list[int]]]:
    """§SPEC-055 块压缩插桩：仅块入口行插 flag 探针；返回
    (插桩源码, 块入口行清单, 块入口→块内全部起始行映射)。

    attribution=True 时 attr_probe_ids 指定携带归属块的 probe 集合
    （方向②函数入口级）；None = 全部 probe 携带（hit 级）。"""
    execs = list(_collect_executable_lines(src))
    bmap = _blocks_from_execs(execs, src)
    starts = sorted(bmap)
    if not starts:
        return src, [], {}
    line_to_id = {ln: i for i, ln in enumerate(starts)}
    fb = "".join(_probe_calls(src, line_to_id, True, sink_name,
                              attribution, attr_probe_ids))
    return (fb + _flag_probe_block(sink_name, len(starts), runtime_sink,
                                   attribution, causal,
                                   fn_mode=attr_probe_ids is not None),
            starts, bmap)


def _instrument(src: str, sink_name: str, flags: bool = False,
                runtime_sink: bool = False,
                attribution: bool = False,
                causal: bool = False) -> tuple[str, list[int]]:
    """AST 语句级插桩：每个可执行语句前插一行探针调用（整数 probe_id），
    文件尾追加探针机制。返回 (插桩源码, probe_id→行号 manifest)。

    flags=True（§SPEC-046 measurement 批式臂）：命中点只做内存 flag 标记
    （预置尺寸静态 Array 索引写），落盘由编排器注入的 finalizer 在 suite
    结束统一完成；direct（缺省）保持既有逐命中追加写形态，单文件/risk
    路径行为与产物字节不变。"""
    manifest = _build_manifest(_collect_executable_lines(src))
    line_to_id = {line: i for i, line in enumerate(manifest)}
    if flags and not manifest:
        # 零可执行行：无探针面，插桩产物 = 原文（vacuous 车道在编排器落）
        return src, manifest
    body = "".join(_probe_calls(src, line_to_id, flags, sink_name,
                                attribution))
    if flags:
        return (body + _flag_probe_block(sink_name, len(manifest),
                                         runtime_sink, attribution, causal),
                manifest)
    return body + _probe_func(sink_name, len(manifest)), manifest


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


def _map_probes_fired(doc) -> "int | None":
    return ((doc.get("arms_state") or {}).get("gut_full") or {}).get(
        "probes_fired")


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


def run_coverage(
    file_path: str,
    project_root: str,
    min_percent: float = 80.0,
    timeout_s: int | None = None,
    tests_glob: str | None = None,
    evidence_out: str | None = None,
) -> dict:
    """单文件行覆盖——由行级归属映射投影（§SPEC-025 唯一真源），零 GUT。

    - 新鲜映射 → 亚秒投影；缺失/失效 → 自动构建或增量重建（`--timeout`
      绑定这些**真实执行阶段**；新鲜投影不启进程，无执行成本）。
    - 旧 direct 就地插桩 + GUT + sink 执行体已物理退役；`--recover` 不再
      作为覆盖来源（sink-recovery 已退役）。
    - `probes_fired` 取自映射 `arms_state`（不伪造）：缺失 → fail-loud；
      `probes_fired==0` 且存在有效行事实（非 vacuous）→ 不得产出可信分数，
      同样 fail-loud。
    - 任何失败路径（含映射构建超时/Godot 启动失败/投影异常）都产出
      `run_error` 域 JSON，并在 `--evidence-out` 下落**失败 bundle**，供
      `measure risk` 逐文件降级、不整批中止。
    输出契约保持 file/total_lines/covered_lines/coverage_percent/min_percent/
    branch_*/hits_lines。
    """
    from ..source_io import anchor_to_project_root, read_gd_source
    from ..mutation import truth_select as _truth
    from .mapping import CoverageMappingError, project_file_coverage

    path = Path(anchor_to_project_root(file_path, project_root))
    if not path.is_file():
        raise FileNotFoundError(
            f"coverage target not found: {file_path} (anchored: {path})")
    src, read_error = read_gd_source(str(path))
    if read_error is not None:
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    read_error)
        return _err_envelope(str(path), read_error, min_percent=min_percent)
    if _PROBE_PREFIX in src:
        msg = (f"target source appears already instrumented (contains "
               f"'{_PROBE_PREFIX}'); restore from version control first "
               f"(legacy direct residue; new path never writes the real file)")
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)

    try:
        proj = project_file_coverage(project_root, str(path), tests_glob,
                                     timeout_s=timeout_s)
    except CoverageMappingError as e:
        msg = str(e)
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)
    except (ValueError, OSError) as e:
        msg = f"{type(e).__name__}: {e}"
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)

    inst = set(proj["instrumentable_lines"])
    covered = (set(proj["covered_lines"]) & inst) if inst else set(proj["covered_lines"])
    total = len(inst)

    # vacuous：无可执行行 → 无行事实可言，无需映射/无需执行即满分 vacuous。
    if total == 0:
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
                return _err_envelope(str(path), why, min_percent=min_percent)
        return {"tool": "measure.coverage", "ok": True, "summary": summary,
                "failures": []}

    try:
        doc = _truth.load_map(proj["map_path"])
    except Exception as e:  # noqa: BLE001
        msg = f"attribution map unusable: {e}"
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)
    arms_state = doc.get("arms_state") or {}
    if not _truth.arms_valid(arms_state):
        msg = ("attribution map execution evidence invalid (arms_state); "
               "line facts untrustworthy")
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)
    probes = _map_probes_fired(doc)
    if probes is None:
        msg = ("attribution map lacks execution evidence (probes_fired "
               "missing); refusing to fabricate a trusted score")
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)
    probes = int(probes)

    inst = set(proj["instrumentable_lines"])
    covered = (set(proj["covered_lines"]) & inst) if inst else set(proj["covered_lines"])
    total = len(inst)

    # 执行证据哨兵：映射构建期 probes_fired==0 且存在有效行事实 → 行事实
    # 不可信，不得产出可信分数（fail-loud；非 vacuous 不豁免）。
    if probes == 0 and total > 0:
        msg = ("attribution map has probes_fired=0 with a non-empty "
               "instrumentable surface — mapped line facts are untrustworthy "
               "(map build timed out or Godot never executed); refusing to "
               "produce a trusted coverage score")
        if evidence_out:
            _write_failure_evidence(evidence_out, str(path), src, tests_glob,
                                    msg)
        return _err_envelope(str(path), msg, min_percent=min_percent)

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
            return _err_envelope(str(path), why, min_percent=min_percent)

    return {"tool": "measure.coverage", "ok": ok, "summary": summary,
            "failures": failures}



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
