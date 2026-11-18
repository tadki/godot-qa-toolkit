"""SEE-1594 BTIDY（零行为拆分）：coverage 行级插桩引擎——可执行行/分支
派生、调用点改写、flag/attr 探针块、块压缩插桩。自 coverage/runner.py
原行段物理迁移；runner 以导入绑定保持既有引用面（patch seam 不变）。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..gd_ast_utils import max_leaf_line


def _rt():
    """运行时经 runner 模块属性解析类型/常量（避免循环导入）。"""
    import godot_qa_toolkit.coverage.runner as _m

    return _m


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


def _dedupe_sorted(lines: list[_rt().ExecutableLine]) -> list[_rt().ExecutableLine]:
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


def _collect_executable_lines(src: str) -> list[_rt().ExecutableLine]:
    """用 gdtoolkit lark（gather_metadata）找出所有可执行语句的【起始行】。

    语句跨多行时只取节点起始行——探针插在语句前，绝不落进表达式内部
    （SEE-1308 实测：探针插进跨行分组表达式内部 → Parse Error）。
    """
    tree = _rt().gdtoolkit_parser.parse(src, gather_metadata=True)
    lines: list[_rt().ExecutableLine] = []
    depths = _paren_depths(src)

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data in _rt()._EXECUTABLE_NODE_NAMES:
            ln = _node_line(node)
            if ln and ln <= len(depths) and depths[ln - 1] == 0:
                lines.append(_rt().ExecutableLine(line=ln, node=node.data))
            # 括号内部行（depths>0）不是语句起始位置——即使 AST 报告该行为
            # 语句行（如跨行分组表达式 / lambda 实参行），也绝不插桩。

    # 内部类（class_def）整体不插桩（SEE-1367 §SPEC-017 硬化 反例实证）：
    # 探针函数追加在外层类作用域，内部类方法的 self 看不到它们——
    # Godot Parse Error "Function __qa_cov_probe_N() not found in base self"，
    # 目标文件整体拒载（GUT 侧表现为 "does not extend GutTest"），零探针假象。
    # 代价：内部类体（通常为测试桩）不计量——在套件自测量场景可接受。
    _tree_depth_walk(tree, visit, prune=lambda n: getattr(n, "data", None) == "class_def")
    return _dedupe_sorted(lines)


def _branch_body_nodes(branch_node):
    """分支体节点流（跳过头行污染层：if_branch/elif_branch 的 children[0]
    是条件表达式、match_branch 的 children[0] 是 pattern——meta.line 均等于
    分支头行；else_branch 无该层，全收）。"""
    children = [c for c in getattr(branch_node, "children", []) if hasattr(c, "data")]
    skip_first = branch_node.data in ("if_branch", "elif_branch", "match_branch")
    body = children[1:] if skip_first else children
    for c in body:
        yield c
        yield from _branch_body_nodes(c)


def _branch_body_line(branch_node) -> "int | None":
    """分支判定锚 = 分支体首条可执行语句行（硬化 反例实证：分支头行探针
    在条件求值时即点火，false 路径下 if 分支会被误判 covered）；体无语句
    （pass/空）回退分支头行。"""
    head = _node_line(branch_node)
    body_first = None
    for desc in _branch_body_nodes(branch_node):
        ln = _node_line(desc)
        if ln is not None and (body_first is None or ln < body_first):
            body_first = ln
    return body_first if body_first is not None else head


def _derive_branches(src: str) -> "_rt().BranchSet":
    """SPEC-007：分支覆盖 v1 纯推导——行探针命中 + AST 分支结构，零新探针。

    if_stmt 的每个子分支（if/elif/else）与 match_stmt 的每个 match_branch
    各计 1 分支。observational：不参与 ok 判定。class_def 剪枝：内部类
    分支体行不在探针面内（防结构性永假分支）。"""
    tree = _rt().gdtoolkit_parser.parse(src, gather_metadata=True)
    items: "list" = []

    def visit(node, depth=0):
        if not hasattr(node, "data"):
            return
        if node.data == "if_stmt":
            branch_nodes = (c for c in node.children
                            if hasattr(c, "data") and c.data in _rt()._IF_BRANCH_NODES)
        elif node.data == "match_stmt":
            branch_nodes = (c for c in node.children
                            if hasattr(c, "data") and c.data == "match_branch")
        else:
            branch_nodes = ()
        for child in branch_nodes:
            items.append(_rt().BranchItem(line=_branch_body_line(child)))

    _tree_depth_walk(tree, visit, prune=lambda n: getattr(n, "data", None) == "class_def")
    return _rt().BranchSet(total=len(items), items=tuple(items))

def _build_manifest(executable_lines: list[_rt().ExecutableLine]) -> list[int]:
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


def _causal_edits(src: str, key_suffix: str, starts: list[int]) -> "list":
    """AST 遍历收集改写点：(start, end, replacement) 列表（嵌套 getattr_call
    重复命中同段，由调用方按 (start, end) 去重）。"""
    def _off(line: int, column: int) -> int:
        return starts[line - 1] + (column - 1)

    edits: list = []

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
                #（如 `.timeout.connect(...)`）原样保留。
                edits.append((start, start + close_idx + 1, repl))
                break
        for child in getattr(node, "children", []):
            _visit(child)

    tree = _rt().gdtoolkit_parser.parse(src, gather_metadata=True)
    _visit(tree)
    return edits


def _rewrite_call_sites(src: str, key_suffix: str) -> tuple[str, int]:
    """§SPEC-083 R2：把插桩文件内的 `call_deferred(...)` / `create_timer(...)`
    调用点改写为创建点包装（捕获当前上下文 tag，回调触发时按 tag 因果归属）；
    裸调用同样处理。只改 AST 确认的调用点；不插入换行，行号与探针 manifest
    保持对齐。返回 (改写后源码, 改写处数)。"""
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    edits = _causal_edits(src, key_suffix, starts)
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
    return f"{_rt()._PROBE_PREFIX}a_{_flag_var_suffix(sink_name)}"


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
                out.append(f"{indent}{_rt()._PROBE_PREFIX}{pid}()\n")
        out.append(line_text)
    return out


def _attr_probe_block_text(attr: str, pid: int, fn_mode: bool,
                           indent: str) -> str:
    """归属探针块（方向②）：fn_mode=True 生成函数入口级块——Engine meta
    `gqt_cov_slot` 统一正数编码上下文槽位（0 = 无主），热路径只有一次
    meta 读 + 一次比较 + 两次数组写；fn_mode=False 保留 hit 级三车道块
    （上轮形态，供单测对照）。计数用显式赋值而非 `+=`：被测仓存在
    「断言源码无 +=」的源文本测试（KOL test_see1116）。"""
    t = f"{_rt()._PROBE_PREFIX}t{pid}"
    if not fn_mode:
        return (
            f"{indent}var {t} = Engine.get_meta(\"gqt_cov_ctx\", 0)\n"
            f"{indent}if {t} > 0:\n"
            f"{indent}\tvar {_rt()._PROBE_PREFIX}s{pid} = {attr}[0]\n"
            f"{indent}\tif {t} > {_rt()._PROBE_PREFIX}s{pid}.size():\n"
            f"{indent}\t\t{_rt()._PROBE_PREFIX}s{pid}.resize({t})\n"
            f"{indent}\t{_rt()._PROBE_PREFIX}s{pid}[{t} - 1] = 1\n"
            f"{indent}\t{attr}[3][0] = {attr}[3][0] + 1\n"
            f"{indent}elif {t} < 0:\n"
            f"{indent}\tvar {_rt()._PROBE_PREFIX}c{pid} = {attr}[1]\n"
            f"{indent}\tif -{t} > {_rt()._PROBE_PREFIX}c{pid}.size():\n"
            f"{indent}\t\t{_rt()._PROBE_PREFIX}c{pid}.resize(-{t})\n"
            f"{indent}\t{_rt()._PROBE_PREFIX}c{pid}[-{t} - 1] = 1\n"
            f"{indent}\t{attr}[3][1] = {attr}[3][1] + 1\n"
            f"{indent}else:\n"
            # 方向③：无主命中记 probe 热点与阶段桶
            # （0 bootstrap / 1 script_gap / 2 shutdown / 3 other）
            f"{indent}\t{attr}[2][{pid}] = {attr}[2].get({pid}, 0) + 1\n"
            f"{indent}\tvar {_rt()._PROBE_PREFIX}p{pid} = "
            f"Engine.get_meta(\"gqt_cov_phase\", 0)\n"
            f"{indent}\tif {_rt()._PROBE_PREFIX}p{pid} >= 0 "
            f"and {_rt()._PROBE_PREFIX}p{pid} <= 3:\n"
            f"{indent}\t\t{attr}[3][{_rt()._PROBE_PREFIX}p{pid} + 2]"
            f" = {attr}[3][{_rt()._PROBE_PREFIX}p{pid} + 2] + 1\n")
    p = f"{_rt()._PROBE_PREFIX}p{pid}"
    return (
        f"{indent}var {t} = Engine.get_meta(\"gqt_cov_slot\", 0)\n"
        f"{indent}if {t} > 0:\n"
        f"{indent}\t{attr}[0][{t} - 1] = 1\n"
        f"{indent}\t{attr}[1][{t} - 1] = {attr}[1][{t} - 1] + 1\n"
        f"{indent}\tvar {_rt()._PROBE_PREFIX}k{pid} = {attr}[2].get_or_add({pid}, {{}})\n"
        f"{indent}\t{_rt()._PROBE_PREFIX}k{pid}[{t} - 1] = 1\n"
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
    return f"{_rt()._PROBE_PREFIX}m_{_flag_var_suffix(sink_name)}"


def _flag_sink_parts(sink_name: str, key8: str,
                     runtime_sink: bool) -> "tuple[str, str]":
    """sink 声明与引用（runtime_sink=True：sink 名拼接进程 PID——同一棵
    插桩树可被多个 GUT 进程并发执行而互不写串）。"""
    if runtime_sink:
        sink_var = f"{_rt()._PROBE_PREFIX}sink_{key8}"
        head = sink_name.rsplit("-", 1)
        sink_expr = (f"\"{head[0]}-\" + str(OS.get_process_id()) + "
                     f"\"-{key8}.txt\"")
        return f"static var {sink_var}: String = {sink_expr}\n", sink_var
    return "", f"\"{sink_name}\""


def _flag_attr_decl(sink_name: str, key8: str, runtime_sink: bool,
                    attribution: bool, causal: bool, fn_mode: bool) -> str:
    """归属注册块（§SPEC-083 试验，默认关闭零生成）：hit 级/fn 级 holder
    与槽位扩容代码 + causal 创建点包装 preload。"""
    attr_decl = ""
    if causal:
        # 创建点包装的上下文脚本（同目录注入，随备份/恢复清理）
        attr_decl += (f"const __qgta_{key8} = "
                      f"preload(\"res://addons/gqt_cov_ctx.gd\")\n")
    if not attribution:
        return attr_decl
    attr = _attr_var_name(sink_name)
    areg = f"{_rt()._PROBE_PREFIX}areg_{key8}"
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
    return attr_decl


def _flag_probe_block(sink_name: str, count: int,
                      runtime_sink: bool = False,
                      attribution: bool = False,
                      causal: bool = False,
                      fn_mode: bool = False) -> str:
    """flag 标记注册块：per-file 预置尺寸静态 Array（变量名带文件 key8
    去继承冲突），经 Engine meta 注册；落盘由注入的 finalizer 在 suite
    结束统一完成（写次数 = 文件数，与命中事件数无关）。分解见
    _flag_sink_parts / _flag_attr_decl。"""
    var = _flag_var_name(sink_name)
    key8 = _flag_var_suffix(sink_name)
    reg = f"{_rt()._PROBE_PREFIX}reg_{key8}"
    sink_decl, sink_ref = _flag_sink_parts(sink_name, key8, runtime_sink)
    attr_decl = _flag_attr_decl(sink_name, key8, runtime_sink, attribution,
                                causal, fn_mode)
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
        f"static func {_rt()._PROBE_PREFIX}wrapper(probe_id):\n"
        f"\tvar f = FileAccess.open(\"{_rt()._SINK_DIR_MARKER}{sink_name}\", FileAccess.READ_WRITE)\n"
        f"\tif f == null:\n"
        f"\t\tf = FileAccess.open(\"{_rt()._SINK_DIR_MARKER}{sink_name}\", FileAccess.WRITE)\n"
        f"\telse:\n"
        f"\t\tf.seek_end()\n"
        f"\tif f:\n"
        f"\t\tf.store_line(str(probe_id))\n"
        f"\t\tf.close()\n\n"
    )
    for i in range(count):
        code += (
            f"static func {_rt()._PROBE_PREFIX}{i}():\n"
            f"\t{_rt()._PROBE_PREFIX}wrapper({i})\n"
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
