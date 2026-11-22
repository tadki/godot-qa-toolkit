"""SEE-1594 BTIDY（零行为拆分）：test-attribution/6 行级归属产物构建——
行事实/派生视图/臂级 payload/映射构建/产物与报告节。自 orchestrator.py
原行段物理迁移；`_derive_views_from_line_facts`、`_attribution_payload`、
`build_attribution_map`、`_attribution_section` 按阶段分解为 ≤50 行函数，
逻辑逐语句等价。"""

from __future__ import annotations

import json
from pathlib import Path

from .shadowio import _read_attr_merged, _read_registries

# SEE-1587：行级主真值 schema——行事实（per-test covered line sets）是唯一
# 主数据，file/function 视图退为派生投影；旧 /5 产物 fail-loud 拒绝（无
# 兼容别名，消费方 load 即报 schema 不符）。
ATTRIBUTION_SCHEMA = "test-attribution/6"
# 归属形态探针文本版本：插桩文本任何变化都要 bump，否则 prep 缓存会复用
# 旧模板（缓存键含 algo 但不含探针文本本身）。SEE-1587：归属粒度进 algo
# 键——全量与增量必须共用同一粒度键，fn 级旧条目（attr/6）与新 hit 级
# 条目（attr-line/1）绝不互相命中（SEE-1585 原型教训：同一键下两种形态
# 静默共存会让行集退化为函数入口行）。
ATTRIBUTION_ALGO_TAG = "attr-line/1"
# 归属身份的规范形态（映射键），产物与报告同源
_ATTRIBUTION_IDENTITY = "res://script::test_name"
# 报告 test_attribution 节从逐臂映射里抽出的汇总键（映射正文留在产物）
_ATTRIBUTION_SUMMARY_KEYS = (
    "tests", "scripts", "files", "pairs", "script_pairs", "function_pairs",
    "line_pairs", "script_line_pairs",
    "attributed_hit_events", "script_hit_events", "unattributed_hit_events",
    "lanes", "unattributed_phases", "unattributed_hotspots",
    "attributed_rate", "script_rate", "unattributed_rate",
    "files_with_unattributed", "unmapped_count", "unmapped_script_count")

_UNATTR_PHASES = ("bootstrap", "script_gap", "shutdown", "other")


def _hit_rate(value: int, total: int) -> float | None:
    """命中事件占比（total=0 时 None，不臆造 0）。"""
    return round(value / total, 6) if total else None


def _file_lane_slots(rel: str, per_pid: dict, test_tables: dict,
                     script_tables: dict) -> tuple:
    """单文件槽位归属：返回 (test 身份集, script 身份集, test 事件数,
    script 事件数, 未归属槽位披露)。test 车道优先（窗口内身份）。"""
    file_tests: set[str] = set()
    file_scripts: set[str] = set()
    test_events = 0
    script_events = 0
    unmapped: list[dict] = []
    for pid, rec in sorted(per_pid.items()):
        ttable = test_tables.get(pid, {})
        stable = script_tables.get(pid, {})
        for slot_idx in sorted(rec["seen"]):
            tident = ttable.get(slot_idx)
            sident = stable.get(slot_idx)
            if tident is not None:
                file_tests.add(tident)
                test_events += rec["cnt"].get(slot_idx, 0)
            elif sident is not None:
                file_scripts.add(sident)
                script_events += rec["cnt"].get(slot_idx, 0)
            else:
                # 组合注册表缺失该槽位——单条披露，不按车道猜测
                unmapped.append({"file": rel, "pid": pid, "slot": slot_idx})
    return file_tests, file_scripts, test_events, script_events, unmapped



def _fns_from_lines(by_rel: dict,
                    fid_by_line_by_rel: "dict[str, dict[int, str]] | None"
                    ) -> set[str]:
    """行集 → 函数集（经源码 registry 的 fid 行区间映射）。"""
    fids: set[str] = set()
    for rel, lines in by_rel.items():
        fmap = (fid_by_line_by_rel or {}).get(rel) or {}
        for ln in lines:
            fid = fmap.get(ln)
            if fid is not None:
                fids.add(fid)
    return fids


def _fns_view_from_lines(by_ident: dict,
                         fid_by_line_by_rel: "dict[str, dict[int, str]] | None"
                         ) -> dict:
    """身份→函数集视图（空函数集的身份不入视图）。"""
    out: dict = {}
    for ident, by_rel in by_ident.items():
        fids = _fns_from_lines(by_rel, fid_by_line_by_rel)
        if fids:
            out[ident] = sorted(fids)
    return out


def _invert_files_view(by_ident_files: dict) -> dict:
    """身份→文件集 → 文件→身份集反查。"""
    out: dict = {}
    for ident, rels in by_ident_files.items():
        for rel in rels:
            out.setdefault(rel, set()).add(ident)
    return out


def _derive_views_from_line_facts(
        test_to_lines: dict, script_to_lines: dict,
        fid_by_line_by_rel: "dict[str, dict[int, str]] | None") -> dict:
    """file/function 视图派生（SEE-1587 唯一口径）：行事实 + 源码 registry
    → 全部 file/function 边与 line→tests 反查。全量与增量共用（视图永不
    独立测量，只从行事实投影）。"""
    test_to_files = {i: sorted(d) for i, d in test_to_lines.items()}
    script_to_files = {i: sorted(d) for i, d in script_to_lines.items()}
    test_to_functions = _fns_view_from_lines(test_to_lines,
                                             fid_by_line_by_rel)
    script_to_functions = _fns_view_from_lines(script_to_lines,
                                               fid_by_line_by_rel)
    file_to_tests = _invert_files_view(test_to_files)
    file_to_scripts = _invert_files_view(script_to_files)
    line_to_tests: dict[str, dict[int, set[str]]] = {}
    for ident, by_rel in test_to_lines.items():
        for rel, lines in by_rel.items():
            slot = line_to_tests.setdefault(rel, {})
            for ln in lines:
                slot.setdefault(ln, set()).add(ident)
    return {
        "test_to_files": test_to_files,
        "script_to_files": script_to_files,
        "test_to_functions": test_to_functions,
        "script_to_functions": script_to_functions,
        "file_to_tests": file_to_tests,
        "file_to_scripts": file_to_scripts,
        "line_to_tests": line_to_tests,
    }


def _line_pair_counts(test_to_lines: dict,
                      script_to_lines: dict) -> "tuple[int, int]":
    """(line_pairs, script_line_pairs) 行事实计数——全量产物与增量合并
    共用口径（`_attribution_payload` / increment `_merge_arm` 单源）。"""
    return (sum(len(lines)
                for d in test_to_lines.values() for lines in d.values()),
            sum(len(lines)
                for d in script_to_lines.values() for lines in d.values()))


def _line_facts_views(test_to_lines: dict, script_to_lines: dict,
                      line_to_tests: dict) -> dict:
    """行事实产物的共用序列化：身份→{文件→sorted covered lines} 与
    行→测试反查——全量产物与增量合并单源（键序与排序口径一致）。"""
    return {
        "test_to_lines": {k: {r: sorted(ls) for r, ls in sorted(v.items())}
                          for k, v in sorted(test_to_lines.items())},
        "script_to_lines": {k: {r: sorted(ls) for r, ls in sorted(v.items())}
                            for k, v in sorted(script_to_lines.items())},
        "line_to_tests": {rel: {str(ln): sorted(ids)
                                for ln, ids in sorted(by_line.items())}
                          for rel, by_line in sorted(line_to_tests.items())},
    }


def _probe_surface_lines(probe_lines, block_map) -> list:
    """块入口 probe_lines 经 block_map 展开为可插桩行全集（sorted）——
    探针面计算的唯一展开实现。"""
    lines: set = set()
    for start in probe_lines:
        lines.update(block_map.get(start) or [start])
    return sorted(lines)


def _file_unattributed(per_pid: dict) -> tuple[list[int], dict[int, int]]:
    """单文件无主阶段计数与热点（probe→命中事件）。"""
    phases = [0, 0, 0, 0]
    counts: dict[int, int] = {}
    for rec in per_pid.values():
        for i, v in enumerate(rec["phases"]):
            phases[i] += v
        for probe_id, n in rec["unattr_counts"].items():
            counts[probe_id] = counts.get(probe_id, 0) + n
    return phases, counts


def _phase_lane_report(phases_total: list[int], lane_total: int) -> dict:
    """方向③：无主命中的四阶段桶（速率 + 占无主总量份额）。"""
    unattributed = sum(phases_total)
    out: dict[str, dict] = {}
    for i, name in enumerate(_UNATTR_PHASES):
        events = phases_total[i]
        out[name] = {
            "hit_events": events,
            "rate": _hit_rate(events, lane_total),
            "share_of_unattributed": (round(events / unattributed, 6)
                                      if unattributed else None),
        }
    return out


def _hotspot_report(hotspot_counts: dict, probe_lines_by_rel,
                    fid_by_line_by_rel, limit: int) -> list[dict]:
    """无主热点：probe→行→函数，按命中事件降序（同数按 file/probe 稳定序）。"""
    merged: list[tuple[int, str, dict]] = []
    for rel, slot in hotspot_counts.items():
        probe_lines = (probe_lines_by_rel or {}).get(rel) or []
        fid_by_line = (fid_by_line_by_rel or {}).get(rel) or {}
        for probe_id, n in slot.items():
            line = (probe_lines[probe_id]
                    if 0 <= probe_id < len(probe_lines) else None)
            fid = fid_by_line.get(line) if line is not None else None
            merged.append((n, rel, {
                "file": rel, "probe_id": probe_id, "line": line,
                "function_id": fid, "hit_events": n}))
    merged.sort(key=lambda e: (-e[0], e[1], e[2]["probe_id"]))
    return [entry for _n, _rel, entry in merged[:max(0, limit)]]

def _attribution_counts_head(arm: str, derived: dict,
                             test_to_lines: dict, script_to_lines: dict,
                             events: tuple) -> dict:
    """臂级 payload 头段：规模计数 + 三车道原始事件数（键序保持）。"""
    test_events, script_events, unattributed_events = events
    test_to_files = derived["test_to_files"]
    script_to_files = derived["script_to_files"]
    line_pairs, script_line_pairs = _line_pair_counts(test_to_lines,
                                                      script_to_lines)
    return {
        "arm": arm,
        "tests": len(test_to_files),
        "scripts": len(script_to_files),
        "files": len(derived["file_to_tests"]),
        "pairs": sum(len(v) for v in test_to_files.values()),
        "script_pairs": sum(len(v) for v in script_to_files.values()),
        "function_pairs": sum(len(v) for v in
                              derived["test_to_functions"].values()),
        "line_pairs": line_pairs,
        "script_line_pairs": script_line_pairs,
        # 兼容上轮口径：attributed = 测试级车道
        "attributed_hit_events": test_events,
        "script_hit_events": script_events,
        "unattributed_hit_events": unattributed_events,
    }


def _attribution_lane_block(events: tuple, lane_total: int,
                            phase_lane: dict, hotspots: list,
                            unmapped: list,
                            files_with_unattributed: int,
                            unattributed_files: "list[str] | None" = None
                            ) -> dict:
    """臂级 payload 中段：车道/相位/热点/速率/未归属披露（键序保持）。"""
    test_events, script_events, unattributed_events = events
    return {
        "lanes": {
            "test": {"hit_events": test_events,
                     "rate": _hit_rate(test_events, lane_total)},
            "script": {"hit_events": script_events,
                       "rate": _hit_rate(script_events, lane_total)},
            "unattributed": {
                "hit_events": unattributed_events,
                "rate": _hit_rate(unattributed_events, lane_total),
                "phases": phase_lane},
        },
        "unattributed_phases": phase_lane,
        "unattributed_hotspots": hotspots,
        "attributed_rate": _hit_rate(test_events, lane_total),
        "script_rate": _hit_rate(script_events, lane_total),
        "unattributed_rate": _hit_rate(unattributed_events, lane_total),
        "files_with_unattributed": files_with_unattributed,
        # SEE-1610 D1：有未归属命中（窗口归因有损）的文件名单——行级地图
        # 对这些文件欠近似，消费侧（mutation test 筛选闭包）据此整文件面。
        "unattributed_files": list(unattributed_files or []),
        "unmapped_test_indices": unmapped[:200],
        "unmapped_count": len(unmapped),
        # 组合注册表下单一天窗：未知槽位只在 unmapped 单列一次（不按车道猜）
        "unmapped_script_indices": [],
        "unmapped_script_count": 0,
    }


def _attribution_view_block(derived: dict) -> dict:
    """臂级 payload 尾段：派生 file/function 视图（键序保持）。"""
    return {
        "test_to_files": {k: sorted(v)
                          for k, v in sorted(derived["test_to_files"].items())},
        "script_to_files": {k: sorted(v)
                            for k, v in
                            sorted(derived["script_to_files"].items())},
        "test_to_functions": {k: sorted(v) for k, v in
                              sorted(derived["test_to_functions"].items())},
        "script_to_functions": {k: sorted(v) for k, v in
                                sorted(derived["script_to_functions"].items())},
        "file_to_tests": {k: sorted(v) for k, v in
                          sorted(derived["file_to_tests"].items())},
        "file_to_scripts": {k: sorted(v) for k, v in
                            sorted(derived["file_to_scripts"].items())},
    }


def _attribution_payload(arm: str, test_to_lines: dict, script_to_lines: dict,
                         derived: dict, unmapped: list, events: tuple,
                         lane_total: int, phase_lane: dict, hotspots: list,
                         files_with_unattributed: int,
                         unattributed_files: "list[str] | None" = None
                         ) -> dict:
    """臂级映射产物（test-attribution/6 的 arms 条目）。

    行事实为主真值：`test_to_lines` / `script_to_lines` 保存 per 身份
    per 文件的 covered line sets；`line_to_tests` 为行→测试反查；
    file/function 视图由 `_derive_views_from_line_facts` 派生。"""
    return {
        **_attribution_counts_head(arm, derived, test_to_lines,
                                   script_to_lines, events),
        **_attribution_lane_block(events, lane_total, phase_lane, hotspots,
                                  unmapped, files_with_unattributed,
                                  unattributed_files),
        # 行级主真值（line-primary）：身份 → {文件 → sorted covered lines}
        # 与行→测试反查（消费面：变异位点所在行的测试集合）
        **_attribution_view_block(derived),
        **_line_facts_views(test_to_lines, script_to_lines,
                            derived["line_to_tests"]),
    }


def _fn_probe_line_facts(per_pid: dict, rel: str, probe_lines: list,
                         block_map: dict, test_tables: dict,
                         script_tables: dict, test_to_lines: dict,
                         script_to_lines: dict) -> None:
    """单文件 `fn <probe> <slot>` 对 → 行事实（hit 级插桩下覆盖整个探针
    面）：probe → probe_lines → block_map 展开为 covered line set，slot
    按注册表解析身份与车道（test 车道优先）。原地写入两个行事实 dict。"""
    for pid, rec in sorted(per_pid.items()):
        ttable = test_tables.get(pid, {})
        stable = script_tables.get(pid, {})
        for probe_id, slots in sorted(rec["fn"].items()):
            if not 0 <= probe_id < len(probe_lines):
                continue     # 探针 manifest 不认识的 probe：不猜，跳过
            start = probe_lines[probe_id]
            lines = block_map.get(start) or [start]
            for slot_idx in sorted(slots):
                tident = ttable.get(slot_idx)
                if tident is not None:
                    test_to_lines.setdefault(tident, {}).setdefault(
                        rel, set()).update(lines)
                    continue
                sident = stable.get(slot_idx)
                if sident is not None:
                    script_to_lines.setdefault(sident, {}).setdefault(
                        rel, set()).update(lines)


def build_attribution_map(arm: str, attr_by_rel: dict[str, dict[int, dict]],
                          registries: tuple[dict, dict],
                          probe_lines_by_rel: "dict[str, list[int]] | None" = None,
                          block_map_by_rel: "dict[str, dict] | None" = None,
                          fid_by_line_by_rel: "dict[str, dict[int, str]] | None" = None,
                          hotspot_limit: int = 20) -> dict:
    """SEE-1587（line-primary）：per-file 归属证据 → 行事实 + 三条车道
    （车道事件统计取自 per-slot cnt，量级口径披露见 notes）。"""
    test_tables, script_tables = registries
    test_to_lines: dict[str, dict[str, set[int]]] = {}
    script_to_lines: dict[str, dict[str, set[int]]] = {}
    unmapped: list[dict] = []
    test_events = 0
    script_events = 0
    phases_total = [0, 0, 0, 0]
    files_with_unattributed = 0
    unattributed_files: list[str] = []
    hotspot_counts: dict[str, dict[int, int]] = {}
    for rel, per_pid in sorted(attr_by_rel.items()):
        _ft, _fs, te, se, um = _file_lane_slots(
            rel, per_pid, test_tables, script_tables)
        test_events += te
        script_events += se
        unmapped.extend(um)
        probe_lines = (probe_lines_by_rel or {}).get(rel) or []
        block_map = (block_map_by_rel or {}).get(rel) or {}
        _fn_probe_line_facts(per_pid, rel, probe_lines, block_map,
                             test_tables, script_tables,
                             test_to_lines, script_to_lines)
        phases, counts = _file_unattributed(per_pid)
        for i, v in enumerate(phases):
            phases_total[i] += v
        if sum(phases):
            files_with_unattributed += 1
            unattributed_files.append(rel)
        slot = hotspot_counts.setdefault(rel, {})
        for probe_id, n in counts.items():
            slot[probe_id] = slot.get(probe_id, 0) + n

    unattributed_events = sum(phases_total)
    lane_total = test_events + script_events + unattributed_events
    phase_lane = _phase_lane_report(phases_total, lane_total)
    hotspots = _hotspot_report(hotspot_counts, probe_lines_by_rel,
                               fid_by_line_by_rel, hotspot_limit)
    derived = _derive_views_from_line_facts(test_to_lines, script_to_lines,
                                            fid_by_line_by_rel)
    return _attribution_payload(
        arm, test_to_lines, script_to_lines, derived, unmapped,
        (test_events, script_events, unattributed_events), lane_total,
        phase_lane, hotspots, files_with_unattributed,
        sorted(unattributed_files))


def _fid_by_line_from_registry(registry: dict) -> dict[int, str]:
    """函数行区间 → fid（首个声明占据该行的函数胜出，与热点口径一致）。"""
    fid_by_line: dict[int, str] = {}
    for entry in (registry or {}).get("entries") or []:
        start = entry.get("start_line")
        end = entry.get("end_line")
        if isinstance(start, int) and isinstance(end, int):
            for line in range(start, end + 1):
                fid_by_line.setdefault(line, entry["fid"])
    return fid_by_line


def _collect_attribution(enabled: bool, root: Path, run_uuid: str, arm: str,
                         sink_by_rel, xdg_homes: list[str],
                         prepared: "dict[str, dict] | None" = None) -> dict | None:
    """§SPEC-083：xdg 清理前读回归属证据与注册表，聚合成臂级映射；
    开关关闭时不读任何 sink、返回 None。prepared 提供探针 manifest、
    func-registry 与块映射（行事实展开与派生视图的输入）。"""
    if not enabled:
        return None
    attr_by_rel = {
        rel: _read_attr_merged(root, run_uuid, rel, xdg_homes)
        for rel in sink_by_rel}
    registries = _read_registries(root, run_uuid, xdg_homes)
    probe_lines_by_rel: dict[str, list[int]] = {}
    block_map_by_rel: dict[str, dict] = {}
    fid_by_line_by_rel: dict[str, dict[int, str]] = {}
    for rel, item in (prepared or {}).items():
        probe_lines_by_rel[rel] = list(item.get("probe_lines") or [])
        block_map_by_rel[rel] = dict(item.get("block_map") or {})
        fid_by_line_by_rel[rel] = _fid_by_line_from_registry(
            item.get("registry") or {})
    return build_attribution_map(arm, attr_by_rel, registries,
                                 probe_lines_by_rel=probe_lines_by_rel,
                                 block_map_by_rel=block_map_by_rel,
                                 fid_by_line_by_rel=fid_by_line_by_rel)


def _tests_face_map(root: Path) -> dict[str, str]:
    """tests/ 树全部 .gd 的 rel→sha256（映射的测试面内容绑定）。

    A11（SEE-1594 S2）单一实现：本体在 coverlink.current_tests_face，
    此处只委派（消费侧 truth_select 同口径）。"""
    from ..mutation.coverlink import current_tests_face
    return current_tests_face(str(root))


def _arms_state(arms: tuple[dict, ...]) -> dict:
    """臂有效性（失效臂的空映射不得被消费方当真值，fail-loud 于消费侧）。

    SEE-1587 D3（QA 回归）：除 state/bundles/pairs 外落盘**执行完整性
    证据**——`probes_fired`（本臂全部 bundle 的探针点火总数）与 `vacuous`
    （全体 bundle 零可执行行）。消费侧据此区分：
    - 零测试/探针未点火且非 vacuous → 不是有效测量（fail-loud）；
    - 合法空面（确有测试执行/探针点火，但无归属边）→ 仍可用。
    缺省字段（旧产物）= 无法证明测过 → 消费侧 fail-loud（不假新鲜）。"""
    def _sent(bundle: dict) -> dict:
        # coverage-evidence/2 的哨兵嵌在 sentinels 下；顶层键形态留兜底
        # （直接调用方/测试可能给扁平 bundle）。
        return (bundle or {}).get("sentinels") or bundle or {}

    out: dict[str, dict] = {}
    for arm in arms:
        attribution = arm.get("attribution") or {}
        bundles_data = arm.get("bundles_data") or {}
        sentinels = [_sent(b) for b in bundles_data.values()]
        probes_fired = sum(int((s or {}).get("probes_fired") or 0)
                           for s in sentinels)
        vacuous = bool(sentinels) and all(
            bool((s or {}).get("vacuous")) for s in sentinels)
        out[arm["arm"]] = {
            "state": arm.get("state"),
            "invalid_reason": arm.get("invalid_reason"),
            "invalid_evidence": arm.get("invalid_evidence"),
            "bundles": int(arm.get("bundles") or 0),
            "pairs": sum(len(v) for v in
                         (attribution.get("test_to_files") or {}).values()),
            "probes_fired": probes_fired,
            "vacuous": vacuous,
        }
    return out


def _instrumentable_lines_from_prepared(prepared: dict) -> dict[str, list[int]]:
    """每目标文件的探针面（可插桩行全集）：块入口 probe_lines 经 block_map
    展开为全部块内语句起始行。Coverage Store 投影的合法跳过锚来源。"""
    out: dict[str, list[int]] = {}
    for rel, item in (prepared or {}).items():
        out[rel] = _probe_surface_lines(item.get("probe_lines") or [],
                                        item.get("block_map") or {})
    return out


def _attribution_artifact(recipe_hash: str, recipe_pristine_hash: str | None,
                          project_root: Path, causal_meta: dict,
                          file_sha256: "dict[str, str] | None",
                          target_files: "list[str] | None",
                          tests_face: "dict[str, str] | None",
                          arms: tuple[dict, ...], arms_attr: dict,
                          notes: list[str],
                          instrumentable_lines: "dict[str, list[int]] | None" = None,
                          baseline_meta: "dict | None" = None,
                          preflight_meta: "dict | None" = None) -> dict:
    """归属产物正文：行级主真值 + 派生视图 + 臂有效性 + 新鲜绑定
    （recipe/文件/测试面）+ 探针面（可插桩行全集）。

    baseline_meta：本构建生效的基线失败集披露（§SPEC-060a——显式提供或
    懒获取的方式/对数/内容哈希/耗时）；无基线构建（绿臂零额外 GUT）为
    None，键不出场。增量刷新经 increment 产物携带（消费者不读不改语义）。"""
    doc = {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": recipe_hash,
        # SEE-1581：pristine recipe hash（注入 toolchain 之前）——消费方
        # 新鲜判据用它做零 godot 依赖的现算对照
        "recipe_hash_pristine": recipe_pristine_hash,
        "project_root": str(project_root),
        "identity": _ATTRIBUTION_IDENTITY,
        "causal": causal_meta,
        # SEE-1581：文件级新鲜绑定（mutation 真值选面的 file hash 判据）
        "file_sha256": dict(file_sha256 or {}),
        "target_files": list(target_files or []),
        # 测试面内容绑定：测试脚本内容变化同样使映射过期（recipe 不含
        # 测试内容；覆盖边却依赖它）
        "tests_face": dict(tests_face or {}),
        # SEE-1587：每文件可插桩行全集（行级主真值的测量宇宙；Coverage
        # Store 投影的 instrumentable 锚直接取自此处）
        "instrumentable_lines": dict(instrumentable_lines or {}),
        "arms": arms_attr,
        "arms_state": _arms_state(arms),
        "notes": notes,
    }
    if baseline_meta is not None:
        doc["baseline_failures_meta"] = baseline_meta
    # §SPEC-PREFLIGHT×PLANFIRST：基线预检产物（status=partial 时
    # excluded_tests 具名在 meta 内）——映射消费者直读「按名排除」事实
    if preflight_meta is not None:
        doc["baseline_preflight"] = preflight_meta
    return doc

# §SPEC-083：报告节固定 notes（causal 追加项运行时按开关拼接）
_ATTRIBUTION_NOTES = (
    "lanes: test = hits inside a GUT test window (start_test..end_test); "
    "script (R1) = hits between start_script and end_script outside any "
    "test window (before_all/after_all, inter-test execution); "
    "unattributed is subdivided into named phase buckets (方向③): "
    "bootstrap = before the first start_script, script_gap = between "
    "scripts, shutdown = after GUT end_run, other = defensive fallback",
    "SEE-1587 line-primary: every block-entry probe carries the "
    "attribution block (hit-level edges over the full probe surface); "
    "per-test/per-script covered line sets are the primary truth "
    "(test_to_lines / script_to_lines / line_to_tests), and file/"
    "function views are derived projections from line facts plus the "
    "source registry; lane event counts are all-probe events (not "
    "comparable to the old fn-entry counts), and the acceptance arm "
    "runs the plain template (attribution off by design)",
    "every hit event is counted in exactly one lane/phase; rates use the "
    "three-lane sum as denominator — no hit is silently assigned",
    "unattributed_hotspots rank probe sites by unattributed hit events "
    "with line and function_id resolution",
    "this is a snapshot of one run bound to recipe_hash; code/"
    "test changes make it stale — rebuild periodically",
    "diagnostic evidence only — certification stays full-face "
    "(not_certified, exploratory)",
)


def _attribution_causal_meta(causal: bool,
                             causal_sites: "list[dict] | None") -> dict:
    """causal (R2) 元数据与追加 note（关闭时 enabled=False、零站点）。"""
    sites = causal_sites or []
    meta = {
        "enabled": bool(causal),
        "files_rewritten": len(sites),
        "sites_rewritten": sum(int(s["sites"]) for s in sites),
        "by_file": sites,
    }
    return meta


def _attribution_write_artifact(artifact: dict,
                                attribution_out: str | None) -> str | None:
    """产物落盘（attribution_out 非空时）；返回披露路径。"""
    if not attribution_out:
        return None
    out_p = Path(attribution_out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return str(out_p)


def _attribution_section(enabled: bool, arms: tuple[dict, ...],
                         recipe_hash: str, project_root: Path,
                         attribution_out: str | None,
                         causal: bool = False,
                         causal_sites: "list[dict] | None" = None,
                         file_sha256: "dict[str, str] | None" = None,
                         target_files: "list[str] | None" = None,
                         recipe_pristine_hash: str | None = None,
                         tests_face: "dict[str, str] | None" = None,
                         prepared: "dict[str, dict] | None" = None,
                         baseline_meta: "dict | None" = None,
                       preflight_meta: "dict | None" = None) -> dict:
    """§SPEC-083：measurement 报告的 test_attribution 节（归属产物在
    attribution_out 非空时落盘，与报告共用同一 notes 列表）。

    默认关闭返回空 dict——报告形状零变化。"""
    if not enabled:
        return {}
    notes = list(_ATTRIBUTION_NOTES)
    if causal:
        notes.append(
            "causal (R2): call_deferred/create_timer creation sites inside "
            "instrumented files are wrapped so callbacks run under the "
            "creator's context; deferred callbacks triggered from engine "
            "internals, uninstrumented code/addons, or after shutdown remain "
            "unattributed — coverage is bounded by those call sites only")
    causal_meta = _attribution_causal_meta(causal, causal_sites)
    arms_attr = {arm["arm"]: arm.get("attribution")
                 for arm in arms
                 if arm.get("attribution") is not None}
    artifact = _attribution_artifact(
        recipe_hash, recipe_pristine_hash, project_root, causal_meta,
        file_sha256, target_files, tests_face, arms, arms_attr, notes,
        instrumentable_lines=_instrumentable_lines_from_prepared(prepared),
        baseline_meta=baseline_meta,
        preflight_meta=preflight_meta)
    artifact_path = _attribution_write_artifact(artifact, attribution_out)
    return {"test_attribution": {
        "schema_version": ATTRIBUTION_SCHEMA,
        "enabled": True,
        "artifact": artifact_path,
        "file_sha256_count": len(file_sha256 or {}),
        "identity": _ATTRIBUTION_IDENTITY,
        "causal": causal_meta,
        "arms": {
            name: {k: amap.get(k) for k in _ATTRIBUTION_SUMMARY_KEYS}
            for name, amap in arms_attr.items()},
        "notes": notes,
    }}
