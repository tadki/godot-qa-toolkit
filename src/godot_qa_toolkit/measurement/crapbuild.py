"""SEE-1594 BTIDY（零行为拆分）：CRAP 记录构建与 stability baseline
读取——车道/发射/回放/基线校验。自 orchestrator.py 原行段物理迁移；
`_load_baseline_report` 按「文档读取 / union 行 / per_fid 行 / crap 记录」
分解为 ≤50 行函数，校验语义逐语句等价。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .backup import MeasurementError
from .shadowio import _read_hits_merged


_FAIL_LANES = ("timeout", "run_error", "vacuous", "no_probe_fired",
               "zero_tests", "suite_invalid")


def _lane_from_reason(reason: str | None) -> str:
    """union 无效原因 → 失败车道。失败样本一律 not-scored（§SPEC-053）。"""
    for lane in _FAIL_LANES:
        if reason and lane in reason:
            return lane
    return "invalid"


_SEM_STATEMENT = "statement_start_line"
_SEM_BLOCK = "block_entry_approximation"

def _crap_emit(records: list, lanes: dict, recipe_hash: str, rel: str,
               entry: dict, *, state: str, covered: int | None = None,
               total: int | None = None,
               coverage_semantics: str = _SEM_STATEMENT) -> None:
    from ..risk.contract import build_shadow_record

    scope = entry.get("lex_scope") or []
    records.append(build_shadow_record(
        function_id=entry["fid"],
        name=entry.get("name", ""),
        lex_path=(".".join(str(x) for x in scope)
                  or entry.get("name", "")),
        start_line=int(entry.get("start_line") or 0),
        end_line=int(entry.get("end_line") or 0),
        # registry 的 CC 字段名为 cc_oracle（§SPEC-053 连接契约）；
        # 缺失即 cc=0 会产出伪零分，必须显式失败
        cc=int(entry["cc_oracle"]),
        covered=covered, total=total,
        cov_source=("canonical_union" if state == "scored"
                    else "not_measured"),
        state=state,
        recipe={"recipe_hash": recipe_hash, "method": "canonical-union/1"},
        lambda_gap=int(entry.get("lambda_gap") or 0),
        # §SPEC-070 FR-4/R26：record 级语义按文件测量形态区分（块压缩文件
        # 不得冒充精确 statement_start_line）
        coverage_semantics=coverage_semantics,
    ))
    lanes[state] = lanes.get(state, 0) + 1


def _crap_emit_file_unmeasured(records: list, lanes: dict,
                               recipe_hash: str, rel: str, entries: list,
                               lane: str, unstable_file: bool,
                               coverage_semantics: str = _SEM_STATEMENT
                               ) -> None:
    """union 缺失/无效文件：整文件按失败车道落记录（§SPEC-053），基线
    消失文件按 §SPEC-057 覆写为显式 unstable。"""
    for e in entries:
        _crap_emit(records, lanes, recipe_hash, rel, e,
                   state=("unstable" if unstable_file else lane),
                   coverage_semantics=coverage_semantics)


def _crap_emit_file_scored(records: list, lanes: dict, recipe_hash: str,
                           rel: str, entries: list, u: dict,
                           unstable_keys: set,
                           coverage_semantics: str = _SEM_STATEMENT) -> None:
    """union 有效文件：per-fid 连接后按 unknown/unstable/scored 三车道
    落记录（§SPEC-053/056）。"""
    per = {row["fid"]: row for row in u["union"]["per_fid"]}
    for e in entries:
        row = per.get(e["fid"])
        # §SPEC-060d：车道判定先 unstable 后 unknown——文件内 fid 消失
        # （基线有、本轮 per_fid 无）会同时命中 unstable 键与 row None，
        # 必须落 unstable 显式车道，不得落 unknown 掩盖稳定性缺陷
        if ("res://" + rel, e["fid"]) in unstable_keys:
            _crap_emit(records, lanes, recipe_hash, rel, e, state="unstable",
                       covered=(int(row["covered"]) if row else None),
                       total=(int(row["total"]) if row else None),
                       coverage_semantics=coverage_semantics)
        elif row is None or int(row.get("total") or 0) <= 0:
            # 缺分母/未入 probe 面：unknown 不出分（不冒充 100%）
            _crap_emit(records, lanes, recipe_hash, rel, e, state="unknown",
                       coverage_semantics=coverage_semantics)
        else:
            _crap_emit(records, lanes, recipe_hash, rel, e, state="scored",
                       covered=int(row["covered"]),
                       total=int(row["total"]),
                       coverage_semantics=coverage_semantics)


def _crap_replay_vanished_baseline(records: list, lanes: dict,
                                   baseline_records: dict,
                                   emitted_fids: set,
                                   prepared: dict) -> None:
    """§SPEC-057：基线 scored 但本轮缺席的函数（目标集/union 变化）→
    从基线记录回放为显式 unstable（不出分），杜绝静默消失。

    §SPEC-071 L2：回放 record 的 coverage_semantics 与本轮该文件实测形态
    对账——rel 在本轮 prepared 内则改写为当前形态（record/crap_line 两处
    一致）；rel 不在本轮测量面时保留 baseline 原值并显式标注 carried
    （provenance 已含 baseline_vanished_this_run），不得无对账混标。"""
    for fid, brec in baseline_records.items():
        if fid in emitted_fids:
            continue
        if brec.get("score_status") != "scored":
            continue
        nr = dict(brec)
        nr["score_status"] = "unstable"
        cl = dict(brec.get("crap_line") or {})
        cl["score"] = None
        cl["score_status"] = "unstable"
        rel = fid.split("::", 1)[0]
        item = prepared.get(rel)
        if item is not None:
            sem = (_SEM_BLOCK if item.get("block_map")
                   else _SEM_STATEMENT)
            nr["coverage_semantics"] = sem
            cl["coverage_semantics"] = sem
        else:
            cl.setdefault("coverage_semantics",
                          nr.get("coverage_semantics",
                                 _SEM_STATEMENT))
            nr["coverage_semantics_carried_from_baseline"] = True
        nr["crap_line"] = cl
        nr["provenance"] = dict(brec.get("provenance") or {},
                                stability="baseline_vanished_this_run")
        records.append(nr)
        lanes["unstable"] = lanes.get("unstable", 0) + 1


def _cause_tail(te) -> str:
    """MeasurementError __cause__（TimeoutExpired）的 stdout/stderr 安全解码
    ——TimeoutExpired 恒携带 bytes（即便 subprocess text=True）。"""
    if te is None:
        return ""
    so = getattr(te, "stdout", "") or ""
    se = getattr(te, "stderr", "") or ""
    if isinstance(so, bytes):
        so = so.decode("utf-8", "replace")
    if isinstance(se, bytes):
        se = se.decode("utf-8", "replace")
    return so + "\n" + se

def _pairs_sha256(pairs: "set[tuple[str, str]] | list") -> str:
    """基线失败对内容哈希（披露用；排序后序列化，跨 run 可比）。"""
    norm = sorted([list(p) for p in pairs])
    return hashlib.sha256(
        json.dumps(norm, ensure_ascii=False).encode("utf-8")).hexdigest()

def _attach_limitation_reasons(records: list, limitations: list) -> None:
    """§SPEC-061c：unstable 记录命中声明的覆盖限制文件 → 具名理由写入
    provenance.stability_reason（不得静默、不得伪造确定性）。"""
    if not limitations:
        return
    for r in records:
        if r.get("score_status") != "unstable":
            continue
        for lim in limitations:
            if r.get("function_id", "").startswith(
                    str(lim["file"]) + "::"):
                prov = dict(r.get("provenance") or {})
                prov["stability_reason"] = str(lim["reason"])
                r["provenance"] = prov


def _crap_records_for_report(unions, prepared, recipe_hash,
                             unstable_keys: set | None = None,
                             baseline_records: dict | None = None):
    """§SPEC-053：函数级 CRAP 记录——union 行集（C/I）与 registry 的
    identity/CC 按 fid 连接；任何失败/无分母车道一律 score=None。"""
    from ..risk.formula import CRAP_LINE_VERSION

    by_file = {u["file"]: u for u in unions}
    records: list[dict] = []
    lanes: dict[str, int] = {}

    unstable_keys = unstable_keys or set()

    for rel, item in prepared.items():
        entries = item["registry"].get("entries", [])
        u = by_file.get("res://" + rel)
        if u is None or not u.get("valid"):
            lane = (_lane_from_reason(u.get("invalid_reason"))
                    if u is not None else "unknown")
            # §SPEC-057：基线内该文件曾有覆盖、本轮 union 消失 → 整文件
            # unstable（显式车道，不出分）
            unstable_file = (f"res://{rel}", "__file_vanished__") \
                in unstable_keys
            _crap_emit_file_unmeasured(records, lanes, recipe_hash, rel,
                                       entries, lane, unstable_file,
                                       coverage_semantics=(
                                           _SEM_BLOCK if
                                           prepared[rel].get("block_map")
                                           else _SEM_STATEMENT))
            continue
        _crap_emit_file_scored(records, lanes, recipe_hash, rel, entries,
                               u, unstable_keys,
                               coverage_semantics=(
                                   _SEM_BLOCK if
                                   prepared[rel].get("block_map")
                                   else _SEM_STATEMENT))
    if baseline_records:
        emitted_fids = {r["function_id"] for r in records}
        _crap_replay_vanished_baseline(records, lanes, baseline_records,
                                       emitted_fids, prepared)
    return records, lanes, CRAP_LINE_VERSION


def _is_baseline_line_id(value) -> bool:
    """§SPEC-067/068/071 L3：行集元素须为**非负**数值行号——非负整数、
    非负整值浮点或全数字字符串（sink 落盘形态 str(i)）。bool 是 int
    子类但语义上不是行号——frozenset({True}) == frozenset({1}) 会伪造
    第 1 行已覆盖；NaN/±Infinity 入集会污染比较；负数、非整数浮点
    （2.5/-2.0）与非数字字符串都不是行号。"""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    if isinstance(value, str):
        return value.isdigit()
    return isinstance(value, float) and math.isfinite(value) \
        and value.is_integer() and value >= 0


def _baseline_total(value) -> int:
    """§SPEC-067/068：total 分母仅允许非负有限整数值或 null——bool 不得
    当数字，NaN/±Infinity 拒绝，负数与非整数拒绝（3.7 不得截断、-1 非法
    分母）。"""
    if value is None:
        return 0
    total = None
    if isinstance(value, bool):
        pass
    elif isinstance(value, int):
        total = value
    elif isinstance(value, float) and math.isfinite(value) \
            and value.is_integer():
        total = int(value)
    if total is None or total < 0:
        raise MeasurementError(
            f"stability baseline per_fid row 'total' must be a non-negative "
            f"finite integer value or null, got {value!r} (§SPEC-068)")
    return total

def _read_baseline_doc(path: str) -> dict:
    """读取 stability baseline 文件并要求 JSON object（§SPEC-066）。"""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MeasurementError(
            f"stability baseline unreadable: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise MeasurementError(
            f"stability baseline must be a JSON object, got "
            f"{type(doc).__name__} (§SPEC-066)")
    return doc


def _baseline_fid_rows(per_fid: list, file: str) -> dict:
    """单文件 per_fid 行校验与行集解析（§SPEC-066/067/068/060d）。"""
    rows: dict = {}
    for row in per_fid:
        if not isinstance(row, dict):
            raise MeasurementError(
                f"stability baseline per_fid row must be an object, "
                f"got {row!r} (§SPEC-066)")
        fid = row.get("fid")
        if not isinstance(fid, str) or not fid.strip():
            raise MeasurementError(
                f"stability baseline per_fid row 'fid' must be a "
                f"non-empty string, got {fid!r} (§SPEC-067)")
        if fid in rows:
            raise MeasurementError(
                f"stability baseline duplicate fid {fid!r} in file "
                f"{file!r} (§SPEC-068)")
        covered = row.get("covered_line_ids")
        # frozenset 消费边界：非 list（裸字符串/缺键）或含容器/None/
        # bool/非有限数值元素会在比较语义上 TypeError、伪造或漂移，
        # 读取前拒绝
        if not isinstance(covered, list) or not all(
                _is_baseline_line_id(x) for x in covered):
            raise MeasurementError(
                f"stability baseline per_fid row 'covered_line_ids' "
                f"must be a list of finite scalars, got {covered!r} "
                f"(§SPEC-067)")
        # §SPEC-060d：行集与 total 分母一并入基线（同行集但分母变化 =
        # 车道迁移，不允许静默保持 scored）
        rows[fid] = (frozenset(covered),
                     _baseline_total(row.get("total")))
    return rows


def _baseline_union_row(u, seen_files: set) -> "tuple[str, dict] | None":
    """单 union 行校验；null 行只参与 file 身份去重（§SPEC-070 FR-5），
    返回 None；有效行返回 (file, rows)。"""
    if not isinstance(u, dict):
        raise MeasurementError(
            f"stability baseline union row must be an object, got "
            f"{u!r} (§SPEC-066)")
    if u.get("union") is None:
        # §SPEC-070 FR-5：null 行同样参与 file 身份去重——否则 null 行
        # + 有效行可绕过重复检测
        file_raw = u.get("file")
        if isinstance(file_raw, str) and file_raw.strip():
            if file_raw in seen_files:
                raise MeasurementError(
                    f"stability baseline duplicate union row for file "
                    f"{file_raw!r} (§SPEC-068)")
            seen_files.add(file_raw)
        return None
    union = u["union"]
    if not isinstance(union, dict):
        raise MeasurementError(
            f"stability baseline union row 'union' must be an object "
            f"or null, got {union!r} (§SPEC-066)")
    file = u.get("file")
    if not isinstance(file, str) or not file.strip():
        raise MeasurementError(
            f"stability baseline union row 'file' must be a non-empty "
            f"string, got {file!r} (§SPEC-067)")
    # §SPEC-068：重复身份明确拒绝——last-wins 静默覆盖会伪造基线
    if file in seen_files:
        raise MeasurementError(
            f"stability baseline duplicate union row for file "
            f"{file!r} (§SPEC-068)")
    seen_files.add(file)
    per_fid = union.get("per_fid", [])
    if not isinstance(per_fid, list):
        raise MeasurementError(
            f"stability baseline 'per_fid' must be a list, got "
            f"{per_fid!r} (§SPEC-066)")
    return file, _baseline_fid_rows(per_fid, file)


def _baseline_crap_records(doc: dict) -> dict:
    """crap.records 读取校验（§SPEC-066/067/068）：fid 唯一、回放字段
    对象形态、score_status 非空白。"""
    crap = doc.get("crap", {})
    if not isinstance(crap, dict):
        raise MeasurementError(
            f"stability baseline 'crap' must be an object, got {crap!r} "
            f"(§SPEC-066)")
    records_list = crap.get("records", [])
    if not isinstance(records_list, list):
        raise MeasurementError(
            f"stability baseline 'crap.records' must be a list, got "
            f"{records_list!r} (§SPEC-066)")
    records: dict = {}
    for r in records_list:
        if not isinstance(r, dict):
            raise MeasurementError(
                f"stability baseline crap record must be an object, got "
                f"{r!r} (§SPEC-066)")
        fid = r.get("function_id")
        if not isinstance(fid, str) or not fid.strip():
            raise MeasurementError(
                f"stability baseline crap record 'function_id' must be a "
                f"non-empty string, got {fid!r} (§SPEC-067)")
        if fid in records:
            raise MeasurementError(
                f"stability baseline duplicate function_id {fid!r} "
                f"(§SPEC-068)")
        # §SPEC-067：回放字段——`_crap_replay_vanished_baseline` 对
        # crap_line / provenance 直接 dict(...)，非对象即 TypeError/ValueError
        for field in ("crap_line", "provenance"):
            v = r.get(field)
            if v is not None and not isinstance(v, dict):
                raise MeasurementError(
                    f"stability baseline crap record {field!r} must be an "
                    f"object or null, got {v!r} (§SPEC-067)")
        # score_status 只经等值比较消费；非 str / 空白会静默失配 replay
        if "score_status" in r:
            status = r["score_status"]
            if not isinstance(status, str) or not status.strip():
                raise MeasurementError(
                    f"stability baseline crap record 'score_status' must "
                    f"be a non-empty string, got {status!r} (§SPEC-067)")
        records[fid] = r
    return records


def _load_baseline_report(path: str) -> tuple[dict, dict]:
    """§SPEC-056/057 stability baseline：读取历史 measurement 报告的逐 fid
    行集与函数级 CRAP 记录。§SPEC-066/067：根与**全部直接消费字段**
    （unions 行/union/per_fid 行/file/fid/covered_line_ids/total、
    crap.records 的 function_id/score_status/crap_line/provenance）在
    读取前完成系统校验；身份串 strip 后非空、数值面 bool/NaN/Inf 拒绝，
    非法形状 MeasurementError，不泄漏 AttributeError/TypeError/KeyError。"""
    doc = _read_baseline_doc(path)
    unions = doc.get("unions", [])
    if not isinstance(unions, list):
        raise MeasurementError(
            f"stability baseline 'unions' must be a list, got {unions!r} "
            f"(§SPEC-066)")
    lines: dict = {}
    seen_files: set = set()
    for u in unions:
        parsed = _baseline_union_row(u, seen_files)
        if parsed is not None:
            lines[parsed[0]] = parsed[1]
    return lines, _baseline_crap_records(doc)


def _unstable_keys(unions, baseline_lines: dict) -> set:
    """§SPEC-057/060d：比较 fid 集合本身与状态迁移——基线消失/新增/行集
    变化/total 分母变化全部捕获，杜绝「只比较当前仍存在 fid」与「只比
    行集不比分母」的漏报。"""
    keys: set = set()
    cur_files = {}
    for u in unions:
        if u.get("union") is None:
            continue
        cur = {row["fid"]: (frozenset(row["covered_line_ids"]),
                            int(row.get("total") or 0))
               for row in u["union"].get("per_fid") or []}
        cur_files[u["file"]] = cur
        base = baseline_lines.get(u["file"], {})
        for fid in set(base) | set(cur):
            b = base.get(fid, (frozenset(), 0))
            c = cur.get(fid, (frozenset(), 0))
            if b != c:
                keys.add((u["file"], fid))
    # 基线中存在但本轮 union 完全消失的文件（整文件不可复现）
    for bfile, bper in baseline_lines.items():
        if bfile not in cur_files and bper:
            keys.add((bfile, "__file_vanished__"))
    return keys
