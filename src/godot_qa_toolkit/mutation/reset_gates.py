"""SEE-1550 §SPEC-020 第一批 ②——R-C 重置协议门套件（定案 ②硬序的门核心）。

本模块把 synthesis §3/§4 的门体系落为**引擎无关的纯模型**：worker 的脚本
缓存态/实例账本/连接指纹/user:// 树/init 计数以显式状态建模，门 = 状态上
的纯函数判定。J4 全部污染向量（含 V1 修复后的 skip_reload 负向控制）在本
测试面逐向量断言「门必须 fire」；真实 worker 集成（后续批）消费同一组门
函数——模型与引擎实现共享判定语义，避免"测试面测的是另一个世界"。

门清单（synthesis §3.3/§4）：
- 静态哈希门（强制级）：loaded-source 内容 == 影盘内容，逐闭包成员；
- 顺序门：闭包 reload rc 全收齐且全 0、顺序符合硬序，才允许进 Gut；
- 静态重放门：重置后 static 值 == **mutant** 冷启动值（scrub 到原码即 fire）；
- 连接指纹门：`(signal, target_class, method)` 元组哈希 == post-boot 基线；
- user:// 树门：腿后 user:// 树 diff == 空；
- 双 init 门：init 计数 == 1（先重置后探针顺序守卫）；
- 闭包新鲜门：闭包成员在盘上全部存在（折叠陈旧检测）。

术语（synthesis §3.10）：「原位刷码」「reload 集」。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .channel_router import compute_reload_closure


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


GATE_STATIC_HASH = "static_hash_gate"
GATE_RELOAD_ORDER = "reload_order_gate"
GATE_STATIC_REPLAY = "static_replay_gate"
GATE_CONNECTION_FINGERPRINT = "connection_fingerprint_gate"
GATE_USER_TREE = "user_tree_gate"
GATE_DOUBLE_INIT = "double_init_gate"
GATE_CLOSURE_FRESHNESS = "closure_freshness_gate"

# 硬序（synthesis §3.2）：闭包按「承载深度」排序 reload——被承载者（叶子）
# 先刷、承载者后刷；本模型以「引用者深度」的逆序 + 路径全序做确定性排序
# （同 Harti J3 的 stable-id 对应口径，顺序门只要求确定且全 rc=0）。
ORDER_OK = 0
ERR_PARSE = 43
ERR_ALREADY_IN_USE = 22


@dataclass
class ResetProtocolModel:
    """worker 单腿的脚本缓存/实例/副作用显式状态（引擎无关模型）。

    disk：影盘内容（mutant 或原码——写影盘时序与 M0 一致）；
    cache：引擎 loaded-source 内容（reload 后 = disk 对应项）；
    reload_ledger：逐成员 reload 记录（顺序 + rc）；
    static_values：静态初始化器当前值（重置后重放断言的输入）；
    instances：会话中实例化过的类账本；
    connections / user_files / init_count：副作用面。
    """

    closure: tuple[str, ...]
    disk: dict[str, bytes]
    cache: dict[str, bytes] = field(default_factory=dict)
    reload_ledger: list[tuple[str, int]] = field(default_factory=list)
    static_values: dict[str, object] = field(default_factory=dict)
    mutant_static_values: dict[str, object] = field(default_factory=dict)
    instances: set[str] = field(default_factory=set)
    connections: list[tuple] = field(default_factory=list)
    user_files: set[str] = field(default_factory=set)
    init_count: int = 0
    reload_hang: bool = False
    parse_failures: set[str] = field(default_factory=set)

    def instantiate(self, cls: str) -> None:
        """实例化记账（worker 会话中实例过的类——F4/静态判定输入）。"""
        self.instances.add(cls)

    def reload(self, res: str) -> int:
        """硬序 reload 原语：rc 收账；挂死模型化（V10）。"""
        if self.reload_hang:
            self.reload_ledger.append((res, -1))
            return -1
        if res not in self.disk:
            self.reload_ledger.append((res, 43))
            return 43
        if res in self.parse_failures:
            # 坏 parse（V3）：rc=43，缓存不更新——顺序门必须拦。
            self.reload_ledger.append((res, 43))
            return 43
        self.cache[res] = self.disk[res]
        self.reload_ledger.append((res, ORDER_OK))
        return ORDER_OK

    def skip_reload(self, res: str) -> None:
        """J4-V1 修复面：skip_reload 显式实现——跳过即缓存残留（负向
        控制的注入手段），静态哈希门必须 fire。"""

    def reset_state(self) -> None:
        """R-C reset 原语（J5 实证 2ms 档）：重建实例账本/副作用面；
        **不**触碰 cache——污染态响应不得依赖它（J3 [1]≠[6] 实证）。"""
        self.instances = set()
        self.init_count = 0


class GateViolation(Exception):
    """门 fire 的模型化载体（fields = 违约明细）。"""

    def __init__(self, gate: str, detail: dict):
        self.gate = gate
        self.detail = detail
        super().__init__(f"{gate} fired: {detail}")


def static_hash_gate(model: ResetProtocolModel) -> dict:
    """强制级：loaded-source == 影盘，逐闭包成员（V1/V5/V6 检测面）。"""
    bad = {}
    for res in model.closure:
        want = model.disk.get(res)
        got = model.cache.get(res)
        if want != got:
            bad[res] = {"disk_sha1": _sha1(want) if want is not None else None,
                        "cache_sha1": _sha1(got) if got is not None else None}
    return {GATE_STATIC_HASH: bad} if bad else {}


def reload_order_gate(model: ResetProtocolModel) -> dict:
    """顺序门：闭包全部 reload rc 收齐、全 0、无挂死，才允许进 Gut。"""
    ledger = dict(model.reload_ledger)
    missing = [r for r in model.closure if r not in ledger]
    failed = {r: rc for r, rc in ledger.items() if rc != ORDER_OK}
    detail = {}
    if missing:
        detail["missing"] = missing
    if failed:
        detail["failed"] = failed
    if model.reload_hang:
        detail["hang"] = True
    return {GATE_RELOAD_ORDER: detail} if detail else {}


def allows_gut(model: ResetProtocolModel) -> bool:
    """顺序门放言语义：收齐且全 0 才进 Gut（V3 判据原文）。"""
    return reload_order_gate(model) == {}


def static_replay_gate(model: ResetProtocolModel) -> dict:
    """静态重放门：重置后 static 值 == mutant 冷启动值（scrub 到原码即
    fire——V5 检测面）。"""
    bad = {}
    for res, mutant_value in model.mutant_static_values.items():
        if model.static_values.get(res) != mutant_value:
            bad[res] = {"replayed": model.static_values.get(res),
                        "expected_mutant": mutant_value}
    return {GATE_STATIC_REPLAY: bad} if bad else {}


def connection_fingerprint_gate(model: ResetProtocolModel,
                                baseline: tuple) -> dict:
    """连接指纹门：`(signal, target_class, method)` 元组哈希 == 基线。"""
    now = tuple(sorted(model.connections))
    base = tuple(sorted(baseline))
    if now == base:
        return {}
    leaked = [c for c in now if c not in base]
    lost = [c for c in base if c not in now]
    detail = {}
    if leaked:
        detail["leaked"] = leaked
    if lost:
        detail["lost"] = lost
    return {GATE_CONNECTION_FINGERPRINT: detail} if detail else {}


def user_tree_gate(model: ResetProtocolModel, baseline: frozenset) -> dict:
    """user:// 树门：腿后 user:// diff == 空。"""
    residue = set(model.user_files) - set(baseline)
    removed = set(baseline) - set(model.user_files)
    detail = {}
    if residue:
        detail["residue"] = sorted(residue)
    if removed:
        detail["removed"] = sorted(removed)
    return {GATE_USER_TREE: detail} if detail else {}


def double_init_gate(model: ResetProtocolModel) -> dict:
    """双 init 门：init 计数 == 1（先重置后探针顺序守卫）。"""
    if model.init_count == 1:
        return {}
    return {GATE_DOUBLE_INIT: {"init_count": model.init_count}}


def closure_freshness_gate(model: ResetProtocolModel,
                           project_disk_files: set[str]) -> dict:
    """闭包新鲜门：闭包成员必须仍在盘（折叠陈旧检测——图过期）。"""
    stale = [r for r in model.closure if r not in project_disk_files]
    return {GATE_CLOSURE_FRESHNESS: stale} if stale else {}


def all_gates(model: ResetProtocolModel, baseline_connections: tuple,
              baseline_user: frozenset,
              project_disk_files: set[str]) -> dict:
    """全门联跑：任一 fire 即细节字典非空（J4 判据面）。"""
    gates: dict = {}
    gates.update(static_hash_gate(model))
    gates.update(reload_order_gate(model))
    gates.update(static_replay_gate(model))
    gates.update(connection_fingerprint_gate(model, baseline_connections))
    gates.update(user_tree_gate(model, baseline_user))
    gates.update(double_init_gate(model))
    gates.update(closure_freshness_gate(model, project_disk_files))
    return gates


def canonical_reload_order(closure: tuple[str, ...],
                           graph: dict[str, set[str]]) -> tuple[str, ...]:
    """硬序的确定性排序（模型口径）：被引用最深者先刷（叶子先行），
    同深按路径全序——顺序门只要求「确定且收齐全 0」，具体序由实现钉死。"""
    depth: dict[str, int] = {res: 0 for res in closure}
    frontier = list(closure)
    while frontier:
        cur = frontier.pop()
        cur_depth = depth.get(cur, 0)
        for holder in graph.get(cur, ()):  # 引用者比被引用物深一层
            if holder in depth and depth[holder] < cur_depth + 1:
                depth[holder] = cur_depth + 1
                frontier.append(holder)
    return tuple(sorted(closure, key=lambda r: (depth.get(r, 0), r)))
