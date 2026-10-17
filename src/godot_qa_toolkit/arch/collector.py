"""Pure Core 架构守卫 collector（SEE-1426 P1，§SPEC-1426-01/02）。

gdtoolkit AST 静态检查，沿 complexity collector/gate 模式：collector 只产出
结构化违规事实（file/line/rule/message），判定留给 gate。规则 v1：
- core/extends-whitelist：顶层 extends 只允许白名单基类（RefCounted/
  Resource 等纯数据/纯逻辑），继承 Node 系即违规（Pure Core 与场景树解耦）。
- no-engine-api：禁引用引擎单例/场景树入口——Node/SceneTree/Engine/Input/
  get_tree/get_node 等裸标识符或调用。
- no-bare-global-random：禁裸全局随机（randi/randf/randfn 及 range 版本、
  randomize）——Pure Core 必须显式注入 RandomNumberGenerator 或种子。

行号取自 lark Token（gdtoolkit 的 tree Meta 为 empty，token.line 是唯一
可靠行号源——本机实测）。规则 id 开放枚举（contract §7 加法演进）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gdtoolkit.parser import parser
from lark.exceptions import LarkError

RULE_EXTENDS = "core/extends-whitelist"
RULE_ENGINE = "no-engine-api"
RULE_RANDOM = "no-bare-global-random"

# core/ 下允许的 extends 白名单：RefCounted 与 Resource 系纯数据/纯逻辑基类。
# 名单开放枚举：后续按定案修订追加（contract §7 加法）。
CORE_EXTENDS_WHITELIST = frozenset({
    "RefCounted", "Resource", "Object",
    "JSON", "AStar2D", "AStar3D", "AStarGrid2D",
    "Curve", "Curve2D", "Curve3D", "Gradient",
})

# 禁引用的引擎全局/单例与场景树入口（裸标识符或调用 callee 均算）。
FORBIDDEN_ENGINE_IDENTIFIERS = frozenset({
    "Node", "Node2D", "Node3D", "SceneTree", "Engine", "Input",
    "Viewport", "CanvasItem", "get_tree", "get_node", "get_viewport",
    "add_child", "remove_child", "queue_free", "is_inside_tree",
})

# 裸全局随机函数：Pure Core 必须显式 RNG/Seed 注入，任何直接引用即违规。
# §SPEC-1426-26：seed() 全局种子函数同样收录（开放枚举加法演进）。
BARE_GLOBAL_RANDOM = frozenset({
    "randi", "randf", "randfn", "randi_range", "randf_range", "randomize",
    "seed",
})


@dataclass(frozen=True)
class ArchViolation:
    file: str
    line: int
    rule: str
    message: str


def collect_file(path: str | Path) -> tuple[list[ArchViolation], str | None]:
    """Collect one .gd file. Returns (violations, error) — error 非空表示
    无法解析（结构性失败，gate 计 failure，不静默跳过）。"""
    p = Path(path)
    try:
        src = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return [], f"unreadable: {e}"
    try:
        tree = parser.parse(src)
    except (LarkError, OSError) as e:
        return [], f"gd parse failed: {type(e).__name__}: {e}"

    violations: list[ArchViolation] = []
    violations.extend(_check_extends(tree, p))
    violations.extend(_check_tokens(tree, p))
    return violations, None


def _check_extends(tree, p: Path) -> list[ArchViolation]:  # noqa: ANN001 — lark Tree
    """extends 基类 = extends_stmt 子树里的第一个 NAME token（gdtoolkit 把
    基类名编码为 token，Meta 无行号——继承 token 自带 line）。"""
    out: list[ArchViolation] = []
    for node in tree.children:
        if not hasattr(node, "data") or node.data != "extends_stmt":
            continue
        for tok in node.scan_values(lambda v: hasattr(v, "line")):
            base = str(tok.value)
            if base not in CORE_EXTENDS_WHITELIST:
                out.append(ArchViolation(
                    file=str(p), line=int(tok.line), rule=RULE_EXTENDS,
                    message=f"extends {base} is not in the Pure Core whitelist",
                ))
            break
    return out


def _check_tokens(tree, p: Path) -> list[ArchViolation]:  # noqa: ANN001
    out: list[ArchViolation] = []
    tokens = _file_tokens(tree)
    # 显式 RandomNumberGenerator 在场 = 注入语义成立，成员形式的随机
    # 调用放行（裸全局形式仍拦截）。
    has_explicit_rng = any(str(t.value) == "RandomNumberGenerator" for t in tokens)
    for idx, tok in enumerate(tokens):
        name = str(tok.value)
        prev = str(tokens[idx - 1].value) if idx > 0 else ""
        member_call = prev == "."
        if name in BARE_GLOBAL_RANDOM:
            if has_explicit_rng and member_call:
                continue
            out.append(ArchViolation(
                file=str(p), line=int(tok.line), rule=RULE_RANDOM,
                message=f"bare global random {name} — inject an explicit RNG/seed",
            ))
        elif name in FORBIDDEN_ENGINE_IDENTIFIERS:
            out.append(ArchViolation(
                file=str(p), line=int(tok.line), rule=RULE_ENGINE,
                message=f"forbidden engine reference {name!r} in Pure Core",
            ))
    return out


def _file_tokens(tree) -> list:  # noqa: ANN001
    return [t for t in tree.scan_values(lambda v: hasattr(v, "line"))]


def collect_paths(paths: list[str | Path]) -> tuple[list[ArchViolation], list[dict]]:
    """收集目录/文件集。返回 (violations, unparseable)。"""
    violations: list[ArchViolation] = []
    unparseable: list[dict] = []
    for root in paths:
        p = Path(root)
        files = sorted(p.rglob("*.gd")) if p.is_dir() else [p]
        for f in files:
            v, error = collect_file(f)
            if error is not None:
                unparseable.append({"file": str(f), "reason": error})
            violations.extend(v)
    return violations, unparseable
