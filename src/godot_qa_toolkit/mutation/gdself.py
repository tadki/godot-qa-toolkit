"""SEE-1405 方案二 — GD 自描述正向索引（gdself）。

反向纯文本 grep 的假 0 命中（定案 69/158）源于间接消费不可见：工厂动态
load、autoload 单例、纯数据类转手、场景绑定。gdself 一次扫描全项目构建
``<文件 → 消费者>`` 正向索引，四信息源（定案 §方案二）：

- S1 被测 .gd 自身 ``class_name``（case-sensitive 标识符键）
- S2 ``project.godot`` ``[autoload]`` 段——autoload **node 名是消费者侧
  token 词表**（非目标侧身份键；KOL 23/23 node 名与 stem 全不同）
- S3 ``.tscn/.tres`` ``ext_resource`` script 绑定（资产节点入闭包图）
- S4 全项目 preload/load 字面量 + 标识符词匹配

身份键三类：res:// 路径（casefold）/ class_name（case-sensitive）/ stem
（casefold）；匹配前 strip comments/strings（autoload token 例外——
``get_node("EffectManager")`` 的消费形态在字符串里，按 raw 词匹配）。
间接消费闭包：深度 ≤2 可配 + hub 扇出截断（单节点消费者 > K 不再下钻，
终端 test 命中保留）+ 达 tests/ 即停（test 文件不扩展）；插值动态 load
识别前缀目录做保守兜底（多算只缩小子集不漏报）；uid:// 字面量 v1 不可
归因，登记 unresolved 分类。v1 零缓存：每次查询现场构建（判据：构建
成本 < 单轮启动税 5% 则 v2 永不必要）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from pathlib import Path

# 字面量捕获不锚右括号——插值形态 load("res://x/%s.gd" % id) 的引号后跟
# 的是格式表达式而非 `)`；非 load 调用里的同形串由 \b(?:preload|load)\s*\( 前缀排除。
_RES_LIT_RE = re.compile(r'\b(?:preload|load)\s*\(\s*"([^"]+)"')
_WORD_RE = re.compile(r"[A-Za-z_]\w*")
_CLASS_NAME_RE = re.compile(r"^\s*class_name\s+([A-Za-z_]\w*)", re.MULTILINE)
_EXT_PATH_RE = re.compile(r'\bpath="([^"]+)"')
_GD_SUFFIX = ".gd"
_ASSET_SUFFIXES = (".tscn", ".tres")
_DEFAULT_MAX_DEPTH = 2
_DEFAULT_HUB_FANOUT = 20


def strip_noise(text: str) -> str:
    """单遍扫描剥除注释与字符串字面量（替换为空格保位）。

    标识符匹配（class_name/stem 词）只认真实代码；res:// 字面量提取在
    本函数之前对 raw 做（字面量本体就在字符串里）。引号/转义/行注释
    交替态机处理，避免「先删串后删注释」的嵌套误判。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
        elif c in "\"'":
            triple = text[i:i + 3] == c * 3
            quote = c * 3 if triple else c
            i += len(quote)
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text.startswith(quote, i):
                    i += len(quote)
                    break
                i += 1
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _scan_gd(raw: str) -> dict:
    """单文件 .gd 信息面：lits/dyn_dirs/词表/class_name。"""
    lits: list[str] = []
    dyn_dirs: list[str] = []
    for lit in _RES_LIT_RE.findall(raw):
        folded = lit.strip().casefold()
        if "%" in folded:
            prefix = folded.split("%", 1)[0]
            dyn_dirs.append(prefix.rsplit("/", 1)[0] + "/"
                            if "/" in prefix else "res://")
        elif folded.startswith("res://"):
            lits.append(folded)
        elif folded.startswith("uid://"):
            continue  # uid 归 unresolved 分类（project 级计数），不入路径键
        else:
            lits.append(folded)
    stripped = strip_noise(raw)
    m = _CLASS_NAME_RE.search(stripped)
    words_stripped = set(_WORD_RE.findall(stripped))
    return {
        "lits": lits,
        "dyn_dirs": dyn_dirs,
        "words_raw": set(_WORD_RE.findall(raw)),
        "words_stripped": words_stripped,
        "words_fold": {w.casefold() for w in words_stripped},
        "class_name": m.group(1) if m else None,
    }


def _scan_asset(raw: str) -> dict:
    """.tscn/.tres 信息面：ext_resource 的 .gd 绑定路径（S3）。"""
    lits: list[str] = []
    for line in raw.splitlines():
        if "ext_resource" not in line:
            continue
        for path in _EXT_PATH_RE.findall(line):
            folded = path.strip().casefold()
            if folded.endswith(_GD_SUFFIX) and folded.startswith("res://"):
                lits.append(folded)
    return {"lits": lits, "dyn_dirs": [], "words_raw": set(),
            "words_stripped": set(), "words_fold": set(), "class_name": None}


_AUTOLOAD_ENTRY_RE = re.compile(r'^(\w+)="\*?res://(.+?)"\s*$', re.MULTILINE)


def _autoload_section(text: str) -> str:
    """截取 project.godot ``[autoload]`` 段（至下一个 section 头）。"""
    lines: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("["):
            in_section = line.strip() == "[autoload]"
            continue
        if in_section and "=" in line:
            lines.append(line)
    return "\n".join(lines)


def _parse_autoloads(pg_text: str) -> dict:
    """``[autoload]`` 段 → {node 名: res:// 路径(casefold)}。"""
    return {m.group(1): "res://" + m.group(2).casefold()
            for m in _AUTOLOAD_ENTRY_RE.finditer(_autoload_section(pg_text))}


def _iter_project_sources(root: Path) -> Iterator[tuple[Path, str]]:
    """产出 (path, rel_posix)：gd 与 .tscn/.tres 资产，跳过 .git/.qa-cache/.godot。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".qa-cache/") or rel.startswith(".godot/"):
            continue
        suffix = path.suffix.casefold()
        if suffix == _GD_SUFFIX or suffix in _ASSET_SUFFIXES:
            yield path, rel


def _uid_literal_count(raw: str) -> int:
    return sum(1 for lit in _RES_LIT_RE.findall(raw)
               if lit.strip().casefold().startswith("uid://"))


def build_index(project_root: str) -> dict:
    """全项目一次扫描 → 正向索引（纯内存，v1 零缓存）。"""
    t0 = time.perf_counter()
    root = Path(project_root).resolve()
    files: dict[str, dict] = {}
    pg = root / "project.godot"
    autoloads = _parse_autoloads(
        pg.read_text(encoding="utf-8")) if pg.is_file() else {}
    uid_literals = 0
    for path, rel in _iter_project_sources(root):
        raw = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.casefold() == _GD_SUFFIX:
            info = _scan_gd(raw)
            info["kind"] = "gd"
            uid_literals += _uid_literal_count(raw)
        else:
            info = _scan_asset(raw)
            info["kind"] = "asset"
        res_key = "res://" + rel.casefold()
        info["res"] = "res://" + rel
        info["stem"] = path.stem.casefold()
        info["tests"] = res_key.startswith("res://tests/")
        info["autoload"] = next(
            (name for name, ap in autoloads.items() if ap == res_key), None)
        files[res_key] = info
    return {"root": str(root), "files": files, "autoloads": autoloads,
            "uid_literals": uid_literals,
            "build_elapsed_s": time.perf_counter() - t0}


def _identities(index: dict, res_key: str) -> dict:
    """目标/闭包节点的身份键组（路径 casefold + stem casefold +
    class_name case-sensitive + autoload node 名 case-sensitive）。"""
    info = index["files"].get(res_key, {})
    return {
        "path": res_key,
        "stem": info.get("stem"),
        "class_name": info.get("class_name"),
        "autoload": info.get("autoload"),
    }


def _static_sources(finfo: dict, ident: dict) -> set[str]:
    """静态四源命中（path 字面量 / class_name / stem / autoload token）。"""
    sources: set[str] = set()
    if ident["path"] in finfo["lits"]:
        sources.add("path")
    cn = ident.get("class_name")
    if cn and cn in finfo["words_stripped"]:
        sources.add("class_name")
    stem = ident.get("stem")
    if stem and stem in finfo["words_fold"]:
        sources.add("stem")
    al = ident.get("autoload")
    if al and al in finfo["words_raw"]:
        sources.add("autoload")
    return sources


def _dynamic_hit(finfo: dict, ident: dict) -> bool:
    """插值目录兜底：目标 res 路径落在 finfo 的某动态前缀目录下。"""
    return any(ident["path"].startswith(d) for d in finfo["dyn_dirs"])


def _match_sources(finfo: dict, ident: dict) -> tuple[set, bool]:
    """文件 finfo 是否消费 ident 节点；返回 (命中源集合, 是否动态兜底)。

    动态目录兜底只在静态四源全 miss 时启用（兜底语义：静态不可解析才
    多算）；autoload token 按 raw 词匹配（消费形态含字符串字面量）。
    """
    sources = _static_sources(finfo, ident)
    if sources:
        return sources, False
    if _dynamic_hit(finfo, ident):
        return {"dynamic"}, True
    return sources, False


def _target_key_of(index: dict, target_res_path: str) -> str:
    """任意形态（fs 路径 / res://）→ 索引键（casefold res://）。"""
    if str(target_res_path).startswith("res://"):
        rel = target_res_path.removeprefix("res://")
    else:
        rel = str(Path(target_res_path).resolve()
                  .relative_to(Path(index["root"])))
    return ("res://" + rel).casefold()


def query_consumers(project_root: str, target_res_path: str,
                    tests_dir: Path,
                    max_depth: int = _DEFAULT_MAX_DEPTH,
                    hub_fanout: int = _DEFAULT_HUB_FANOUT,
                    index: dict | None = None) -> dict:
    """目标 → 消费者查询（闭包 ≤max_depth，hub 扇出截断，tests/ 即停）。

    返回 {target, consumers:[{res, kind, depth, sources}], unresolved:
    {reason, hub_truncated, dynamic_hits, uid_literals}}。reason ∈
    hub_truncated / dynamic_dir / uid_literal / no_tests（仅零消费者时）。
    """
    index = index if index is not None else build_index(project_root)
    target_key = _target_key_of(index, target_res_path)
    consumers, hub_truncated, dynamic_hits = _walk_closure(
        index, target_key, max_depth, hub_fanout)
    return {
        "target": index["files"].get(target_key, {}).get("res", target_key),
        "consumers": consumers,
        "unresolved": {
            "reason": _unresolved_reason(consumers, hub_truncated,
                                         dynamic_hits, index, target_key),
            "hub_truncated": hub_truncated,
            "dynamic_hits": dynamic_hits,
            "uid_literals": index["uid_literals"],
        },
    }


def _walk_closure(index: dict, target_key: str, max_depth: int,
                  hub_fanout: int) -> tuple[list, int, int]:
    """BFS 闭包：返回 (consumers, hub_truncated, dynamic_hits)。

    tests/ 文件是终端（不扩展）；单节点消费者 > hub_fanout 时披露截断、
    只下钻被禁止（该节点终端 test 命中保留，生产链不展开）。
    """
    consumers: list[dict] = []
    hub_truncated = 0
    dynamic_hits = 0
    if target_key not in index["files"]:
        return consumers, hub_truncated, dynamic_hits
    visited = {target_key}
    frontier = [(target_key, _identities(index, target_key))]
    for depth in range(1, max_depth + 1):
        nxt: list[tuple[str, dict]] = []
        for _, node_ident in frontier:
            batch = _consume_one_hop(index, node_ident, visited)
            if len(batch) > hub_fanout:
                hub_truncated += 1
            for key, finfo, sources, dyn in batch:
                dynamic_hits += 1 if dyn else 0
                consumers.append({"res": finfo["res"], "kind": finfo["kind"],
                                  "depth": depth, "sources": sorted(sources)})
                if not finfo["tests"] and len(batch) <= hub_fanout:
                    nxt.append((key, _identities(index, key)))
        if not nxt:
            break
        frontier = nxt
    return consumers, hub_truncated, dynamic_hits


def _consume_one_hop(index: dict, node_ident: dict,
                     visited: set) -> list[tuple]:
    """单跳扩展：node_ident 的全部消费者（跳过已访问）。"""
    batch: list[tuple] = []
    for key, finfo in index["files"].items():
        if key in visited:
            continue
        sources, dyn = _match_sources(finfo, node_ident)
        if sources:
            visited.add(key)
            batch.append((key, finfo, sources, dyn))
    return batch


def _unresolved_reason(consumers: list, hub_truncated: int, dynamic_hits: int,
                       index: dict, target_key: str) -> str | None:
    """零消费者时的诚实分类（披露优先级：hub > 动态 > uid > 真无测试）。"""
    if consumers:
        return None
    if hub_truncated:
        return "hub_truncated"
    if dynamic_hits:
        return "dynamic_dir"
    if index["uid_literals"] and target_key.endswith(_GD_SUFFIX):
        return "uid_literal"
    return "no_tests"
