"""coverage-union/1（§SPEC-036）：两臂覆盖行集并集契约。

有效性矩阵（任一不满足 → valid=false 不出分，fail-loud 不带病 union）：
同源码 sha256、同 probe manifest 哈希、两臂 run_uuid 不同、两臂 bundle 均
state=valid、整臂替换禁拼接（两臂臂名必须不同且各引用单一 run_uuid）、
**禁平均**——coverage = |∪C| / total（行集并集，任何百分比平均都是实现
缺陷，反向探针 fixture 钉死）。comparable 仅在两臂 recipe_hash 一致且等于
canonical recipe_hash 时 true（unit-only/core/affected 子集为 false）。
"""

from __future__ import annotations

import hashlib
import json

UNION_VERSION = "coverage-union/1"


def _canonical_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def arm_record_from_bundle(bundle: dict, *, recipe_hash: str,
                           wall_s: float = 0.0) -> dict:
    """把 coverage-evidence/2 bundle 归一化为 union 消费的臂记录。

    state 判定（§SPEC-026 四异常态 + no-probe 哨兵）：run_error/timeout/
    zero_tests/vacuous 任一为真 → invalid；probes_fired==0 → no_probe
    invalid（空命中 ≠ 真零覆盖，禁带病并集）。
    """
    sent = bundle["sentinels"]
    state, reason = "valid", None
    for lane in ("run_error", "timeout", "zero_tests", "vacuous"):
        if sent.get(lane):
            state, reason = "invalid", lane
            break
    if state == "valid" and not sent.get("probes_fired"):
        state, reason = "invalid", "no_probe_fired"
    manifest = bundle["probe_manifest"]["inline"]
    return {
        "arm": bundle["recipe"]["arm"],
        "run_uuid": bundle["run_uuid"],
        "file": bundle["target"]["file"],
        "target_sha256": bundle["target"]["sha256"],
        "probe_manifest_sha256": hashlib.sha256(
            _canonical_json(manifest).encode("utf-8")).hexdigest(),
        "recipe_hash": recipe_hash,
        "state": state,
        "state_reason": reason,
        # C 行集（fid → covered line ids）与 I 分母（fid → i_lines），
        # union 用；此处直接引用 bundle 的行（不拷贝大数组）
        "C": {row["fid"]: row["covered_line_ids"] for row in bundle["C"]},
        "I": {row["fid"]: row["i_lines"] for row in bundle["I"]},
        "probes_total": manifest["census"]["probes_total"],
        "wall_s": wall_s,
    }


def union_file(arm_a: dict, arm_b: dict, *,
               canonical_recipe_hash: str | None = None) -> dict:
    """两臂并集。invalid 时 union=null + invalid_reason（不出分）。"""
    arms_meta = [
        {"arm": arm_a["arm"], "run_uuid": arm_a["run_uuid"],
         "state": arm_a["state"], "covered": sum(len(v) for v in
                                                 arm_a["C"].values()),
         "wall_s": arm_a["wall_s"]},
        {"arm": arm_b["arm"], "run_uuid": arm_b["run_uuid"],
         "state": arm_b["state"], "covered": sum(len(v) for v in
                                                 arm_b["C"].values()),
         "wall_s": arm_b["wall_s"]},
    ]
    rec: dict = {
        "union_version": UNION_VERSION,
        "file": arm_a["file"],
        "target_sha256": arm_a["target_sha256"],
        "probe_manifest_sha256": arm_a["probe_manifest_sha256"],
        "recipe_hash": arm_a["recipe_hash"],
        "comparable": (
            canonical_recipe_hash is not None
            and arm_a["recipe_hash"] == canonical_recipe_hash
            and arm_b["recipe_hash"] == canonical_recipe_hash
        ),
        "arms": arms_meta,
        "union": None,
        "valid": False,
        "invalid_reason": None,
    }

    def invalid(reason: str) -> dict:
        rec["invalid_reason"] = reason
        return rec

    if arm_a["arm"] == arm_b["arm"]:
        return invalid(f"duplicate_arm:{arm_a['arm']}")
    if arm_a["target_sha256"] != arm_b["target_sha256"]:
        return invalid("source_sha256_mismatch")
    if arm_a["probe_manifest_sha256"] != arm_b["probe_manifest_sha256"]:
        return invalid("probe_manifest_mismatch")
    if arm_a["run_uuid"] == arm_b["run_uuid"]:
        return invalid("run_uuid_reuse")
    for arm in (arm_a, arm_b):
        if arm["state"] != "valid":
            return invalid(f"arm_invalid:{arm['arm']}:{arm['state_reason']}")

    # 禁平均：per-fid 与整文件一律行集并集 |∪C|
    per_fid: list[dict] = []
    all_ids: set[int] = set()
    for fid in sorted(set(arm_a["C"]) | set(arm_b["C"])):
        ids = set(arm_a["C"].get(fid, [])) | set(arm_b["C"].get(fid, []))
        all_ids |= ids
        per_fid.append({
            "fid": fid,
            "covered": len(ids),
            "total": arm_a["I"].get(fid, 0),
            "covered_line_ids": sorted(ids),
        })
    rec["union"] = {
        "per_fid": per_fid,
        "covered": len(all_ids),
        "total": arm_a["probes_total"],
        "method": "line_union",
    }
    rec["valid"] = True
    return rec
