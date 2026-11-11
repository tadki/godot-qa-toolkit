"""SEE-1550 §SPEC-020 第一批 ①——执行前静态路由器（定案 A1–A9 排除语义）。

通道分配权威（规划期静态判定，先于任何引擎调用）：
- target ∈ project.godot `[autoload]` 表 → 冷通道；
- reload 闭包任一成员 ∈ `[autoload]` 表 → 冷通道（接线链面）；
- target 或 reload 闭包任一成员 ∈ GUT 域（`addons/gut/**`）→ 冷通道
  （synthesis §3.7 GUT 域排除）；
- 其余 → worker 通道占位（worker-reset/1 实施于后续批，本批仅判定）。

reload 闭包（synthesis §3.2 术语，源码静态可扫）：
- {目标} ∪ {静态承载脚本集} ∪ {preload/折叠形态持有者集}——
  实现 = 引用图上从目标的反向可达闭包（引用者链）∪ {目标}；
- 纯运行时 `load` 形态持有者豁免（synthesis 定案，Bachi R3：通道自愈）；
- autoload 裸名使用（compile-time 绑定语义，SEE-1550 PoC B2 实证）计为
  引用边——保守方向：多一条边只会多判冷（丢速度不丢正确性）。

reason 码随 RouteDecision 披露（stdout 只加不改口径：后续 worker/报告
集成只新增字段，不改既有键）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# preload("res://x") / load("res://x") 字面量（与 testscan 同源形态）
_RES_REF_PATTERN = re.compile(r'\b(?:preload|load)\s*\(\s*"([^"]+)"\s*\)')
# 顶层 class_name 声明（compile-time 全局类；内嵌 class X 不匹配）
_CLASS_NAME_DECL = re.compile(r"^class_name\s+([A-Za-z_]\w*)", re.MULTILINE)
# .tscn/.tres ext_resource 的 path 属性
_EXT_RESOURCE_PATH = re.compile(r'path\s*=\s*"(res://[^"]+)"')
# 场景缓存持有形态的文件后缀（R-DW4：闭包成员持有者 → 冷）
_SCENE_EXT = (".tscn", ".tres")
GUT_DOMAIN_PREFIX = "res://addons/gut/"
_EXCLUDED_DIRS = {".git", ".qa-cache", ".godot"}

REASON_TARGET_IN_AUTOLOAD = "target_in_autoload_table"
REASON_CLOSURE_MEMBER_IN_AUTOLOAD = "closure_member_in_autoload_table"
REASON_TARGET_IN_GUT_DOMAIN = "target_in_gut_domain"
REASON_CLOSURE_INTERSECTS_GUT_DOMAIN = "closure_intersects_gut_domain"
REASON_CLOSURE_SCENE_CACHED = "closure_scene_cached"
REASON_ELIGIBLE_WORKER = "eligible_worker"

CHANNEL_COLD = "cold"
CHANNEL_WORKER = "worker"


@dataclass(frozen=True)
class RouteDecision:
    """单目标路由判定。reasons 为机器可读码列表（含成员上下文）。"""

    target_res: str
    channel: str  # "cold" | "worker"
    reasons: tuple[str, ...] = field(default_factory=tuple)
    closure: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_cold(self) -> bool:
        return self.channel == CHANNEL_COLD


def _iter_project_sources(project_root: str) -> list[Path]:
    """枚举参与引用图的源文件（.gd + .tscn/.tres），排除非项目目录。"""
    root = Path(project_root)
    out: list[Path] = []
    for pattern in ("*.gd", "*.tscn", "*.tres"):
        for p in root.rglob(f"**/{pattern}"):
            if not (_EXCLUDED_DIRS & set(p.parts)):
                out.append(p)
    return sorted(set(out))


def _file_res_path(project_root: str, fs_path: Path) -> str:
    root = Path(project_root).resolve()
    rel = fs_path.resolve().relative_to(root).as_posix()
    return "res://" + rel


def parse_autoload_table(project_root: str) -> dict[str, str]:
    """{res:// 脚本路径 → autoload 单例名}（project.godot [autoload] 段）。"""
    from .beacon import autoload_map
    return autoload_map(project_root)


def build_reference_graph(
        project_root: str,
        files: list[Path] | None = None,
) -> dict[str, set[str]]:
    """构建引用图：{被引用 res 路径 → 引用者 res 路径集合}（反向边）。

    边来源：.gd 的 preload/load 字面量、.tscn/.tres 的 ext_resource path、
    autoload 裸名使用（compile-time 绑定语义——B2 探针实证不随 free/register
    更新，静态保守计入）、class_name 裸名使用（同 compile-time 绑定族——
    D-W2 根因修复：缺此边时消费者不进 reload 闭包，跨腿持有首腿编译绑定，
    mutant 对测试不可见 → 漏杀/过杀双向分歧）。方向：引用者 → 被引用物。
    """
    root = Path(project_root)
    if files is None:
        files = _iter_project_sources(project_root)
    autoload_table = parse_autoload_table(project_root)
    name_to_res = {v: k for k, v in autoload_table.items()}
    # pass 1：顶层 class_name 声明表（全局类名 → 声明脚本 res 路径）
    class_decls: dict[str, str] = {}
    texts: dict[Path, str] = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        texts[p] = text
        if p.suffix == ".gd":
            m = _CLASS_NAME_DECL.search(text)
            if m is not None:
                class_decls.setdefault(m.group(1),
                                       _file_res_path(project_root, p))
    graph: dict[str, set[str]] = {}
    for p, text in texts.items():
        holder_res = _file_res_path(project_root, p)
        refs: set[str] = set()
        if p.suffix == ".gd":
            refs.update(_RES_REF_PATTERN.findall(text))
            # autoload 裸名使用：compile-time 绑定（B2 探针实证不可重定向）
            for name, res in name_to_res.items():
                if re.search(r"\b" + re.escape(name) + r"\b", text):
                    refs.add(res)
            # class_name 裸名使用：compile-time 全局类绑定（同绑定语义族）
            for name, res in class_decls.items():
                if res != holder_res and re.search(r"\b" + re.escape(name)
                                                   + r"\b", text):
                    refs.add(res)
        else:
            refs.update(_EXT_RESOURCE_PATH.findall(text))
        for ref in refs:
            graph.setdefault(ref, set()).add(holder_res)
    return graph


def compute_reload_closure(graph: dict[str, set[str]],
                           target_res: str) -> tuple[str, ...]:
    """reload 闭包 = {目标} ∪ 反向可达（引用者链，含折叠持有者）。

    纯运行时 load 持有者豁免：本图只采静态可扫边（字面量/ext_resource/
    裸名）——运行时构造路径的 load 不构成边，天然豁免（synthesis 定案）。
    """
    closure = {target_res}
    frontier = [target_res]
    while frontier:
        cur = frontier.pop()
        for holder in graph.get(cur, ()):  # 引用者（反向边）
            if holder not in closure:
                closure.add(holder)
                frontier.append(holder)
    return tuple(sorted(closure))


def _in_gut_domain(res_path: str) -> bool:
    return res_path.startswith(GUT_DOMAIN_PREFIX)


@lru_cache(maxsize=8)
def _cached_graph(project_root: str) -> dict[str, set[str]]:
    """引用图进程级缓存：真实仓在 mutation 期间零写入（影盘写入在槽内），
    同进程对同一项目复用图——executor 多 worker 路径不重复扫树。"""
    return build_reference_graph(project_root)


@lru_cache(maxsize=8)
def _cached_autoload_table(project_root: str) -> dict[str, str]:
    return parse_autoload_table(project_root)


def _scene_cached_members(graph: dict[str, set[str]],
                          closure: tuple[str, ...]) -> list[str]:
    """被 .tscn/.tres ext_resource 持有的闭包成员（场景缓存持有形态）。

    PackedScene 在加载时即把成员脚本对象绑定进缓存，常驻 worker 的腿间
    重置对该缓存不可达（R-DW4 Owner 裁决：场景持有者全路由冷通道，指针
    旁路彻底退役）。保守方向：命中任一场景持有者即冷（丢速度不丢正确性）。"""
    return [m for m in closure
            if any(h.endswith(_SCENE_EXT) for h in graph.get(m, ()))]


def _collect_route_reasons(target_res, closure, graph, autoload_table):
    """排除语义 reason 码全量收集（autoload 表 / GUT 域 / 场景缓存持有）。"""
    reasons: list[str] = []
    if target_res in autoload_table:
        reasons.append(REASON_TARGET_IN_AUTOLOAD)
    if target_res and _in_gut_domain(target_res):
        reasons.append(REASON_TARGET_IN_GUT_DOMAIN)
    for member in closure:
        if member != target_res and member in autoload_table:
            reasons.append(f"{REASON_CLOSURE_MEMBER_IN_AUTOLOAD}:{member}")
        if member != target_res and _in_gut_domain(member):
            reasons.append(f"{REASON_CLOSURE_INTERSECTS_GUT_DOMAIN}:{member}")
    for member in _scene_cached_members(graph, closure):
        reasons.append(f"{REASON_CLOSURE_SCENE_CACHED}:{member}")
    return reasons


def route_target(project_root: str, target_res: str,
                 graph: dict[str, set[str]] | None = None,
                 autoload_table: dict[str, str] | None = None,
                 closure: tuple[str, ...] | None = None) -> RouteDecision:
    """单目标静态路由：排除语义命中任一即冷通道，reason 码全量披露。"""
    if autoload_table is None:
        autoload_table = _cached_autoload_table(project_root)
    if closure is None:
        if graph is None:
            graph = _cached_graph(project_root)
        closure = compute_reload_closure(graph, target_res)
    if graph is None:
        graph = _cached_graph(project_root)
    reasons = _collect_route_reasons(target_res, closure, graph,
                                     autoload_table)
    channel = CHANNEL_COLD if reasons else CHANNEL_WORKER
    if not reasons:
        reasons.append(REASON_ELIGIBLE_WORKER)
    return RouteDecision(target_res=target_res, channel=channel,
                         reasons=tuple(reasons), closure=closure)


def route_project_census(project_root: str) -> dict:
    """全量静态路由 census（纯源码读取，零引擎成本、零写入）。

    返回逐目标判定 + 份额聚合（Owner 要的实测倍率分解基础）。
    """
    files = _iter_project_sources(project_root)
    graph = build_reference_graph(project_root, files=files)
    autoload_table = parse_autoload_table(project_root)
    targets = [_file_res_path(project_root, p) for p in files
               if p.suffix == ".gd"]
    decisions = []
    for t in sorted(targets):
        d = route_target(project_root, t, graph=graph,
                         autoload_table=autoload_table)
        decisions.append({"target": d.target_res, "channel": d.channel,
                          "reasons": list(d.reasons)})
    cold = [d for d in decisions if d["channel"] == CHANNEL_COLD]
    worker = [d for d in decisions if d["channel"] == CHANNEL_WORKER]
    by_reason: dict[str, int] = {}
    for d in cold:
        for r in d["reasons"]:
            key = r.split(":")[0]
            by_reason[key] = by_reason.get(key, 0) + 1
    total = len(decisions)
    return {
        "project_root": str(Path(project_root).resolve()),
        "total_targets": total,
        "cold_targets": len(cold),
        "worker_eligible": len(worker),
        "worker_share": round(len(worker) / total, 4) if total else 0.0,
        "cold_by_reason": by_reason,
        "decisions": decisions,
    }
