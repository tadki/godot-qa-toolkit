"""SEE-1594 BTIDY（零行为拆分）：mutation 父层分片执行与按文件合并——
chunk worker、聚合累加、单文件 summary 组装、attention 投影与输入序重排。
自 executor.py 原行段物理迁移；`_accumulate_chunk`、`_merge_results` 分解
为 ≤50 行函数。

**patch seam 纪律**：`run_mutation` 被测试钉在 `executor.*`，chunk worker
经 `_x()` 运行时解析——调用链与拆分前逐点等价。"""

from __future__ import annotations

from .reporting import SURVIVOR_DETAIL_KEYS, SURVIVORS_CAP
from .coverlink import coverage_prefilter_summary


def _x(name: str):
    """运行时经 executor 模块命名空间解析共享 helper（patch seam 保持）。"""
    import godot_qa_toolkit.mutation.executor as _e

    return getattr(_e, name)


def _chunk_worker(payload: dict) -> dict:
    """worker 进程入口：消费共享 baseline 与父层差分规划的 retry_ids 分片。

    baseline 快照缺失（冷启动失败 / 脏 baseline）时退回串行单任务
    （serial=True，无 retry_ids、worker 自跑 baseline 并自建差分）。
    v2 条目由 run_mutation 内增量落盘——各 worker 只写各自 retry_ids 的
    条目文件（分片零重叠），manifest 索引层由父层收尾单点写。
    timeout_factor（§SPEC-1420-02）随 payload 穿线。
    identity（SEE-1550）：父层身份包随 payload 注入——多 worker 同 key。
    """
    binary_kind = payload.get("binary_kind")
    timeout_factor = payload.get("timeout_factor")
    identity = payload.get("identity")
    # §SPEC-012：worker 与父层同源遵守 select_mode（payload 携带父层
    # 派发值；tests_glob=None 的显式全脸 chunk 不得被 worker 侧重扫降级）。
    select_mode = payload.get("selection_mode") or "full"
    if payload.get("serial"):
        return _x("run_mutation")(
            payload["file"], payload["project_root"], budget=payload["budget"],
            timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
            binary_kind=binary_kind,
            timeout_factor=timeout_factor,
            mutate_all=bool(payload.get("mutate_all")),
            progress=payload.get("progress"),
            identity=identity,
            worker_channel=payload.get("worker_channel"),
            select_mode=select_mode,
            selection_meta=payload.get("selection_meta"))
    return _x("run_mutation")(
        payload["file"], payload["project_root"], budget=payload["budget"],
        timeout_s=payload["timeout_s"], tests_glob=payload["tests_glob"],
        precomputed_baseline=payload["baseline"],
        retry_ids=payload["retry_ids"],
        binary_kind=binary_kind,
        timeout_factor=timeout_factor,
        progress=payload.get("progress"),
        identity=identity,
        worker_channel=payload.get("worker_channel"),
        select_mode=select_mode,
        selection_meta=payload.get("selection_meta"),
    )
def _merge_prefilter_disclosure(cur, pf) -> object:
    """SEE-1587：prefilter 披露跨分片合并——dict 计数求和、source 取首个
    非空；字符串态（store_miss 等）最后写入者为准。"""
    if pf is None:
        return cur
    if isinstance(pf, dict):
        if isinstance(cur, dict):
            # A08：聚合同样走唯一 writer（计数求和、source 取首个非空）
            return coverage_prefilter_summary(
                cur.get("source") or pf.get("source"),
                (cur.get("uncovered") or 0) + (pf.get("uncovered") or 0),
                (cur.get("covered_executed") or 0)
                + (pf.get("covered_executed") or 0))
        return pf
    if not isinstance(cur, dict):
        return pf
    return cur


def _accumulate_chunk(agg: dict, cr: dict,
                      passthrough_keys: tuple[str, ...]) -> None:
    """把单个 chunk 累加进文件级聚合。"""
    s = cr["summary"]
    agg["jobs"] += 1
    agg["ok"] = agg["ok"] and bool(cr.get("ok", True))
    for k in passthrough_keys:
        if k in s and k != "coverage_prefilter":
            agg[k] = s[k]
    merged_pf = _merge_prefilter_disclosure(
        agg.get("coverage_prefilter"), s.get("coverage_prefilter"))
    if merged_pf is not None:
        agg["coverage_prefilter"] = merged_pf
    for k in ("mutants", "killed", "survived", "suspect",
              "timeout", "run_errors", "invalid_mutants", "resumed",
              "timeout_pinned"):
        agg[k] += s.get(k, 0)
    # O2（SEE-1420 qa 复验）：executed = 实际执行语义——worker 层已按
    # invalid 预检覆写，逐分片求和（父层不得回填计划面值）。
    if s.get("executed") is not None:
        agg["executed"] = (agg["executed"] or 0) + s["executed"]
    # SEE-1550 只加不改披露面：机制标签（passthrough）+ 槽位/新鲜度/killed
    # 证书/疑似孤儿（跨分片拼接——各分片槽位结论与证书并集）。
    agg["freshness"] += list(s.get("freshness") or [])
    agg["killed_by_certificates"] += list(s.get("killed_by_certificates") or [])
    agg["suspected_orphans"] += list(s.get("suspected_orphans") or [])
    # §SPEC-202（只加不改）：跨分片阶段耗时求和 + RSS 峰值取最大（分片级
    # VmHWM 是各 worker 进程自己的高水位——文件级取 max 为其诚实上界）。
    for k, v in (s.get("stage_timings") or {}).items():
        if isinstance(v, (int, float)):
            agg["stage_timings"][k] = agg["stage_timings"].get(k, 0.0) + v
    rss = s.get("rss_peak_mb")
    if isinstance(rss, (int, float)):
        agg["rss_peak_mb"] = max(agg["rss_peak_mb"], float(rss))
    agg["failures"].extend(cr.get("failures", []))
    agg["scoped"] = bool(s.get("scoped"))
    agg["budget"] = s.get("budget")
    agg["baseline_reused"] = bool(s.get("baseline_reused"))
    total = agg["mutants"]
    agg["kill_rate"] = round(agg["killed"] / total, 4) if total else 0.0


# §SPEC-010/§SPEC-006/§SPEC-002/008/SEE-1589：随分片透出的 passthrough 键
_PASSTHROUGH_KEYS = (
    "adaptive_timeout_s", "timing_source", "samples_n", "baseline_p90",
    "run_error", "error", "inject_scheme", "slot_mode", "exec_scheme",
    # SEE-1581：真值选面披露随分片结果透出（只增不改）
    "test_selection",
    # SEE-1587 §SPEC-006：prefilter 来源披露随分片透出（计数经
    # _accumulate_chunk 专用合并求和，source 取首个非空）
    "coverage_prefilter",
    # SEE-1589 B §SPEC-002/008：三项机制具名 reason 与阶段对账随分片透出
    "worker_channel", "farm", "stage_reconciliation",
    # SEE-1589 A：选面/并发披露随分片透出（_inject_one 再按组数修正）
    "selection_mode", "selection_groups", "jobs_limit_reason",
)


def _new_file_agg(file: str) -> dict:
    """单文件聚合初值。"""
    return {
        "file": file, "mutants": 0, "killed": 0, "survived": 0,
        "suspect": 0, "timeout": 0, "run_errors": 0,
        "invalid_mutants": 0, "resumed": 0, "timeout_pinned": 0,
        "executed": None,
        "jobs": 0, "ok": True, "failures": [], "scoped": None,
        "budget": None, "baseline_reused": None,
        "total_sites": None, "attention": None,
        "freshness": [], "killed_by_certificates": [],
        "suspected_orphans": [],
        "stage_timings": {}, "rss_peak_mb": 0.0,
    }


def _merge_results(chunks: list[dict], chunk_results: list[dict],
                   jobs: int) -> dict:
    """按文件合并分片契约：计数求和、failures 拼接、kill_rate 重算。

    resumed（§SPEC-010）：各 chunk 的 resumed 求和进聚合 summary——resume
    语义下同文件多 chunk 各 skip 各的（分片不重叠），求和即该文件总跳过
    数，与单文件扁平路径口径一致。
    """
    passthrough_keys = _PASSTHROUGH_KEYS
    per_file: dict[str, dict] = {}
    resumed_present = any("resumed" in cr["summary"] for cr in chunk_results)
    for chunk, cr in zip(chunks, chunk_results):
        agg = per_file.setdefault(chunk["file"], _new_file_agg(chunk["file"]))
        _accumulate_chunk(agg, cr, passthrough_keys)
        # §SPEC-1420-01：截断面 N/M 披露的父层聚合锚（截断前全量位点数）
        if agg["total_sites"] is None:
            agg["total_sites"] = chunk.get("total_sites")
        if agg.get("attention") is None:
            agg["attention"] = chunk.get("attention")
    results = [_file_summary(agg, passthrough_keys, resumed_present)
               for agg in per_file.values()]
    return {
        "tool": "mutation",
        "ok": all(r["ok"] for r in results),
        "jobs": len(chunks),
        "failures": [f for r in results for f in r.get("failures", [])],
        "results": results,
    }


def _truncation_disclosure(agg: dict) -> dict:
    """§SPEC-1420-01 截断面披露（total_sites > budget 时在场）。"""
    if agg["total_sites"] is None or agg["budget"] is None \
            or agg["total_sites"] <= agg["budget"]:
        return {}
    executed = (agg["executed"] if agg["executed"] is not None
                else min(agg["total_sites"], agg["budget"]))
    return {"total_sites": agg["total_sites"], "executed": executed}


def _see1550_disclosures(agg: dict) -> dict:
    """SEE-1550 只加不改披露面（跨分片拼接的槽位/新鲜度/证书/孤儿）。"""
    out: dict = {}
    if agg.get("freshness"):
        out["freshness"] = agg["freshness"]
    if agg.get("killed_by_certificates"):
        out["killed_by_certificates"] = agg["killed_by_certificates"]
    if agg.get("suspected_orphans"):
        out["suspected_orphans"] = agg["suspected_orphans"]
    return out


def _file_summary(agg: dict, passthrough_keys: tuple[str, ...],
                  resumed_present: bool) -> dict:
    """聚合态 → 单文件 result（summary 形态唯一出处，<800/复杂度纪律拆分）。"""
    ok = (agg["ok"] and agg["survived"] == 0 and agg["timeout"] == 0
          and agg["run_errors"] == 0 and agg["suspect"] == 0)
    total = agg["mutants"]
    kill_rate = round(agg["killed"] / total, 4) if total else 0.0
    summary = {
        "file": agg["file"],
        "jobs": agg["jobs"],
        "mutants": agg["mutants"],
        "killed": agg["killed"],
        "survived": agg["survived"],
        "suspect": agg["suspect"],
        "timeout": agg["timeout"],
        "run_errors": agg["run_errors"],
        "invalid_mutants": agg["invalid_mutants"],
        "kill_rate": kill_rate,
        "budget": agg["budget"],
        "scoped": agg["scoped"],
        "baseline_reused": agg["baseline_reused"],
        **({"timeout_pinned": agg["timeout_pinned"]}
           if agg["timeout_pinned"] else {}),
        **(_truncation_disclosure(agg)),
        **({"attention": agg["attention"]}
           if agg.get("attention") else {}),
        **{k: agg[k] for k in passthrough_keys if k in agg},
        **_see1550_disclosures(agg),
        **({"resumed": agg["resumed"]} if resumed_present else {}),
        "stage_timings": {k: round(v, 3)
                          for k, v in sorted(agg["stage_timings"].items())},
        "rss_peak_mb": round(agg["rss_peak_mb"], 1),
    }
    return {
        "tool": "mutation",
        "ok": ok,
        "summary": summary,
        "failures": agg["failures"],
    }


def _attention_details(results: list[dict]) -> list[dict]:
    """跨文件非 killed 明细合并（≤5 条）：只取带完整 mutant 字段的记录，
    error/no_sites 契约的 reason-only 失败项不进投影。"""
    details: list[dict] = []
    for r in results:
        for f in r.get("failures", []):
            if "verdict" in f:
                details.append({k: f[k] for k in SURVIVOR_DETAIL_KEYS
                                if k in f})
            if len(details) >= SURVIVORS_CAP:
                return details
    return details


def _order_results(files: list[str], merged: list[dict],
                   settled: list[dict]) -> list[dict]:
    """按输入文件顺序重排（merged 已按 chunk 序，settled 按收集序）。"""
    by_file: dict[str, dict] = {}
    for r in merged + settled:
        by_file[r["summary"]["file"]] = r
    return [by_file[f] for f in files if f in by_file]
