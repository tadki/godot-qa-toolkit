"""SEE-1581 方向① — attribution 映射增量重建（探索轮；SEE-1587 行级化）。

按变更分类计算失效面并重跑最小测试集，产出带 provenance 的增量映射
产物（schema 与全量归属产物同形，另附 `increment` 块）：

- 只改测试脚本 → 重跑该脚本、整体替换其全部行事实（test→lines /
  script→lines）与派生边；
- 改生产文件 → 失效面 = 触达该文件的测试（行事实反查）∪ 静态索引直连
  消费者（gdself depth==1），重跑候选脚本并整体替换其行事实；
- 新增生产文件 / recipe_hash 失配 / 校准到期 / 重跑面过大 → 回退全脸
  （内部转调 run_measurement，provenance.mode 标注原因）；
- 删除测试脚本 → 仅删边不重跑；改名 = 旧脚本删边 + 新脚本重跑。
- 删除生产文件（SEE-1610 §SPEC-N1，`prod_removed`）→ 失效面 = 行事实
  反查触达测试 ∪ 静态直连消费者，重跑并整体替换其行事实；旧图该文件
  死边从行事实主真值 purge（派生视图单源重算自动干净）；target 面/
  文件哈希/可插桩面同步剔除；
- 其余路径（非 .gd、无基线边的删除文件等）→ 具名 `IncrementError`
  fail-loud 拒绝（不静默披露后照跑）。

SEE-1587：行事实是主真值——file/function 视图与 line→tests 反查从合并后
行事实单源重算（`_derive_views_from_line_facts`），不独立拼接。

如实边界：车道事件计数（hit_events/rates/phases/hotspots）不做增量合并，
增量产物标记 `event_counts_stale: true`（结构统计 tests/files/pairs/
function_pairs/line_pairs 由合并后的行事实精确重算）。行为耦合超出圈定面、
单脚本运行与全脸运行的状态差异，都会体现为增量 vs 全量的边差分——差异
逐边披露，不用不一致的数据冒充新鲜（`diff_artifacts` 是差分的唯一出处）。
"""

from __future__ import annotations
from ..digest import sha256_file

import json
import shutil
import sys
import time
from pathlib import Path

from .increment_runner import (
    execute_rerun_scripts,
    prepare_attr_templates,
)
from .attribution import (
    ATTRIBUTION_SCHEMA,
    _derive_views_from_line_facts,
    _fid_by_line_from_registry,
    _line_facts_views,
    _line_pair_counts,
    _probe_surface_lines,
    build_attribution_map,
)
from .shadowio import (
    _read_attr_merged,
    _read_registries,
)
from .orchestrator import (
    _inject_toolchain,
    make_run_uuid,
    run_measurement,
)

# SEE-1610 §SPEC-TH1 实测校准（KOL 824 文件/285 脚本 warm 副本）：增量墙钟
# ≈ 16s + 8.9s×重跑脚本数（S1 1.75%/5 脚本 → 60.5s，S2 37.5%/107 → 966s，
# S3 44.9%/128 → 1162s），暖全脸 223-227s——交叉点 ≈ 23.6 脚本（~8.3% 占比）。
# 0.5 在中位面上让增量比全脸慢 4.3-5.2 倍，故收紧到 0.05（≈14 脚本，帽内
# 增量仍 ≤ ~140s，保住 ≥38% 节省且留出噪声带余量）。
DEFAULT_RERUN_FULL_FACE_RATIO = 0.05
# SEE-1631 §SPEC-016：gherkin 臂自身的重跑比例上限（与 gut_full 同语义的
# 经济性闸——重跑面超过基线 feature 数 × cap 时，整臂全脸回退更便宜）。
# 校准基准待 KOL 复测由 Owner 判定；初值沿用 gut 侧实测口径。
DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO = 0.05
_FWD_KEYS = ("test_to_files", "test_to_functions",
             "script_to_files", "script_to_functions")
_LINE_KEYS = ("test_to_lines", "script_to_lines")

class IncrementError(Exception):
    """增量入参/基线违例（CLI 转领域 JSON）。"""

def load_baseline(path: str | Path) -> dict:
    try:
        baseline = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise IncrementError(f"baseline load failed: {e}") from e
    if baseline.get("schema_version") != ATTRIBUTION_SCHEMA:
        raise IncrementError(
            f"baseline schema {baseline.get('schema_version')!r} != "
            f"{ATTRIBUTION_SCHEMA!r}")
    arm = (baseline.get("arms") or {}).get("gut_full")
    if not isinstance(arm, dict) or "test_to_files" not in arm:
        raise IncrementError("baseline missing arms.gut_full edge maps")
    return baseline

def _script_name_of(identity: str) -> str:
    """测试身份 → 脚本 basename。C3-R21（D2）：真建图身份的脚本段是
    完整 res:// 路径（§SPEC-030 同名保留），合成夹具仍是裸 basename——
    失效面/rerun 匹配统一按 basename 口径，两形态在此归一。"""
    return Path(identity.split("::", 1)[0]).name

def _baseline_script_names(baseline: dict) -> set[str]:
    arm = baseline["arms"]["gut_full"]
    names = {_script_name_of(i) for i in (arm.get("test_to_files") or {})}
    names |= {Path(k).name for k in (arm.get("script_to_files") or {})}
    return names

def _fid_file_of(fid: str) -> str:
    return fid.split("::", 1)[0]

def _prod_classify_index(arm: dict) -> tuple:
    """生产文件分类索引（SEE-1610 §SPEC-N1）：file/fn 边全集。"""
    edge_files = (set(arm.get("file_to_tests") or {})
                  | set(arm.get("file_to_scripts") or {}))
    known_fids = {fid for ids in (arm.get("test_to_functions") or {}).values()
                  for fid in ids}
    known_fids |= {fid for ids in (arm.get("script_to_functions") or {})
                   .values() for fid in ids}
    return edge_files, known_fids


def _known_test_names(arm: dict, baseline: dict) -> "set[str]":
    """测试删除的 known 判定：身份全集 ∪ 基线测试面脚本名。

    SEE-1610：行事实是主真值，身份全集须并入行事实身份；C3-R8：还须认
    基线测试面（tests_face）——零归属边的测试（只 assert 自足的填充脚本）
    删除时无身份可查，此前被判 unknown 触发全量重建；因素表第 6 行要求
    按 test_removed 增量剔除。"""
    idents = set(arm.get("test_to_functions") or {}) | set(
        arm.get("script_to_functions") or {})
    idents |= set(arm.get("test_to_lines") or {})
    idents |= set(arm.get("script_to_lines") or {})
    names = {_script_name_of(i) for i in idents}
    names |= {Path(rel).name
              for rel in (baseline.get("tests_face") or {})}
    return names


def classify_changes(root: Path, changed: list[str],
                     baseline: dict) -> dict[str, list[str]]:
    """变更分类（各类返回相对 root 的路径列表）。

    - test：`tests/` 下的 test_*.gd；存在且在基线=changed、存在不在基线
      =new、不在盘上且在基线=removed；
    - prod：目标面内 .gd 且有基线边（file 边或 fn 边前缀）=changed，
      无基线边=new（触发全脸回退——不猜失效面），有基线边且不在盘上
      =prod_removed（SEE-1610 §SPEC-N1，清死边 + 触达面重跑）；
    - feature（SEE-1631）：`addons/gqt_gherkin/features/*.feature` 按
      normalize stem 对基线 gherkin 臂 feature key——在基线且在盘
      =feature_changed、不在基线=feature_new、在基线不在盘=feature_removed；
    - 其余路径拒绝（unknown，fail-loud 不静默）。"""
    arm = baseline["arms"]["gut_full"]
    edge_files, known_fids = _prod_classify_index(arm)
    known_test_names = _known_test_names(arm, baseline)
    feature_keys = _baseline_gherkin_feature_keys(baseline)
    out: dict[str, list[str]] = {"test_changed": [], "test_new": [],
                                 "test_removed": [], "prod_changed": [],
                                 "prod_new": [], "prod_removed": [],
                                 "content_changed": [], "content_new": [],
                                 "content_removed": [],
                                 "feature_changed": [], "feature_new": [],
                                 "feature_removed": [],
                                 "unknown": []}
    content_fingerprints = dict(baseline.get("production_content_sha256")
                                or {})
    for raw in changed:
        verdict = _classify_one(root, raw, edge_files, known_fids,
                                known_test_names, feature_keys,
                                content_fingerprints)
        out[verdict[0]].append(verdict[1])
    return out


_FEATURES_REL_PREFIX = "addons/gqt_gherkin/features/"


def _baseline_gherkin_feature_keys(baseline: dict) -> set[str]:
    """基线 gherkin 臂的 feature key 全集（feature_to_lines 权威 + 行事实
    身份前缀反推兜底——旧增量产物可能缺 feature 视图）。"""
    from .attribution import _gherkin_feature_of

    garm = ((baseline.get("arms") or {}).get("gherkin") or {})
    keys = set(garm.get("feature_to_lines") or {})
    for ident in (garm.get("test_to_lines") or {}):
        k = _gherkin_feature_of(str(ident))
        if k:
            keys.add(k)
    return keys


def _path_lifecycle(p: Path, rel: str, raw: str, known: bool,
                    changed: str, new: str, removed: str) -> "tuple[str, str]":
    """盘上存在性与已知集合成对的通用生命周期分类（feature/test 车道
    共用）：盘上+已知=changed、盘上+未知=new、已删+已知=removed、
    其余=unknown。"""
    if p.is_file():
        return (changed if known else new), rel
    if known:
        return removed, rel
    return "unknown", raw


def _classify_one(root: Path, raw: str, edge_files: set[str],
                  known_fids: set[str],
                  known_test_names: set[str],
                  feature_keys: "set[str] | None" = None,
                  content_fingerprints: "set[str] | None" = None
                  ) -> tuple[str, str]:
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        rel = str(p.relative_to(root))
    except ValueError:
        return "unknown", raw
    # SEE-1631：gherkin feature 单根（addons/gqt_gherkin/features/*.feature）
    if rel.startswith(_FEATURES_REL_PREFIX) and p.suffix == ".feature":
        from ..gherkin.addon import normalize_feature_key

        key = normalize_feature_key(p.stem)
        known = key in (feature_keys or set())
        return _path_lifecycle(p, rel, raw, known,
                               "feature_changed", "feature_new",
                               "feature_removed")
    name = p.name
    if rel.startswith("tests/") and name.startswith("test_"):
        known = name in known_test_names
        return _path_lifecycle(p, rel, raw, known,
                               "test_changed", "test_new", "test_removed")
    # C3-R9 退回1 + Owner 裁定：非脚本生产内容（因素表第 1 行"与脚本
    # 同一套逻辑"）——在盘且在指纹：哈希变 = content_changed（增量重跑
    # 静态引用触达面）、不在指纹 = content_new（第 2 行具名全量回退）；
    # 已删 = content_removed（第 3 行：剔除指纹与死边，不重跑）。
    # 生产 .gd 删除不在此列（上方 prod 分支 = prod_removed，触达面重跑）
    if p.suffix != ".gd" and rel in (content_fingerprints or {}):
        if not p.is_file():
            return "content_removed", rel
        import hashlib as _hl

        have = _hl.sha256(p.read_bytes()).hexdigest()
        return ("content_changed"
                if have != content_fingerprints[rel] else "content_new"), rel
    prod = _classify_prod_entry(p, rel, edge_files, known_fids)
    if prod is not None:
        return prod
    return "unknown", raw

def _classify_prod_entry(p: Path, rel: str, edge_files: set[str],
                         known_fids: set[str]) -> "tuple[str, str] | None":
    """生产文件分类（SEE-1610 §SPEC-N1）：盘上+基线边=changed、盘上无
    边=new、已删+有边=prod_removed；非 .gd 或无边的删除文件返回 None
    （由调用方归 unknown 具名 fail-loud）。"""
    if p.suffix != ".gd":
        return None
    has_edges = rel in edge_files or any(
        _fid_file_of(fid) == rel for fid in known_fids)
    if p.is_file():
        return ("prod_changed" if has_edges else "prod_new"), rel
    if has_edges:
        return "prod_removed", rel
    return None

def _static_candidates(root: Path, rel: str,
                       index: dict | None) -> set[str]:
    from ..mutation.gdself import build_index, query_consumers
    idx = index if index is not None else build_index(str(root))
    out = query_consumers(str(root), "res://" + rel,
                          Path(root) / "tests", index=idx)
    names = set()
    for c in out["consumers"]:
        if not c["res"].startswith("res://tests/") or c.get("depth") != 1:
            continue
        names.add(c["res"].rsplit("/", 1)[-1])
    return names

def _line_fact_touchers(arm: dict, rel: str) -> set[str]:
    """行事实反查：触达 rel 的脚本 basename 集（test 车道身份 ∪ script
    车道）。SEE-1610 §SPEC-N1 prod_removed 失效面的主来源——文件已删，
    fn/file 视图不可信，行事实是唯一反查面。"""
    touch: set[str] = set()
    for identity, by_rel in (arm.get("test_to_lines") or {}).items():
        if rel in by_rel:
            touch.add(_script_name_of(identity))
    for script, by_rel in (arm.get("script_to_lines") or {}).items():
        if rel in by_rel:
            touch.add(Path(script).name)
    return touch


def compute_invalidation(root: Path, baseline: dict,
                         classes: dict[str, list[str]],
                         index: dict | None) -> dict:
    """失效面 → {rerun, dropped, feature_rerun, sources}（sources 记录每个
    候选的来源）。SEE-1631：feature_rerun = gherkin 臂失效面（feature
    变更键 ∪ 行事实触达生产变更的 feature 键）——非空即触发具名全脸
    回退（gherkin 行事实重绑，不交付半成品）。"""
    arm = baseline["arms"]["gut_full"]
    rerun: set[str] = set()
    dropped: set[str] = set()
    sources: dict[str, list[str]] = {}
    fid_to_tests: dict[str, set[str]] = {}
    for identity, fids in (arm.get("test_to_functions") or {}).items():
        for fid in fids:
            fid_to_tests.setdefault(_fid_file_of(fid), set()).add(
                _script_name_of(identity))
    explicit_rerun: set[str] = set()
    for rel in classes["test_changed"] + classes["test_new"]:
        name = Path(rel).name
        rerun.add(name)
        explicit_rerun.add(name)
        sources.setdefault(name, []).append(f"test_changed:{rel}")
    for rel in classes["test_removed"]:
        # C3-R24（N1/决策 16）：删除面必须是完整路径身份——收 basename
        # 会在同名碰撞下把幸存脚本的边与判定一并清掉
        dropped.add(rel)
        sources.setdefault(Path(rel).name, []).append(f"test_removed:{rel}")
    for rel in classes["prod_changed"]:
        touch = set(fid_to_tests.get(rel, set()))
        for name in touch:
            sources.setdefault(name, []).append(f"fn_edges:{rel}")
        rerun |= touch
        static = _static_candidates(root, rel, index)
        for name in static:
            sources.setdefault(name, []).append(f"static:{rel}")
        rerun |= static
    for rel in classes.get("prod_removed", []):
        # SEE-1610 §SPEC-N1：删除文件不可信 fn/file 视图，失效面走行事实
        # 反查（test 车道 ∪ script 车道）∪ 静态直连消费者。
        touch = _line_fact_touchers(arm, rel)
        for name in touch:
            sources.setdefault(name, []).append(f"prod_removed_lines:{rel}")
        rerun |= touch
        static = _static_candidates(root, rel, index)
        for name in static:
            sources.setdefault(name, []).append(f"static:{rel}")
        rerun |= static
    # C3-R9 Owner 裁定（因素表第 1 行）：非脚本内容**修改**与脚本同一套
    # 逻辑——重跑面与 gherkin 触达 feature 见 _content_changed_face。
    content_feature_keys: set[str] = set()
    for rel in classes.get("content_changed", []):
        rerun |= _content_changed_rerun(root, rel, arm, index, sources)
        content_feature_keys |= _content_feature_keys(root, baseline, rel,
                                                      index)
    feature_rerun = _gherkin_invalidation_surface(baseline, classes)
    # C3-R9：非脚本内容修改的生产消费者 .gd 触达的 feature 键并入
    feature_rerun |= content_feature_keys
    return {"rerun": rerun, "dropped": dropped, "sources": sources,
            "explicit_rerun": explicit_rerun, "feature_rerun": feature_rerun}


def _content_changed_rerun(root: Path, rel: str, arm: dict,
                           index: dict | None,
                           sources: dict) -> "set[str]":
    """C3-R9 Owner 裁定（因素表第 1 行）：非脚本内容**修改**与脚本同一套
    逻辑——重跑面 = 静态直连消费者测试 ∪ 消费该内容的生产 .gd 的触达
    测试。无引用者 → 具名警告、零重跑。"""
    rerun: set[str] = set()
    static = _static_candidates(root, rel, index)
    for name in static:
        sources.setdefault(name, []).append(f"content_static:{rel}")
    rerun |= static
    from ..mutation.gdself import query_consumers as _qc

    out_q = _qc(str(root), "res://" + rel, Path(root), index=index)
    prod_gds = {c["res"].removeprefix("res://")
                for c in out_q.get("consumers") or []
                if str(c.get("res", "")).endswith(".gd")
                and not str(c["res"]).startswith("res://tests/")}
    for gd in prod_gds:
        # C3-R26（D1/因素表第 1 行）：触达反查按**行事实双车道**（与
        # prod_removed 的 _line_fact_touchers 同源：test 车道 + script
        # 车道，basename 归一）——真实建图里 script 车道通常为空
        # （KOL 真图 script_to_lines 0 : test_to_lines 208），单查
        # file_to_scripts 会漏掉主流形态（test 体内消费）的触达测试。
        touch = _line_fact_touchers(arm, gd)
        for name in touch:
            sources.setdefault(name, []).append(f"content_prod:{gd}")
        rerun |= touch
    return rerun


def _content_feature_keys(root: Path, baseline: dict, rel: str,
                          index: dict | None) -> "set[str]":
    """content_changed 的 gherkin 触达 feature 键 = 消费该内容的生产 .gd
    行事实所在的 feature 键。"""
    from ..mutation.gdself import query_consumers as _qc

    out_q = _qc(str(root), "res://" + rel, Path(root), index=index)
    prod_gds = {c["res"].removeprefix("res://")
                for c in out_q.get("consumers") or []
                if str(c.get("res", "")).endswith(".gd")
                and not str(c["res"]).startswith("res://tests/")}
    garm = (baseline.get("arms") or {}).get("gherkin") or {}
    out: set[str] = set()
    for key, by_rel in (garm.get("feature_to_lines") or {}).items():
        if prod_gds & set(by_rel or {}):
            out.add(key)
    return out


def _gherkin_toucher_feature_keys(garm: dict,
                                  changed_rels: set[str]) -> set[str]:
    """基线 gherkin 臂中行事实触达 changed_rels 的 feature 键集。

    feature 视图在场 → 按视图逐 key 判定；视图缺席（旧产物兜底）→
    按行事实身份前缀反推。"""
    out: set[str] = set()
    f2l = garm.get("feature_to_lines") or {}
    for key, by_rel in f2l.items():
        if changed_rels & set(by_rel or {}):
            out.add(key)
    if not f2l:
        from .attribution import _gherkin_feature_of

        for ident, by_rel in (garm.get("test_to_lines") or {}).items():
            if changed_rels & set(by_rel or {}):
                k = _gherkin_feature_of(str(ident))
                if k:
                    out.add(k)
    return out


def _gherkin_invalidation_surface(baseline: dict,
                                  classes: dict[str, list[str]]
                                  ) -> set[str]:
    """SEE-1631 §SPEC-016（Owner 裁决：增量对齐 gut_full）：gherkin 臂
    重跑 feature 键集 = feature 变更键 ∪ 新增 feature 键 ∪ 基线 gherkin
    行事实触达 prod_changed/prod_removed 的 feature 键。非空 → 只重跑
    这些 feature（其余原样继承）；超比例上限才整臂具名回退。"""
    from ..gherkin.addon import normalize_feature_key

    out: set[str] = set()
    for rel in classes.get("feature_changed", []):
        out.add(normalize_feature_key(Path(rel).stem))
    for rel in classes.get("feature_new", []):
        out.add(normalize_feature_key(Path(rel).stem))
    garm = (baseline.get("arms") or {}).get("gherkin")
    if not isinstance(garm, dict) or not garm:
        return out
    changed_rels = set(classes.get("prod_changed") or []) | set(
        classes.get("prod_removed") or [])
    out |= _gherkin_toucher_feature_keys(garm, changed_rels)
    return out


def _baseline_gherkin_dropped(baseline: dict,
                              classes: dict[str, list[str]]) -> set[str]:
    """SEE-1631 §SPEC-016：删除 feature 键集（边剔除、无重跑）。"""
    from ..gherkin.addon import normalize_feature_key

    out: set[str] = set()
    for rel in classes.get("feature_removed", []):
        out.add(normalize_feature_key(Path(rel).stem))
    return out

def _gherkin_facts_by_identity(facts: "dict | None",
                               skip_feature_keys: set[str]) -> dict:
    """按 feature key 过滤拷贝行事实（身份 → {rel: [lines] } 浅拷贝，
    行列表独立复制；skip 命中身份所属 feature 的整身份剔除）。"""
    from .attribution import _gherkin_feature_of

    out: dict = {}
    for i, by_rel in (facts or {}).items():
        if _gherkin_feature_of(str(i)) in skip_feature_keys:
            continue
        out[i] = {r: list(ls) for r, ls in by_rel.items()}
    return out


def _merged_line_facts(sources: "list[dict]",
                       removed_rels: set[str]) -> "dict[str, dict[str, set]]":
    """多源行事实同键合并（先到先建槽位，removed_rels 的边剔除）。"""
    merged: dict[str, dict[str, set]] = {}
    for src in sources:
        for i, by_rel in src.items():
            slot = merged.setdefault(i, {})
            for r, ls in by_rel.items():
                if r in removed_rels:
                    continue
                slot.setdefault(r, set()).update(ls)
    return merged


def _gherkin_scenarios_of_kept(scenarios: "dict | None",
                               skip_feature_keys: set[str]) -> dict:
    """按 feature key 过滤 scenario 视图（skip 命中身份所属 feature 的
    整条剔除；值原样保留引用——与全量产物同口径）。"""
    from .attribution import _gherkin_feature_of

    return {i: v for i, v in (scenarios or {}).items()
            if _gherkin_feature_of(str(i)) not in skip_feature_keys}


def _merge_gherkin_arm(baseline_gherkin: "dict | None",
                       fresh: "dict | None",
                       rerun_keys: set[str],
                       dropped_keys: set[str],
                       removed_rels: set[str]) -> "dict | None":
    """SEE-1631 §SPEC-016：gherkin 臂增量合并（与 gut_full 同口径）。

    重跑 feature 的身份整体替换为 fresh；删除 feature 与被删除生产文件
    触达的边剔除；其余 feature 原样继承（行事实逐位不变）。行事实是主
    真值——feature/scenario 视图与计数从合并后行事实单源重算。基线无
    gherkin 臂 → fresh 原样（无重跑键时 None）。"""
    keep_skip = rerun_keys | dropped_keys
    base_facts = _gherkin_facts_by_identity(
        (baseline_gherkin or {}).get("test_to_lines"), keep_skip)
    base_script_facts = _gherkin_facts_by_identity(
        (baseline_gherkin or {}).get("script_to_lines"), keep_skip)
    fresh_facts = _gherkin_facts_by_identity(
        (fresh or {}).get("test_to_lines"), set())
    fresh_script_facts = _gherkin_facts_by_identity(
        (fresh or {}).get("script_to_lines"), set())
    test_to_lines = _merged_line_facts([base_facts, fresh_facts],
                                       removed_rels)
    script_to_lines = _merged_line_facts([base_script_facts,
                                          fresh_script_facts], removed_rels)
    if not test_to_lines and not script_to_lines:
        return None
    from .attribution import (
        _derive_views_from_line_facts,
        _line_pair_counts,
    )

    derived = _derive_views_from_line_facts(test_to_lines, script_to_lines,
                                            None)
    scenarios = _gherkin_scenarios_of_kept(
        (baseline_gherkin or {}).get("gherkin_scenarios"), keep_skip)
    scenarios.update({i: v for i, v in ((fresh or {}).get(
        "gherkin_scenarios") or {}).items()})
    line_pairs, script_line_pairs = _line_pair_counts(test_to_lines,
                                                      script_to_lines)
    unmapped = int(((fresh if fresh is not None else baseline_gherkin)
                    or {}).get("gherkin_unmapped_identities") or 0)
    return _gherkin_arm_out(derived, test_to_lines, script_to_lines,
                            scenarios, unmapped, line_pairs,
                            script_line_pairs)


def _gherkin_arm_out(derived: dict, test_to_lines: dict, script_to_lines: dict,
                     scenarios: dict, unmapped: int, line_pairs: int,
                     script_line_pairs: int) -> dict:
    """合并后派生数据 → gherkin 臂 payload（与全量相同的序列化出口：
    derived 内是 set 形态，直接落盘会 JSON 爆炸——SEE-1587 口径：视图
    永不裸奔）。"""
    from .attribution import (
        _attribution_view_block,
        _line_facts_views,
    )

    return {
        **_attribution_view_block(derived),
        **_line_facts_views(test_to_lines, script_to_lines,
                            derived["line_to_tests"]),
        "feature_to_lines": _gherkin_views_from_lines(test_to_lines),
        "line_to_features": _line_to_features_view(test_to_lines),
        "gherkin_scenarios": scenarios,
        "gherkin_unmapped_identities": unmapped,
        "tests": len(derived["test_to_files"]),
        "scripts": len(derived["script_to_files"]),
        "files": len(derived["file_to_tests"]),
        "pairs": sum(len(v) for v in derived["test_to_files"].values()),
        "script_pairs": sum(len(v) for v in derived["script_to_files"]
                            .values()),
        "function_pairs": sum(len(v) for v in derived["test_to_functions"]
                              .values()),
        "line_pairs": line_pairs,
        "script_line_pairs": script_line_pairs,
    }


def _gherkin_views_from_lines(test_to_lines: dict) -> dict:
    """合并后行事实 → feature_to_lines（单源重算，与全量口径一致）。"""
    from .attribution import _gherkin_views

    return _gherkin_views(test_to_lines)["feature_to_lines"]


def _line_to_features_view(test_to_lines: dict) -> dict:
    from .attribution import _gherkin_views

    return _gherkin_views(test_to_lines)["line_to_features"]


def _identity_script_segment(identity) -> str:
    """身份 → 脚本段（test 身份取 `::` 前段；script 车道键无 `::` 时取
    整键）。"""
    return str(identity).partition("::")[0]


def _legacy_family_state(dropped_rels: "set[str] | None",
                         tests_face: "dict | None") -> dict:
    """C3-R24（N1/§SPEC-038⑤）：删除面的升级过渡家族态——basename →
    (族是否全删, 幸存 rel 列表)。旧 basename 形态身份无法按路径定向：
    唯一幸存者可改写规范形态，多幸存者无法消歧只能披露，整族全删才可
    剔除。`tests_face` 兼容基线面与删除后合并面两种口径（幸存者按
    `rel not in dropped_rels` 判定，两口径同结果）。"""
    out: dict[str, tuple] = {}
    for rel in dropped_rels or ():
        base = Path(rel).name
        if base in out:
            continue
        survivors = [r for r in (tests_face or {})
                     if Path(r).name == base and r not in dropped_rels]
        out[base] = (not survivors, survivors)
    return out


def _merged_identities(key: str, arm: dict, fresh: dict, dropped: set[str],
                       replace_segments: set[str], replace_bases: set[str],
                       legacy_dead_bases: "set[str] | None") -> dict:
    """同语义合并：dropped 删边（完整路径身份精确匹配；升级过渡旧
    basename 键仅整族已删才剔除）+ replace 整体替换（规范键按完整路径、
    旧键按 basename——D4 升级过渡语义不回退）+ 其余身份原样保留 +
    fresh 注入。C3-R24（N1/决策 16）：basename 只是「选哪些脚本来跑」
    的入口，不得反向放大到边面。"""
    base = {}
    for identity, value in (arm.get(key) or {}).items():
        sc = _identity_script_segment(identity)
        if sc.startswith("res://"):
            if sc[len("res://"):] in dropped or sc in replace_segments:
                continue
        elif Path(sc).name in replace_bases \
                or (legacy_dead_bases
                    and Path(sc).name in legacy_dead_bases):
            continue
        base[identity] = value
    for identity, value in (fresh.get(key) or {}).items():
        base[identity] = value
    return base


def _canonicalize_deleted_families(merged: dict, families: dict,
                                   collisions: "list[dict] | None") -> None:
    """C3-R24（N1/§SPEC-038⑤）：删除家族的旧 basename 条目处置——唯一
    幸存者改写规范形态（幸存者判定保留、产物单一形态）；多幸存者无法
    消歧原样保留 + 具名披露；整族已删兜底剔除（_verdict_drop_removed
    已删时此处为空转）。"""
    for ident in list(merged):
        sc, _, nm = str(ident).partition("::")
        if not nm or sc.startswith("res://") \
                or sc.startswith("gherkin://"):
            continue
        fam = families.get(Path(sc).name)
        if fam is None:
            continue
        _dead, survivors = fam
        full_survivors = [f"res://{r}" for r in survivors]
        if len(full_survivors) == 1:
            canon = f"{full_survivors[0]}::{nm}"
            if canon not in merged and ident in merged:
                merged[canon] = merged.pop(ident)
                if collisions is not None:
                    collisions.append({
                        "basename_key": ident,
                        "candidates": sorted({ident, canon}),
                        "resolved": canon,
                        "reason": "legacy_basename_identity_"
                                  "canonicalized_after_delete",
                    })
        elif len(full_survivors) > 1:
            if collisions is not None:
                collisions.append({
                    "basename_key": ident,
                    "candidates": sorted(full_survivors),
                    "resolved": "",
                    "reason": "legacy_basename_identity_ambiguous_after_delete",
                })
        elif ident in merged:
            merged.pop(ident, None)
            if collisions is not None:
                collisions.append({
                    "basename_key": ident,
                    "candidates": sorted({ident}),
                    "resolved": "",
                    "reason": "legacy_basename_identity_dropped_with_family",
                })


def _disclose_unresolvable_legacy(merged: dict, tests_by_base: dict,
                                  collisions: "list[dict] | None") -> None:
    """tests_face/宇宙都恢复不出规范路径的旧 basename 条目原样保留并具名
    披露（不静默丢证据，C3-R22 D4/QA-R2）。C3-R25（N8）：gherkin://
    身份不属于旧 basename 形态，不得进披露。"""
    for ident in list(merged):
        sc, _, nm = str(ident).partition("::")
        if not nm or sc.startswith("res://") \
                or sc.startswith("gherkin://"):
            continue
        if not (tests_by_base.get(Path(sc).name) or []):
            if collisions is not None:
                collisions.append({
                    "basename_key": ident,
                    "candidates": [],
                    "resolved": "",
                    "reason": "legacy_basename_identity_kept_no_canonical_path",
                })


def _canonical_verdict_dedup(merged: dict, tests_face: "dict[str, str] | None",
                             arm_identities: "list[str] | None",
                             collisions: "list[dict] | None",
                             legacy_families: "dict | None" = None) -> None:
    """C3-R22（D4/QA-R2）：按规范身份（完整 res:// 路径）去重——升级
    过渡（旧 basename 形态基线 × 新 res:// 形态钩子）的增量合并会把同
    一物理测试的两种身份键并存。同 basename::name 的旧 basename 条目
    在规范条目在场时剔除（守恒：重跑重算应替换）；tests_face/宇宙都
    恢复不出规范路径的旧条目原样保留并具名披露（不静默丢证据）。
    两目录同名测试的规范身份互异，不受影响（D2 不回退）。
    C3-R24（N1/§SPEC-038⑤）：删除家族——唯一幸存者的旧 basename 条目
    改写规范形态（幸存者判定保留、产物单一形态）；多幸存者无法消歧
    原样保留 + 具名披露；整族已删兜底剔除。"""
    tests_by_base, _idents_by_base = _verdict_identity_index(tests_face,
                                                             arm_identities)
    replaced: list[tuple[str, str]] = []
    for ident in list(merged):
        sc, _, nm = str(ident).partition("::")
        if not nm or sc.startswith("res://") \
                or sc.startswith("gherkin://"):
            continue  # 已是规范形态 / gherkin 身份
        base = Path(sc).name
        twin = next((f"{full}::{nm}" for full in tests_by_base.get(base, [])
                     if f"{full}::{nm}" in merged and full != sc), None)
        if twin is not None:
            merged.pop(ident, None)
            replaced.append((ident, twin))
    if collisions is not None:
        for legacy, twin in replaced:
            collisions.append({
                "basename_key": legacy,
                "candidates": sorted({legacy, twin}),
                "resolved": twin,
                "reason": "legacy_basename_identity_replaced_by_canonical",
            })
        _disclose_unresolvable_legacy(merged, tests_by_base, collisions)
    _canonicalize_deleted_families(merged,
                                   legacy_families
                                   if legacy_families is not None else {},
                                   collisions)

def _purge_removed_rels(test_to_lines: dict, script_to_lines: dict,
                        removed_rels: set[str]) -> tuple[dict, dict]:
    """SEE-1610 §SPEC-N1：从合并后行事实剔除已删除生产文件的全部边
    （触达身份已整体替换，purge 是死边兜底）。"""
    def strip(by_ident: dict) -> dict:
        return {i: {r: ls for r, ls in by_rel.items()
                    if r not in removed_rels}
                for i, by_rel in by_ident.items()}
    return strip(test_to_lines), strip(script_to_lines)

def _replace_surface_and_dead(fresh: dict,
                              legacy_families: "dict | None"
                              ) -> "tuple[set[str], set[str], set[str] | None]":
    """替换面与升级过渡死族（C3-R24/N1/决策 16）：规范 res:// 键按完整
    路径替换；旧 basename 键沿用 basename 口径——D4 升级过渡语义不回退；
    整族已删的 basename 集来自删除家族态（basename 只是「选哪些脚本」
    入口，不得反向放大到边面）。"""
    replace_segments = {_identity_script_segment(i) for i in
                        (fresh.get("test_to_files") or {})}
    replace_segments |= {_identity_script_segment(k) for k in
                         (fresh.get("script_to_files") or {})}
    replace_bases = {Path(s).name for s in replace_segments}
    dead_bases = {base for base, (dead, _surv)
                  in (legacy_families or {}).items() if dead} or None
    return replace_segments, replace_bases, dead_bases


def _merge_arm(arm: dict, fresh: dict, dropped: set[str],
               fid_by_line_by_rel: "dict[str, dict[int, str]] | None" = None,
               removed_rels: "set[str] | None" = None,
               legacy_families: "dict | None" = None) -> dict:
    """按身份替换/删边合并一张臂级映射；行事实同语义合并（重跑脚本
    整体替换、删除脚本删边、其余身份原样保留），file/function 视图与
    line→tests 反查从合并后行事实单源重算（SEE-1587：视图永不独立拼接）。
    `removed_rels`（SEE-1610 §SPEC-N1）＝已删除生产文件：从合并后行事实
    purge 该文件的全部边（触达身份已整体替换，purge 是死边兜底）。
    `legacy_families`（C3-R24/§SPEC-038⑤）＝删除面升级过渡家族态。
    结构性统计精确重算。"""
    replace_segments, replace_bases, dead_bases = \
        _replace_surface_and_dead(fresh, legacy_families)
    test_to_lines = _merged_identities(
        "test_to_lines", arm, fresh, dropped, replace_segments,
        replace_bases, dead_bases)
    script_to_lines = _merged_identities(
        "script_to_lines", arm, fresh, dropped, replace_segments,
        replace_bases, dead_bases)
    if removed_rels:
        test_to_lines, script_to_lines = _purge_removed_rels(
            test_to_lines, script_to_lines, removed_rels)
    # file/function 边不再独立合并：从合并后行事实派生（行事实是主真值）。
    derived = _derive_views_from_line_facts(test_to_lines, script_to_lines,
                                            fid_by_line_by_rel)
    test_to_files = derived["test_to_files"]
    script_to_files = derived["script_to_files"]
    test_to_functions = derived["test_to_functions"]
    script_to_functions = derived["script_to_functions"]
    file_to_tests = derived["file_to_tests"]
    file_to_scripts = derived["file_to_scripts"]
    line_to_tests = derived["line_to_tests"]
    line_pairs, script_line_pairs = _line_pair_counts(test_to_lines,
                                                      script_to_lines)
    return {
        "tests": len(test_to_files),
        "scripts": len(script_to_files),
        "files": len(file_to_tests),
        "pairs": sum(len(v) for v in test_to_files.values()),
        "script_pairs": sum(len(v) for v in script_to_files.values()),
        "function_pairs": sum(len(v) for v in test_to_functions.values()),
        "line_pairs": line_pairs,
        "script_line_pairs": script_line_pairs,
        "test_to_files": {k: sorted(v)
                          for k, v in sorted(test_to_files.items())},
        "test_to_functions": {k: sorted(v)
                              for k, v in sorted(test_to_functions.items())},
        "script_to_files": {k: sorted(v)
                            for k, v in sorted(script_to_files.items())},
        "script_to_functions": {k: sorted(v)
                                for k, v in sorted(script_to_functions.items())},
        "file_to_tests": {k: sorted(v)
                          for k, v in sorted(file_to_tests.items())},
        "file_to_scripts": {k: sorted(v)
                            for k, v in sorted(file_to_scripts.items())},
        **_line_facts_views(test_to_lines, script_to_lines, line_to_tests),
    }
def _increment_decision(root: Path, recipe: dict, baseline: dict,
                        changed_files: list[str], since_full: int,
                        calibration_every: int,
                        rerun_full_face_ratio: float,
                        gherkin_rerun_full_face_ratio: float =
                        DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO) -> tuple:
    """增量资格判定：hash 守恒 + 变更分类 + 失效计算 + fallback 仲裁。
    `recipe` 是已注入 toolchain 的配方（hash 判据用）。
    返回 (classes, invalidation, rerun, reason)。"""
    classes = classify_changes(root, changed_files, baseline)
    if classes["unknown"]:
        # SEE-1610 §SPEC-N1：对齐模块 docstring——不可分类路径具名拒绝，
        # 绝不披露后照跑（旧图死边 + 零重跑是静默失真）。
        unk = classes["unknown"]
        sample = ", ".join(sorted(unk)[:8])
        more = f" (+{len(unk) - 8} more)" if len(unk) > 8 else ""
        raise IncrementError(
            f"unknown_changed_paths: {len(unk)} changed path(s) cannot be "
            f"classified as test/prod change (non-.gd or no baseline edges) "
            f"— failing loud per module contract: {sample}{more}")
    invalidation = {"rerun": set(), "dropped": set(), "sources": {}}
    hash_ok = (baseline.get("report_recipe_hash")
               == recipe["recipe_hash"])
    if hash_ok and not classes["prod_new"]:
        from ..mutation.gdself import build_index
        invalidation = compute_invalidation(
            root, baseline, classes, build_index(str(root)))
    known = _baseline_script_names(baseline)
    # 分类来源（改/增测试脚本）不过 known 闸——新脚本天然不在基线；
    # 静态候选（可能指向基线外的脚本）必须过滤，防止凭空圈定
    explicit = set(invalidation.get("explicit_rerun") or ())
    rerun = explicit | {n for n in invalidation["rerun"] if n in known}
    ratio = (len(rerun & known) / len(known)) if known else 0.0
    gherkin_known = len(_baseline_gherkin_feature_keys(baseline))
    reason = _fallback_reason(hash_ok, classes, since_full,
                              calibration_every, ratio,
                              rerun_full_face_ratio,
                              feature_rerun=invalidation.get(
                                  "feature_rerun") or set(),
                              gherkin_known=gherkin_known,
                              gherkin_ratio_cap=gherkin_rerun_full_face_ratio)
    return classes, invalidation, rerun, reason

def _increment_emit(root: Path, out_path, artifact: dict, merged: dict,
                    classes: dict, rerun: set, invalidation: dict,
                    arm_exec_s, t0: float) -> dict:
    """产物落盘（父目录自建）+ 增量报告组装（provenance/merged_stats）。"""
    out_p = Path(out_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(artifact, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return {
        "mode": "incremental",
        "artifact": str(out_p),
        "changed": classes,
        "rerun_scripts": sorted(rerun),
        "dropped": sorted(invalidation["dropped"]),
        "rerun_sources": {k: v for k, v in
                          invalidation["sources"].items()
                          if k in set(rerun)},
        "merged_stats": {k: merged[k] for k in
                         ("tests", "scripts", "files", "pairs",
                          "function_pairs")},
        "event_counts_stale": True,
        "wall_s": round(time.monotonic() - t0, 3),
        "arm_exec_s": arm_exec_s,
    }


def _deleted_family_state(baseline: dict, dropped_rels: "set[str]") -> dict:
    """删除面升级过渡家族态（C3-R24/N1）：从**基线**测试面现算（基线面
    含被删 rel，幸存者判定不受合并面新增同名脚本干扰）。"""
    return _legacy_family_state(dropped_rels,
                                baseline.get("tests_face") or {})


def run_attribution_increment(project_root: "str | Path",
                              target_files: list[str], recipe: dict,
                              baseline_path: str | Path,
                              changed_files: list[str],
                              out_path: "str | Path",
                              *, timeout_s: int = 180,
                              attribution_causal: bool = False,
                              calibration_every: int = 5,
                              rerun_full_face_ratio: float
                              = DEFAULT_RERUN_FULL_FACE_RATIO,
                              gherkin_rerun_full_face_ratio: float
                              = DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO
                              ) -> dict:
    """C3-R14（§SPEC-032③）插桩互斥外壳：增量腿同样把插桩文本写进源
    树，与测量臂共用 `<root>/.qa-cache/probe-rw.lock` 串行化。"""
    from .backup import probe_rw_lock

    with probe_rw_lock(project_root):
        return _run_attribution_increment_locked(
            project_root, target_files, recipe, baseline_path,
            changed_files, out_path, timeout_s=timeout_s,
            attribution_causal=attribution_causal,
            calibration_every=calibration_every,
            rerun_full_face_ratio=rerun_full_face_ratio,
            gherkin_rerun_full_face_ratio=gherkin_rerun_full_face_ratio)


def _load_baseline_with_conservation(baseline_path: str | Path) -> dict:
    """C3-R14（§SPEC-025）表守恒入口检查：基线缺 `test_verdicts` 键 →
    增量会补出空表冒充守恒（MapView 的"表在场"检查反而通过）。
    具名拒绝，走全量。"""
    baseline = load_baseline(baseline_path)
    if "test_verdicts" not in (baseline or {}):
        raise IncrementError(
            "incremental rebuild refused: baseline map is missing "
            "`test_verdicts` (§SPEC-025 table conservation) — rebuild "
            "the attribution map from scratch")
    return baseline


def _run_attribution_increment_locked(
        project_root: str | Path, target_files: list[str],
        recipe: dict, baseline_path: str | Path,
        changed_files: list[str], out_path: str | Path,
        *, timeout_s: int = 180,
        attribution_causal: bool = False,
        calibration_every: int = 5,
        rerun_full_face_ratio: float
        = DEFAULT_RERUN_FULL_FACE_RATIO,
        gherkin_rerun_full_face_ratio: float
        = DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO
        ) -> dict:
    """增量重建主入口（含产物落盘与 provenance）。C3-R14：插桩互斥
    外壳 + §SPEC-025 表守恒入口检查。"""
    root = Path(project_root).resolve()
    baseline = _load_baseline_with_conservation(baseline_path)
    t0 = time.monotonic()
    # D1：先存 raw recipe（toolchain 注入前）。回退全脸重建必须用它——
    # run_measurement 自行注入并据此计算 pristine hash；若传已注入配方，
    # pristine 会被写成注入域 hash → 映射永久"不新鲜"自锁。
    raw_recipe = dict(recipe)
    recipe = _inject_toolchain(root, dict(recipe))
    prev = baseline.get("increment") or {}
    since_full = int(prev.get("increments_since_full") or 0)
    classes, invalidation, rerun, reason = _increment_decision(
        root, recipe, baseline, changed_files, since_full, calibration_every,
        rerun_full_face_ratio, gherkin_rerun_full_face_ratio)
    if reason != "incremental":
        # 全脸回退必须传 **raw recipe**（run_measurement 自行注入并计算
        # pristine hash；传已注入配方会把 pristine 写成注入 hash → 映射
        # 永久"不新鲜"自锁）
        return _fallback_full_face(root, target_files, raw_recipe, out_path,
                                   classes, reason, attribution_causal,
                                   timeout_s, t0)
    removed = set(classes.get("prod_removed") or [])
    # 删除文件不进插桩臂（无源码可模板化）；其边由合并侧 purge。
    arm_targets = [t for t in target_files if Path(t).is_file()]
    arm_exec_s, fresh, executions, fid_by_line_by_rel = _run_increment_arm(
        root, arm_targets, recipe, rerun, timeout_s, attribution_causal)
    # C3-R24（N1/§SPEC-038⑤）：升级过渡家族态从**基线**测试面现算
    # （基线面含被删 rel，幸存者判定不受合并面新增同名脚本干扰）。
    legacy_families = _deleted_family_state(baseline,
                                            set(invalidation["dropped"]))
    merged = _merge_arm(baseline["arms"]["gut_full"], fresh,
                        invalidation["dropped"], fid_by_line_by_rel,
                        removed_rels=removed,
                        legacy_families=legacy_families)
    # SEE-1631 §SPEC-016：gherkin 臂增量腿——只重跑失效 feature（与
    # gut_full 同粒度），其余 feature 原样继承；templates 为 attr 模板
    # （与 gut 重跑同一插桩文本单源）。
    gherkin_rerun = set(invalidation.get("feature_rerun") or [])
    gherkin_dropped = _baseline_gherkin_dropped(baseline, classes)
    templates = prepare_attr_templates(root, arm_targets, recipe,
                                       attribution_causal)
    gh_exec_s = 0.0
    gh_executions: list[dict] = []
    fresh_gherkin = None
    if gherkin_rerun:
        from .increment_runner import run_gherkin_increment_leg
        from ..coverage.evidence import make_run_uuid

        t_gh = time.monotonic()
        fresh_gherkin, gh_executions = run_gherkin_increment_leg(
            root, sorted(gherkin_rerun), templates, recipe,
            attribution_causal, make_run_uuid(), timeout_s)
        gh_exec_s = round(time.monotonic() - t_gh, 3)
    baseline_gherkin = (baseline.get("arms") or {}).get("gherkin")
    if isinstance(baseline_gherkin, dict) and baseline_gherkin or \
            fresh_gherkin:
        merged_gherkin = _merge_gherkin_arm(
            baseline_gherkin, fresh_gherkin,
            rerun_keys=gherkin_rerun, dropped_keys=gherkin_dropped,
            removed_rels=removed)
    else:
        merged_gherkin = None
    invalidation = {**invalidation,
                    "gherkin_rerun": sorted(gherkin_rerun),
                    "gherkin_dropped": sorted(gherkin_dropped)}
    # SEE-1631 §SPEC-025：verdict 表守恒——重跑身份按新执行结果重算
    # （脚本级执行账 out 解析失败名），未重跑身份原样继承，删除剔除。
    # C3-R6：新测试身份补表需要**合并后**的 tests_face（新文件的完整
    # 路径只在合并面里——基线面没有新文件）
    face_after = _merged_tests_face(root, baseline, classes)
    merge_collisions: list = []
    merged_verdicts = _merged_test_verdicts(
        baseline, executions or [], rerun, invalidation["dropped"], removed,
        _gherkin_leg_exec_source(fresh_gherkin, gh_executions),
        gherkin_rerun, gherkin_dropped,
        tests_face=face_after,
        arm_identities=sorted((merged.get("test_to_lines") or {}).keys()),
        collisions=merge_collisions,
        legacy_families=legacy_families)
    artifact = _build_increment_artifact(
        baseline, merged, recipe, root, classes,
        sorted(rerun), invalidation, since_full, arm_exec_s,
        executions, merged_gherkin=merged_gherkin,
        gh_exec_s=gh_exec_s, gh_executions=gh_executions,
        merged_verdicts=merged_verdicts, merge_collisions=merge_collisions)
    return _increment_emit(root, out_path, artifact, merged, classes, rerun,
                           invalidation, arm_exec_s, t0)
def _fallback_reason(hash_ok: bool, classes: dict, since_full: int,
                     calibration_every: int, ratio: float,
                     ratio_cap: float,
                     feature_rerun: "set | frozenset" = frozenset(),
                     gherkin_known: int = 0,
                     gherkin_ratio_cap: float =
                     DEFAULT_GHERKIN_RERUN_FULL_FACE_RATIO) -> str:
    """SEE-1631 §SPEC-016（Owner 裁决）：feature 失效面走增量重跑，不再
    一刀切整臂回退；仅当 gherkin 重跑面超自身比例上限（增量不比整臂便宜）
    时具名回退。既有回退条件（hash 失配/新增产品文件/校准到期/gut 比例
    上限）不变。"""
    if not hash_ok:
        return "hash_mismatch_fallback"
    if classes["prod_new"] or classes.get("content_new"):
        # 因素表第 2 行：生产内容新增（含非脚本内容）无基线边、不猜失效面
        # → 具名全量回退（C3-R9 Owner 裁定：修改走增量、新增仍全量）
        return "new_prod_file_fallback"
    if gherkin_known and len(feature_rerun) > gherkin_known * gherkin_ratio_cap:
        return "gherkin_rerun_surface_too_large_fallback"
    if since_full >= calibration_every:
        return "calibration_due_fallback"
    if ratio > ratio_cap:
        return "rerun_surface_too_large_fallback"
    return "incremental"

def _run_increment_arm(root: Path, target_files: list[str], recipe: dict,
                       rerun: set[str], timeout_s: int,
                       causal: bool) -> tuple[float, dict, list[dict],
                                              dict[str, dict[int, str]]]:
    """在插桩副本上只跑 rerun 脚本，读回归属证据并聚合成臂级映射。
    返回 (读回+聚合秒数, fresh 边/行事实集, 逐脚本执行结果, fid_by_line_by_rel)
    ——fid 行映射覆盖全部目标文件（模板面），供合并侧派生视图单源重算。"""
    templates = prepare_attr_templates(root, target_files, recipe, causal)
    gut_dirs = list(recipe["gut_face"].get("dirs") or [])
    run_uuid = make_run_uuid()
    results, xdg_homes = execute_rerun_scripts(
        root, gut_dirs, sorted(rerun), timeout_s, templates, causal,
        run_uuid)
    t0 = time.monotonic()
    attr_by_rel = {
        rel: _read_attr_merged(root, run_uuid, rel, xdg_homes)
        for rel in templates}
    registries = _read_registries(root, run_uuid, xdg_homes)
    fid_by_line_by_rel = {rel: _fid_by_line_from_registry(it["registry"])
                          for rel, it in templates.items()}
    arm_map = build_attribution_map(
        "increment", attr_by_rel, registries,
        probe_lines_by_rel={rel: it["probe_lines"]
                            for rel, it in templates.items()},
        block_map_by_rel={rel: it["block_map"]
                          for rel, it in templates.items()},
        fid_by_line_by_rel=fid_by_line_by_rel)
    for xdg in xdg_homes:
        shutil.rmtree(xdg, ignore_errors=True)
    return (round(time.monotonic() - t0, 3), _group_fresh_edges(arm_map),
            results, fid_by_line_by_rel)

def _group_fresh_edges(inc_arm: dict) -> dict:
    out = {}
    for key in _FWD_KEYS + _LINE_KEYS:
        out[key] = {k: {r: list(ls) for r, ls in v.items()}
                    if key in _LINE_KEYS else list(v)
                    for k, v in (inc_arm.get(key) or {}).items()}
    return out

def _strip_lines_by_rel(by_ident: "dict | None",
                        removed_rels: set[str]) -> dict:
    """行事实剔除已删除生产文件的边（行列表 sorted 拷贝，身份原序保留）。"""
    return {i: {r: sorted(ls) for r, ls in by_rel.items()
                if r not in removed_rels}
            for i, by_rel in (by_ident or {}).items()}


def _strip_fids_by_rel(by_ident: "dict | None",
                       removed_rels: set[str]) -> dict:
    """fn 视图剔除已删除生产文件的 fid（fid 列表 sorted 拷贝）。"""
    return {i: sorted(f for f in (fids or [])
                      if _fid_file_of(f) not in removed_rels)
            for i, fids in (by_ident or {}).items()}


def _dropping_empty(mapping: dict) -> dict:
    """丢弃空值条目（列表/字典视图共用的收尾过滤，顺序保留）。"""
    return {k: v for k, v in mapping.items() if v}


def _file_lists_dropping_empty(by_ident: "dict | None",
                               removed_rels: set[str]) -> dict:
    """file 列表剔除已删除生产文件并丢弃空列表身份（与既有全量口径一致）。"""
    stripped = {i: sorted(f for f in (files or [])
                          if f not in removed_rels)
                for i, files in (by_ident or {}).items()}
    return _dropping_empty(stripped)


def _rel_keyed_dropping_removed(mapping: "dict | None",
                                removed_rels: set[str]) -> dict:
    """rel 键视图（file_to_* / line_to_*）剔除已删除生产文件的键。"""
    return {r: v for r, v in (mapping or {}).items()
            if r not in removed_rels}


def _carried_gherkin_arm(baseline: dict,
                         removed_rels: set[str]) -> dict:
    """SEE-1631 §SPEC-004：增量产物的 gherkin 臂继承（无 feature 失效面）。

    身份稳定原样继承；已删除生产文件的边全视图剔除（行事实/file 反查/
    fn 视图/feature 视图/计数），不残留死边。基线无 gherkin 臂 → 空
    dict（arms.gherkin 键不出场）。"""
    garm = (baseline.get("arms") or {}).get("gherkin")
    if not isinstance(garm, dict) or not garm:
        return {}
    out = {k: v for k, v in garm.items()
           if k not in ("test_to_lines", "script_to_lines", "line_to_tests",
                        "test_to_files", "script_to_files", "file_to_tests",
                        "file_to_scripts", "test_to_functions",
                        "script_to_functions", "feature_to_lines",
                        "line_to_features")}

    test_to_lines = _strip_lines_by_rel(garm.get("test_to_lines"),
                                        removed_rels)
    script_to_lines = _strip_lines_by_rel(garm.get("script_to_lines"),
                                          removed_rels)
    test_to_files = _file_lists_dropping_empty(garm.get("test_to_files"),
                                               removed_rels)
    script_to_files = _file_lists_dropping_empty(garm.get("script_to_files"),
                                                 removed_rels)
    file_to_tests = _rel_keyed_dropping_removed(garm.get("file_to_tests"),
                                                removed_rels)
    file_to_scripts = _rel_keyed_dropping_removed(garm.get("file_to_scripts"),
                                                  removed_rels)
    test_to_functions = _dropping_empty(
        _strip_fids_by_rel(garm.get("test_to_functions"), removed_rels))
    script_to_functions = _dropping_empty(
        _strip_fids_by_rel(garm.get("script_to_functions"), removed_rels))
    line_to_tests = _rel_keyed_dropping_removed(garm.get("line_to_tests"),
                                                removed_rels)
    feature_to_lines = _dropping_empty(
        _strip_lines_by_rel(garm.get("feature_to_lines"), removed_rels))
    line_to_features = _rel_keyed_dropping_removed(
        garm.get("line_to_features"), removed_rels)
    out.update({
        "test_to_lines": test_to_lines, "script_to_lines": script_to_lines,
        "line_to_tests": line_to_tests,
        "test_to_files": test_to_files, "script_to_files": script_to_files,
        "file_to_tests": file_to_tests, "file_to_scripts": file_to_scripts,
        "test_to_functions": test_to_functions,
        "script_to_functions": script_to_functions,
        "feature_to_lines": feature_to_lines,
        "line_to_features": line_to_features,
        "pairs": sum(len(v) for v in test_to_files.values()),
        "script_pairs": sum(len(v) for v in script_to_files.values()),
        "function_pairs": sum(len(v) for v in test_to_functions.values()),
        "line_pairs": sum(len(ls) for by_rel in test_to_lines.values()
                          for ls in by_rel.values()),
        "script_line_pairs": sum(len(ls) for by_rel in script_to_lines.values()
                                 for ls in by_rel.values()),
    })
    return out


def _effective_targets(baseline: dict, root: Path,
                       removed: set[str]) -> list[str]:
    """SEE-1610 §SPEC-N1：target 面剔除已删除文件（abs/rel 双形态匹配——
    target_files 存储格式随入口不同）。"""
    removed_abs = {str((root / r).resolve()) for r in removed}
    return [t for t in (baseline.get("target_files")
                        or sorted((baseline.get("file_sha256") or {})))
            if t not in removed and str(Path(t).resolve()) not in removed_abs]


def _carried_gherkin_state(baseline: dict,
                           carried_gherkin: "dict | None") -> dict:
    """SEE-1631 §SPEC-016：arms_state.gherkin 状态原样继承（invalid 不被
    洗白），pairs 按合并后行事实重算。"""
    gh_state_out = dict((baseline.get("arms_state") or {}).get("gherkin")
                        or {})
    if carried_gherkin:
        gh_state_out["pairs"] = int(carried_gherkin.get("pairs") or 0)
    return gh_state_out


def _increment_arms_block(baseline: dict, merged: dict, classes: dict,
                          carried_gherkin: "dict | None") -> dict:
    """arms 块：gut_full 保守并集 + gherkin 臂按存在性出场。

    SEE-1610 D1：增量产物无法重测未归属事件——保守并集（基线名单
    ∪ 本轮变更/新增文件）供 test 筛选闭包整文件面判定。
    SEE-1631 §SPEC-016：gherkin 臂 feature 粒度增量——重跑键整体替换、
    其余原样继承（不因增量被静默清空）；超比例上限才具名整臂回退
    （行事实由全脸重建重绑）。"""
    arms: dict = {"gut_full": {
        **merged,
        "unattributed_files": sorted(
            set((baseline.get("arms", {}).get("gut_full") or {})
                .get("unattributed_files") or [])
            | set(classes.get("prod_changed") or [])
            | set(classes.get("prod_new") or []))}}
    if carried_gherkin:
        arms["gherkin"] = carried_gherkin
    return arms

def _drop_removed_test_identities(merged: dict, dropped_rels: set,
                                  dead_bases: "set[str] | None") -> None:
    """C3-R24（N1）：删除测试脚本按完整路径身份剔除；升级过渡旧 basename
    键仅整族已删才剔除（幸存在场保留，改写/披露归
    _canonical_verdict_dedup）。"""
    for ident in list(merged):
        sc = str(ident).partition("::")[0]
        if sc.startswith("gherkin://"):
            continue
        if sc.startswith("res://"):
            if sc[len("res://"):] in dropped_rels:
                del merged[ident]
        elif dead_bases and Path(sc).name in dead_bases:
            del merged[ident]


def _verdict_drop_removed(merged: dict, dropped_scripts: set,
                          removed_rels: set,
                          legacy_families: "dict | None" = None) -> None:
    """§SPEC-025：删除剔除——删除测试脚本按**完整路径身份**精确生效
    （C3-R24/N1：同名幸存者不受牵连）；删除生产文件（res:// rel）触达
    的身份从表中删除。"""
    dropped_rels = set(dropped_scripts or set())
    dead_bases = {base for base, (dead, _surv)
                  in (legacy_families or {}).items() if dead} or None
    _drop_removed_test_identities(merged, dropped_rels, dead_bases)
    for rel in removed_rels or set():
        for ident in list(merged):
            sc = str(ident).split("::", 1)[0]
            if sc.startswith("res://") and sc[len("res://"):] == rel:
                del merged[ident]


def _verdict_per_script(executions: list[dict]) -> dict:
    """重跑脚本执行账：脚本 basename → (rc, 解析失败名集, 脚本限定失败对,
    实际执行脚本头集)。C3-R23（V1）：同名碰撞下 `-gselect` 按 basename
    可能选中多个物理脚本——账必须携带脚本级证据（失败对 + 执行头）才能
    逐脚本归因，不能只按名字并集。C3-R24（N2/§SPEC-038⑥）：证据在
    截断之前从完整输出提取并随执行账保存（evidence 键），此处优先消费；
    out 允许截断只作披露，不再从尾部重解析——部分截断不得把本轮转红
    的同名前脚本记成绿。旧形态账（无 evidence 键）回退解析 out。"""
    from ..mutation.gutrun import (
        _parse_failing_tests, executed_script_headers,
        parse_failures_with_scripts,
    )

    per_script: dict[str, tuple] = {}
    for r in executions or []:
        script = str(r.get("script") or "")
        if not script:
            continue
        b = Path(script).name
        ev = r.get("evidence")
        if isinstance(ev, dict):
            failing = {str(x) for x in ev.get("failing") or []}
            pairs = {(str(s), str(n)) for s, n in (ev.get("pairs") or [])}
            executed = {str(x) for x in ev.get("executed") or []}
        else:
            out = r.get("out") or ""
            failing = _parse_failing_tests(out)
            pairs = parse_failures_with_scripts(out)
            executed = executed_script_headers(out)
        rc_raw = r.get("rc")
        # C3-R25（N6）：执行异常（rc 缺失 / error 在场——超时、二进制
        # 拉起失败、GUT runner 缺失）不得折成 rc=0 放绿：按失败计（保守
        # 整脚本红，既有红不洗白），与 gherkin 腿「缺失 rc 当失败」
        # （_gherkin_rerun_all_passed）同口径；错误文本随执行账
        # （rerun_executions / rerun_execution_errors）具名披露。
        if rc_raw is None or r.get("error"):
            rc = 1
        else:
            rc = int(rc_raw)
        per_script[b] = (rc, failing, pairs, executed)
    return per_script


def _script_scoped_failed(script: str, name: str, rc: int,
                          failing: "set[str]", pairs: set,
                          executed: set) -> bool:
    """C3-R23（V1）：单身份失败判定按脚本级证据归因。

    - script 是完整 `res://` 路径且运行流带脚本头：只有它自己的失败对
      判红；Run Summary 通配仅在无任何具体脚本证据时保守红（摘要段是
      运行流的复制，具体证据在场时不得外溢到同名脚本）；有失败对但
      该名字无任何失败对时取保守方向——失败名集在场即红（C3-R24/N4：
      两套解析器不一致不得记绿）；其余 → 绿；
    - 无脚本头（旧形态/输出截断只剩摘要）或裸 basename 身份（升级过渡
      无法定向）：保守名集合并集（与 0d14f98 行为一致，不放宽）。"""
    if script.startswith("res://") and executed:
        if (script, name) in pairs:
            return True
        if ("*", name) in pairs:
            concrete = {n for (s, n) in pairs if s != "*"}
            return name not in concrete
        if pairs:
            return name in failing
        return (name in failing) if failing else (rc != 0)
    return (name in failing) if failing else (rc != 0)


def _verdict_recompute_rerun(merged: dict, per_script: dict) -> None:
    """重跑身份按脚本执行账**逐测试**重算（§SPEC-025/D8，R4）：
    失败名可解析时按名判定——通过的测试不得随脚本级 rc 整体标红；
    失败名空且 rc!=0 = 不可归因（输出不可解析/崩溃）保守整脚本红
    （不得无证据放绿）；red 分类沿基线（pre_existing 不被无依据改写
    为 new）；基线绿而本轮失败 = new。
    C3-R23（V1）：同名碰撞下按脚本头证据逐脚本归因——重跑腿未执行的
    物理脚本基线判定原样继承，不被同名脚本的执行账改写。"""
    for ident in list(merged):
        sc = str(ident).split("::", 1)[0]
        b = Path(sc).name
        acct = per_script.get(b)
        if acct is None:
            continue
        rc, failing, pairs, executed = acct
        if executed and sc.startswith("res://") and sc not in executed:
            continue
        name = str(ident).split("::", 1)[1]
        failed = _script_scoped_failed(sc, name, rc, failing, pairs,
                                       executed)
        if not failed:
            merged[ident] = {"verdict": "green"}
            continue
        prior = merged.get(ident) or {}
        cls = (prior.get("classification")
               if prior.get("verdict") == "red" else None) \
            or "new"
        merged[ident] = {"verdict": "red", "classification": cls,
                         "script": prior.get("script") or sc}


def _drop_gherkin_dropped(merged: dict, gherkin_dropped: set) -> None:
    """§SPEC-028：gherkin_dropped 的 feature 身份从表剔除（零行为拆分）。"""
    if not gherkin_dropped:
        return
    from .attribution import _gherkin_feature_of

    for ident in list(merged):
        if _gherkin_feature_of(str(ident)) in gherkin_dropped:
            del merged[ident]


def _gherkin_rerun_all_passed(fresh_gherkin: dict) -> bool:
    """§SPEC-028：重跑腿**全部**执行账判定——任一 rc!=0 都算失败
    （多分片/多 rep 各算一账）；零执行账 = 失败（不落 green）。
"""
    execs = fresh_gherkin.get("executions") or []
    # rc=0 是合法成功值——falsy 陷阱（`0 or 1`）会把成功当失败
    return bool(execs) and all(
        int(r.get("rc") if r.get("rc") is not None else 1) == 0
        for r in execs)


def _gherkin_failing_scenarios(fresh_gherkin: dict) -> "set[str] | None":
    """§SPEC-025/D8（R4）：重跑腿逐 scenario 失败名集合（全部执行账并集
    ——多分片/多 rep 任一失败都算）。执行账带 `failing_scenarios`（编译
    manifest test_name → scenario 回写）时返回该集合；缺席（旧形态账）
    返回 None = 无法归因，调用方保守整键处理。"""
    failing: set[str] = set()
    seen = False
    for r in (fresh_gherkin.get("executions") or []):
        fs = r.get("failing_scenarios")
        if fs is None:
            continue
        seen = True
        failing |= {str(x) for x in fs}
    return failing if seen else None


def _gherkin_leg_exec_source(fresh_gherkin: "dict | None",
                             gh_executions: "list[dict] | None"
                             ) -> "dict | None":
    """§SPEC-028/D14（R5）：gherkin 腿执行账来源（failing_scenarios 所在
    ——fresh_gherkin 臂字典无 executions 键，真实 map-update 路径此前整
    腿保守红）。"""
    gh_leg_execs = list(gh_executions or [])
    if fresh_gherkin is not None and not gh_leg_execs:
        gh_leg_execs = list(fresh_gherkin.get("executions") or [])
    if gh_leg_execs or fresh_gherkin is not None:
        return {"executions": gh_leg_execs}
    return None

def _verdict_gherkin_rerun(merged: dict, fresh_gherkin: dict,
                           gherkin_rerun: set) -> None:
    """gherkin 重跑键（feature_changed/new）：**逐 scenario** 按重跑腿
    **全部**执行账判定（多分片/多 rep 任一失败都算失败）——通过身份
    green（不得随 feature 级 rc 整体标红）；失败或不一致 → red，分类沿
    基线（pre_existing/suspected_intolerant 保持 §SPEC-017 车道，基线绿
    而失败 = new）。`gherkin_dropped` 的剔除由 `_merged_test_verdicts`
    无条件消费（D9）。"""
    from .attribution import _gherkin_feature_of

    gh_ok = _gherkin_rerun_all_passed(fresh_gherkin)
    failing_scenarios = _gherkin_failing_scenarios(fresh_gherkin)
    for ident in list(merged):
        key = _gherkin_feature_of(str(ident))
        if key is None or key not in gherkin_rerun:
            continue
        if gh_ok:
            merged[ident] = {"verdict": "green"}
            continue
        # 失败腿：scenario 名可归因时逐名判定（通过 scenario 不标红）；
        # 失败名缺失（旧形态账）**或空集**（腿失败但零可归因名——不可
        # 归因）都保守整键红（C3-R25/N6：不得按空集逐 scenario 放绿，
        # 与 gut 腿「rc!=0 无失败名 = 保守红」对齐）。
        scenario = str(ident).split("::", 1)[1] \
            if "::" in str(ident) else ""
        failed = (not failing_scenarios) or scenario in failing_scenarios
        if not failed:
            merged[ident] = {"verdict": "green"}
            continue
        prior = merged.get(ident) or {}
        cls = (prior.get("classification")
               if prior.get("verdict") == "red" else None) or "new"
        merged[ident] = {"verdict": "red", "classification": cls,
                         "script": prior.get("script")
                         or str(ident).split("::", 1)[0]}


def _verdict_identity_index(
        tests_face: "dict[str, str] | None",
        arm_identities: "list[str] | None") -> "tuple[dict, dict]":
    """脚本 basename → res:// 完整路径表 与 basename → 宇宙身份表。"""
    tests_by_base: dict[str, list[str]] = {}
    for rel in (tests_face or {}):
        base = Path(rel).name
        tests_by_base.setdefault(base, []).append("res://" + rel)
    idents_by_base: dict[str, list[str]] = {}
    for ident in arm_identities or []:
        sc, _, nm = str(ident).partition("::")
        if nm:
            idents_by_base.setdefault(Path(sc).name,
                                      []).append(str(ident))
    return tests_by_base, idents_by_base


def _verdict_case_a_concrete(merged: dict, full_paths: "list[str]",
                             pairs: set) -> "set[str]":
    """Case A 具体归因腿：脚本头证据在场时按 (script, name) 建规范形态
    red；摘要通配（无具体证据的失败名）保守全路径红。返回已归因名集。"""
    concrete = sorted({(s, n) for (s, n) in pairs if s != "*"})
    concrete_names = {n for _s, n in concrete}
    wild = sorted({n for (s, n) in pairs if s == "*"} - concrete_names)
    for (s, n) in concrete:
        ident = f"{s}::{n}"
        if ident not in merged:
            merged[ident] = {"verdict": "red", "classification": "new",
                             "script": s}
    for n in wild:
        for fp in full_paths:
            ident = f"{Path(fp).name}::{n}"
            if ident not in merged:
                merged[ident] = {"verdict": "red", "classification": "new",
                                 "script": fp}
    return concrete_names | set(wild)


def _verdict_case_a_failures(merged: dict, full_paths: "list[str]",
                             failing: "set[str]", pairs: set,
                             executed: set) -> None:
    """Case A：执行账失败名（宇宙外身份）→ red 进表。C3-R23（V1）：有
    脚本头证据按 (script, name) 具体归因——同名碰撞下不再给未失败的
    另一目录同名脚本凭空建红；Run Summary 通配（无具体证据）与无头
    （旧输出形态）失败名保守全路径红。"""
    if executed and pairs:
        attributed = _verdict_case_a_concrete(merged, full_paths, pairs)
        residual = [n for n in sorted(failing) if n not in attributed]
    else:
        residual = sorted(failing)
    for fp in full_paths:
        base = Path(fp).name
        for name in residual:
            ident = f"{base}::{name}"
            if ident not in merged:
                merged[ident] = {"verdict": "red",
                                 "classification": "new",
                                 "script": fp}


def _verdict_add_new_identities(merged: dict, per_script: dict,
                                rerun_scripts: "set[str]",
                                tests_face: "dict[str, str] | None",
                                arm_identities: "list[str] | None") -> None:
    """重跑脚本里的新身份进判定表（§SPEC-025 宇宙覆盖 + C3-R6 Case A/B）。

    绿：行事实宇宙（merged.test_to_lines）里基线表没有的新身份 →
    `{"verdict": "green"}`（C3-R7：判定表必须覆盖行事实宇宙——通过测试
    不得从产物消失；51de7e3 行为恢复）。红：执行账（per_script 解析的
    失败名）→ red——不触达产品代码的测试没有归属事实（Case A）也要进表；
    `script` 字段 = `res://` 完整路径（`tests_face` 按 basename 反查——
    与全量建图条目同形态，物理跳过按完整路径命中；C3-R6 Case B）。"""
    tests_by_base, idents_by_base = _verdict_identity_index(
        tests_face, arm_identities)

    for script_base, (rc, failing, pairs, executed) in per_script.items():
        if script_base not in rerun_scripts:
            continue
        full_paths = tests_by_base.get(script_base, [])
        full = full_paths[0] if full_paths else None
        # 宇宙新身份：逐测试判定（与 _verdict_recompute_rerun 同口径——
        # 失败名可解析时只看 name in failing，脚本内其余通过测试记 green；
        # 全部不可解析（无失败名且 rc!=0）才整脚本保守红。C3-R8 退回2：
        # 此前 rc!=0 把同脚本其余新身份全记红 → mutation 把可杀的 mutant
        # 报成存活。C3-R23（V1）：同名碰撞按脚本级证据归因；重跑腿未
        # 执行的物理脚本不凭空补表（基线继承面负责）
        for ident in idents_by_base.get(script_base, []):
            if ident in merged:
                continue
            sc = str(ident).partition("::")[0]
            name = str(ident).partition("::")[2]
            if executed and sc.startswith("res://") and sc not in executed:
                continue
            failed = _script_scoped_failed(sc, name, rc, failing, pairs,
                                           executed)
            if not failed:
                merged[ident] = {"verdict": "green"}
            else:
                # script 字段 = 身份自己的完整路径（V1：同名碰撞下
                # full_paths[0] 是另一个目录的脚本，不得张冠李戴）
                merged[ident] = {"verdict": "red",
                                 "classification": "new",
                                 "script": (sc if sc.startswith("res://")
                                            else (full or sc))}
        # 执行账失败名（宇宙外身份，Case A）→ red（归因见
        # _verdict_case_a_failures）
        _verdict_case_a_failures(merged, full_paths, failing, pairs,
                                 executed)


def _merged_test_verdicts(baseline: dict, executions: list[dict],
                          rerun_scripts: set[str],
                          dropped_scripts: set[str],
                          removed_rels: set[str],
                          fresh_gherkin: "dict | None",
                          gherkin_rerun: set[str],
                          gherkin_dropped: set[str],
                          tests_face: "dict[str, str] | None" = None,
                          arm_identities: "list[str] | None" = None,
                          collisions: "list[dict] | None" = None,
                          legacy_families: "dict | None" = None
                          ) -> "dict | None":
    """§SPEC-025：增量 verdict 表合并（重跑重算 / 未重跑继承 / 删除剔除）。

    - 基线表原样继承（未重跑身份逐位不变）；
    - 重跑脚本的身份按脚本执行账重算：rc==0 且无解析失败名 → green；
      有失败名 → red/new（增量形态新树的失败如实记 new）；
    - gherkin 重跑键（feature_changed/new）随重跑腿执行账重算（rc==0 →
      green，否则维持基线值）；
    - 删除测试脚本（完整路径身份，C3-R24/N1）与删除生产文件触达的
      身份剔除。
    C3-R22（D4）：规范身份去重——升级过渡（旧 basename 形态基线 × 新
    res:// 形态身份）的同物理测试双键并存剔除（删旧留新 + 具名披露）。
    `legacy_families`（C3-R24/§SPEC-038⑤）＝删除面升级过渡家族态；
    缺省时按 (dropped_scripts, tests_face) 现算。
    基线无表（旧产物）→ None（消费方按缺表具名 fail-loud）。"""
    base = baseline.get("test_verdicts")
    if not isinstance(base, dict):
        return None
    families = legacy_families
    if families is None:
        families = _legacy_family_state(dropped_scripts, tests_face)
    merged = dict(base)
    _verdict_drop_removed(merged, dropped_scripts, removed_rels, families)
    # §SPEC-028/D9（R4）：gherkin_dropped 剔除无条件消费——只删不重跑的
    # 轮次（fresh_gherkin=None）同样必须剔除（C2-R3 曾把它锁在
    # fresh_gherkin 门内，真实 map-update 端到端未生效）。
    _drop_gherkin_dropped(merged, gherkin_dropped)
    per_script = _verdict_per_script(executions)
    _verdict_recompute_rerun(merged, per_script)
    _verdict_add_new_identities(merged, per_script, rerun_scripts,
                               tests_face, arm_identities)
    if fresh_gherkin is not None:
        _verdict_gherkin_rerun(merged, fresh_gherkin, gherkin_rerun)
    _canonical_verdict_dedup(merged, tests_face, arm_identities, collisions,
                             legacy_families=families)
    return merged


def _rerun_error_disclosure(executions: "list[dict] | None") -> "list[dict]":
    """C3-R25（N6）：执行异常具名披露（超时 / 拉起失败 / rc 缺失）——
    对应脚本的判定已按保守方向记红。"""
    return [
        {"script": str(r.get("script") or ""),
         "error": str(r.get("error") or "rc missing")}
        for r in (executions or [])
        if r.get("error") or r.get("rc") is None]


def _build_increment_artifact(baseline: dict, merged: dict, recipe: dict,
                              root: Path, classes: dict, rerun_list: list[str],
                              invalidation: dict, since_full: int,
                              arm_exec_s: float,
                              executions: list[dict] | None = None,
                              merged_gherkin: "dict | None" = None,
                              gh_exec_s: float = 0.0,
                              gh_executions: "list[dict] | None" = None,
                              merged_verdicts: "dict | None" = None,
                              merge_collisions: "list[dict] | None" = None
                              ) -> dict:
    removed = set(classes.get("prod_removed") or [])
    # SEE-1631 §SPEC-016：gherkin 臂继承（merged_gherkin None = 基线无
    # gherkin 臂，arms.gherkin 键不出场）；删除生产文件触达的 gherkin 边
    # 同步 purge。
    carried_gherkin = merged_gherkin
    if carried_gherkin is None:
        carried_gherkin = _carried_gherkin_arm(baseline, removed)
    targets = _effective_targets(baseline, root, removed)
    gh_state_out = _carried_gherkin_state(baseline, carried_gherkin)
    return {
        "schema_version": ATTRIBUTION_SCHEMA,
        "report_recipe_hash": recipe["recipe_hash"],
        "recipe_hash_pristine": baseline.get("recipe_hash_pristine"),
        # C3-R15（定案 4/§SPEC-016）：feature 内容指纹随增量产物前进，
        # 供 status/增量的 feature 失效面对账（per-feature 重跑通道）。
        "feature_sha256": (recipe.get("gherkin_face") or {})
        .get("feature_sha256") or {},
        "project_root": str(root),
        "identity": baseline.get("identity"),
        "causal": baseline.get("causal"),
        "file_sha256": _merged_file_sha(root, baseline, classes),
        "target_files": targets,
        "tests_face": _merged_tests_face(root, baseline, classes),
        "instrumentable_lines": _merged_instrumentable_lines(root, baseline,
                                                             classes),
        # §SPEC-029 表 #5/#6（C3-R5 项1）：生产内容指纹——场景/资源/数据
        # 文本与生产 .gd 同类，增量产物必须携带（否则续新后第一条命令即
        # 报"116 new universe files"）。按当前树现算（内容变更/新增/删除
        # 同一套逻辑）。
        "production_content_sha256": _merged_production_content(
            root, baseline, classes),
        # SEE-1631 §SPEC-021/025：arms 块（tidy 拆分 _increment_arms_block）
        # + verdict 表守恒（未重跑身份原样继承、重跑身份按新执行结果重算、
        # 删除剔除；缺表 = 消费方具名 fail-loud，不得当空红集）。
        "arms": _increment_arms_block(baseline, merged, classes,
                                      carried_gherkin),
        "test_verdicts": merged_verdicts if merged_verdicts is not None
        else dict(baseline.get("test_verdicts") or {}),
        # C3-R22（D4）：升级过渡的规范身份去重披露（旧 basename 条目被
        # 规范条目替换 / 无法恢复的旧条目保留）——与全量建图的
        # identity_collisions 同键（空列表常态键在场）。
        "identity_collisions": list(merge_collisions or []),
        "arms_state": {**dict(baseline.get("arms_state") or {}),
                       **({"gherkin": gh_state_out}
                          if carried_gherkin else {})},
        "baseline_failures_meta": baseline.get("baseline_failures_meta"),
        "notes": list(baseline.get("notes") or []),
        "increment": {
            "mode": "incremental",
            "base_recipe_hash": baseline.get("report_recipe_hash"),
            "changed_files": classes,
            "rerun_scripts": rerun_list,
            "dropped_scripts": sorted(invalidation["dropped"]),
            "rerun_sources": invalidation["sources"],
            "gherkin_rerun_features": sorted(
                invalidation.get("gherkin_rerun") or []),
            "gherkin_dropped_features": sorted(
                invalidation.get("gherkin_dropped") or []),
            "increments_since_full": since_full + 1,
            "calibration_policy": ("full face every N increments or on "
                                   "hash mismatch / new prod file / "
                                   "oversized rerun surface (deleted prod "
                                   "files stay incremental: edge purge + "
                                   "toucher rerun, SEE-1610 §SPEC-N1); "
                                   "gherkin features rerun per invalidation "
                                   "surface, own ratio cap (SEE-1631 "
                                   "§SPEC-016)"),
            "event_counts_stale": True,
            "arm_exec_s": arm_exec_s,
            "gherkin_leg_exec_s": gh_exec_s,
            "gherkin_leg_executions": gh_executions or [],
            "rerun_executions": executions or [],
            # C3-R25（N6）：执行异常的具名披露（对应脚本判定已保守记红）
            "rerun_execution_errors": _rerun_error_disclosure(executions),
        },
    }

def _merged_production_content(root: Path, baseline: dict,
                               classes: dict) -> "dict[str, str]":
    """生产内容指纹继承（§SPEC-029 表 #5/#6）：基线指纹 + 变更/新增
    重算 + 删除剔除；范围不可闭合时继承基线（不归零）。"""
    import hashlib

    from .classify import production_content_files, ScopeError

    face = dict(baseline.get("production_content_sha256") or {})
    try:
        content = production_content_files(root)
    except ScopeError:
        return face
    for rel in content:
        p = Path(root) / rel
        if not p.is_file():
            face.pop(rel, None)
            continue
        face[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    for rel in classes.get("prod_removed", []):
        face.pop(rel, None)
    # C3-R9 退回1：已删除的非脚本内容文件不再在盘上（上面的重算循环扫
    # 不到它们）——陈旧指纹条目必须剔除，否则装填后复检永不过期
    on_disk = set(content)
    for rel in list(face):
        if rel not in on_disk:
            face.pop(rel, None)
    return face


def _merged_tests_face(root: Path, baseline: dict,
                       classes: dict) -> dict:
    """测试面内容绑定继承 + 变更/新增测试脚本按当前内容重算、删除剔除。"""
    face = dict(baseline.get("tests_face") or {})
    for rel in classes.get("test_changed", []) + classes.get("test_new", []):
        p = Path(root) / rel
        if p.is_file():
            face[rel] = sha256_file(p)
    for rel in classes.get("test_removed", []):
        face.pop(rel, None)
    return face

def _merged_file_sha(root: Path, baseline: dict,
                     classes: dict) -> dict:
    """基线文件哈希继承 + 变更生产文件按当前内容重算、删除文件剔除
    （增量新鲜绑定）。"""
    shas = dict(baseline.get("file_sha256") or {})
    for rel in classes.get("prod_changed", []) + classes.get("prod_new", []):
        p = Path(root) / rel
        if p.is_file():
            shas[rel] = sha256_file(p)
    for rel in classes.get("prod_removed", []):
        shas.pop(rel, None)
    return shas

def _merged_instrumentable_lines(root: Path, baseline: dict,
                                 classes: dict) -> dict:
    """探针面（可插桩行全集）继承 + 变更生产文件按当前源码重算——
    与模板构建同源（_instrument_blocks），行级主真值的测量宇宙随之更新。"""
    surface = dict(baseline.get("instrumentable_lines") or {})
    from ..coverage.runner import _instrument_blocks
    for rel in classes.get("prod_changed", []) + classes.get("prod_new", []):
        p = Path(root) / rel
        if not p.is_file():
            surface.pop(rel, None)
            continue
        src = p.read_text(encoding="utf-8")
        _tpl, probe_lines, block_map = _instrument_blocks(
            src, "gqt-surface-computation.txt", runtime_sink=False)
        surface[rel] = _probe_surface_lines(probe_lines, block_map)
    for rel in classes.get("prod_removed", []):
        surface.pop(rel, None)
    return surface

def _fallback_full_face(root: Path, target_files: list[str], recipe: dict,
                        out_path: str | Path, classes: dict, reason: str,
                        causal: bool, timeout_s: int, t0: float) -> dict:
    # D1：新增生产文件必须并入重建 target 面——否则 brand_new.gd 永远
    # 不进图（回退仅重建基线 targets 时新文件被静默漏测）。
    # SEE-1610 §SPEC-N1：已删除文件从重建 target 面剔除（基线继承的
    # target 列表可能仍含删除 rel——auto 续新路径以基线 target_files 为入参）。
    targets = [t for t in target_files if Path(t).is_file()]
    for rel in classes.get("prod_new") or []:
        abs_path = str((Path(root) / rel).resolve())
        if abs_path not in targets:
            targets.append(abs_path)
    report = run_measurement(root, targets, recipe,
                           timeout_s=timeout_s, test_attribution=True,
                           attribution_causal=causal,
                           attribution_out=str(out_path))
    artifact_path = Path(out_path)
    if artifact_path.is_file():
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["increment"] = {
            "mode": reason,
            "base_recipe_hash": None,
            "changed_files": classes,
            "increments_since_full": 0,
            "reason": reason,
        }
        artifact_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return {
        "mode": reason,
        "artifact": str(artifact_path),
        "changed": classes,
        "canonical_ok": bool(report.get("ok")),
        "wall_s": round(time.monotonic() - t0, 3),
    }

def diff_artifacts(a_doc: dict, b_doc: dict) -> dict:
    """两张归属产物的逐边差分（file/function 边 + 行事实三元组）。

    返回每张边的 {added_count, removed_count, added_edges(截断 200),
    removed_edges(截断 200)}——added = 仅 b 有，removed = 仅 a 有。
    行事实边按 (identity, rel, line) 三元组粒度差分。"""
    def edges(doc: dict, key: str) -> dict[str, set[str]]:
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        return {k: set(v) for k, v in (arm.get(key) or {}).items()}

    def line_edges(doc: dict, key: str) -> dict[str, set[str]]:
        arm = (doc.get("arms") or {}).get("gut_full") or {}
        out: dict[str, set[str]] = {}
        for identity, by_rel in (arm.get(key) or {}).items():
            for rel, lines in by_rel.items():
                edge = f"{identity} -> {rel}"
                out.setdefault(edge, set()).update(
                    str(ln) for ln in lines)
        return out

    def _diff(a: dict, b: dict) -> dict:
        added = sorted(f"{k} -> {x}" for k, v in b.items()
                       for x in v - a.get(k, set()))
        removed = sorted(f"{k} -> {x}" for k, v in a.items()
                         for x in v - b.get(k, set()))
        return {
            "added_count": len(added), "removed_count": len(removed),
            "added_edges": added[:200], "removed_edges": removed[:200],
        }

    out = {}
    for key in _FWD_KEYS:
        out[key] = _diff(edges(a_doc, key), edges(b_doc, key))
    for key in _LINE_KEYS:
        out[key] = _diff(line_edges(a_doc, key), line_edges(b_doc, key))
    return out

