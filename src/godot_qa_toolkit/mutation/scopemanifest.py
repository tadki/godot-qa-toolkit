"""SEE-1420 Phase 1 — scope manifest 提取（定案 D1/D2）+ 稳定 id（D3）
+ no-mutate 过滤（L6）。

scope 集（D1）：顶层 func + 内部 class 整体一个黑盒 + module（非 func 顶层
声明）；lambda 归外层 func（行域天然落在宿主 func span 内，零特判）。

header hash（D2）只收非 func/class 顶层声明；**全部 func 的 hash 绝不进
全量重跑触发成分**（否则差分在"改一个函数"场景自杀——H1 修补）。单向保守：
header 变更 → 本文件全量重跑；func 变更 → 只该 scope 重跑，绝不反向。

稳定 id（D3）= ``<file>#<scope_id>#<n>``：scope_id 为语义名（同名消歧
``foo#2`` 对标 clj defn/foo#2），n 为 scope 内静态扫描序（1-based）。id
仅是寻址提示——跨 run 正确性完全交给全签名身份校验（diffengine）。

class_def / func_def 自身 meta 的 end 行不可靠（gdtoolkit 实测 end 缺失或
越界）——span 终点一律取子树叶 token 最大行号。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from gdtoolkit.parser import parser as gdtoolkit_parser

# scope_id 同名消歧从第 2 个同名者开始（对标 clj defn/foo#2）。
_NO_MUTATE_MARKER = re.compile(r"#\s*gqt:\s*no-mutate\b")
_FUNC_DEF_NODES = ("func_def", "static_func_def")
_CLASS_DEF_NODES = ("class_def",)


@dataclass(frozen=True)
class ScopeSpan:
    scope_id: str
    kind: str  # "func" | "class"
    start_line: int
    end_line: int  # inclusive
    hash: str


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _lines_of(src: str) -> list[str]:
    return src.splitlines()


def _span_text(lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(lines[start_line - 1:end_line])


def _max_leaf_line(node) -> int:
    """子树叶 token 最大行号——span 终点唯一可信来源。

    gdtoolkit 容器节点（func_def/class_def/根）的 meta.end_line 会溢出到
    文件尾（实测：4 行 func 报 end_line=6）——容器的 end 绝不采信，只有
    叶 token 的行号与数据节点的起始行可用。
    """
    best = 0
    meta = getattr(node, "meta", None)
    if meta is not None and not meta.empty:
        best = max(best, getattr(meta, "line", 0) or 0)
    for child in getattr(node, "children", []):
        if hasattr(child, "data"):
            best = max(best, _max_leaf_line(child))
        else:
            best = max(best, getattr(child, "line", 0) or 0)
    return best


def _disambiguate(names_in_order: list[str]) -> dict[int, str]:
    """出现序 → scope_id：首个用本名，后续同名加 ``#k``（k 从 2 起）。"""
    seen: dict[str, int] = {}
    out: dict[int, str] = {}
    for idx, name in enumerate(names_in_order):
        n = seen.get(name, 0) + 1
        seen[name] = n
        out[idx] = name if n == 1 else f"{name}#{n}"
    return out


def extract_scopes(src: str) -> tuple[str, list[ScopeSpan]]:
    """返回 (header_hash, scopes)。scopes 按源码出现序排列。

    header hash：全部【非 func/class】顶层声明 span 文本拼接的 sha1（D2：
    只收非 func 顶层声明——注释/空行/纯排版不触发全量重跑，声明文本才触发）。
    func/class scope hash：各自 span 文本的 sha1（内容不变域 = id 稳定域）。
    """
    tree = gdtoolkit_parser.parse(src, gather_metadata=True)
    lines = _lines_of(src)
    func_nodes: list = []
    class_nodes: list = []
    header_spans: list[tuple[int, int]] = []
    for child in tree.children:
        data = getattr(child, "data", None)
        if data in _FUNC_DEF_NODES:
            func_nodes.append(child)
        elif data in _CLASS_DEF_NODES:
            class_nodes.append(child)
        elif data is not None:
            meta = getattr(child, "meta", None)
            if meta is not None and not meta.empty and (meta.line or 0) > 0:
                header_spans.append((meta.line, _max_leaf_line(child)))

    scopes: list[ScopeSpan] = []
    func_ids = _disambiguate(
        [_func_name(n) for n in func_nodes])
    for i, node in enumerate(func_nodes):
        start = getattr(node.meta, "line", 0)
        end = max(_max_leaf_line(node), start)
        scopes.append(ScopeSpan(func_ids[i], "func", start, end,
                                _sha1(_span_text(lines, start, end))))
    class_ids = _disambiguate([_class_name(n) for n in class_nodes])
    for i, node in enumerate(class_nodes):
        start = getattr(node.meta, "line", 0)
        end = max(_max_leaf_line(node), start)
        scopes.append(ScopeSpan(class_ids[i], "class", start, end,
                                _sha1(_span_text(lines, start, end))))
    scopes.sort(key=lambda s: s.start_line)

    header_hash = _sha1("\n".join(
        _span_text(lines, s, e) for s, e in header_spans))
    return header_hash, scopes


def _decl_name(node) -> str:
    for child in getattr(node, "children", []):
        if not hasattr(child, "data") and getattr(child, "line", 0) > 0:
            value = getattr(child, "value", "")
            if value and re.match(r"^[A-Za-z_]", value):
                return value
        if hasattr(child, "data"):
            found = _decl_name(child)
            if found:
                return found
    return "anonymous"


def _func_name(node) -> str:
    return _decl_name(node)


def _class_name(node) -> str:
    return _decl_name(node)


def scope_of_line(spans: list[ScopeSpan], line: int) -> str:
    """行 → 最内含 scope_id；不在任何 func/class span 内 → module scope。"""
    inner: ScopeSpan | None = None
    for s in spans:
        if s.start_line <= line <= s.end_line:
            if inner is None or (s.end_line - s.start_line) < \
                    (inner.end_line - inner.start_line):
                inner = s
    return inner.scope_id if inner is not None else "module"


def assign_scope_ids(spans: list, file_label: str, mutants: list) -> list:
    """为 mutant 标定 (scope_id, n) 并写 stable_id（返回新列表）。

    n = scope 内静态扫描序（1-based，scope 内去重）——id 稳定域 = scope
    内容不变域（D3）。本函数**不**按 hash 过滤位点：位点全量标定后由
    diffengine 决定执行面。
    """
    counter: dict[str, int] = {}
    out = []
    for m in mutants:
        scope_id = scope_of_line(spans, m.line)
        counter[scope_id] = counter.get(scope_id, 0) + 1
        n = counter[scope_id]
        sid = f"{file_label}#{scope_id}#{n}"
        out.append(m.with_scope(scope_id, n, sid))
    return out


def build_manifest(src: str, file_label: str, mutants: list) -> tuple[
        str, list, list, dict[str, str]]:
    """P1 manifest 编排入口：extract → no-mutate 过滤 → id 标定 → scope 表。

    返回 (header_hash, spans, mutants_with_ids, site_scopes)。过滤先行——
    **无位点 scope 不进 manifest**（D2）：site_scopes 只收含 ≥1 位点的
    scope；module 位点的 hash 锚定 header hash（D2 同域——顶层 var/const
    变更必然同触 header 重跑，module 差分不 self-invalidate）。
    """
    header_hash, spans = extract_scopes(src)
    filtered = filter_no_mutate(src, mutants, spans)
    tagged = assign_scope_ids(spans, file_label, filtered)
    site_scopes: dict[str, str] = {}
    for s in spans:
        if any(m.scope_id == s.scope_id for m in tagged):
            site_scopes[s.scope_id] = s.hash
    if any(m.scope_id == "module" for m in tagged):
        site_scopes["module"] = header_hash
    return header_hash, spans, tagged, site_scopes


def scopes_map(spans: list) -> dict[str, str]:
    """全部 scope 的 hash 表（含无位点 scope）——id 标定/诊断用；差分判定
    一律消费 build_manifest 产出的 site_scopes。"""
    return {s.scope_id: s.hash for s in spans}


def filter_no_mutate(src: str, mutants: list, spans: list[ScopeSpan]) -> list:
    """``# gqt: no-mutate`` 过滤（L6）：函数级（至下一顶层声明）+ 行级。

    函数级判定：marker 在顶层 func 定义行（含行尾注释）上，或其后的下一条
    非空行是顶层 func 定义行——抑制整个 func span；其余一律行级（只抑制
    marker 所在行）。内部类方法上的 marker 按 L6 黑盒粒度只有行级生效。
    """
    lines = _lines_of(src)
    func_starts = {s.start_line: s for s in spans if s.kind == "func"}
    suppressed_lines: set[int] = set()
    for i, text in enumerate(lines, start=1):
        if not _NO_MUTATE_MARKER.search(text):
            continue
        target = func_starts.get(i)
        if target is None:
            nxt = _next_code_line(lines, i)
            target = func_starts.get(nxt) if nxt else None
        if target is not None:
            suppressed_lines.update(range(target.start_line, target.end_line + 1))
        else:
            suppressed_lines.add(i)
    if not suppressed_lines:
        return mutants
    return [m for m in mutants if m.line not in suppressed_lines]


def _next_code_line(lines: list[str], after: int) -> int | None:
    for i in range(after + 1, len(lines) + 1):
        if lines[i - 1].strip():
            return i
    return None
