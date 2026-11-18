"""coverage 前置过滤的持久化链路（SEE-1420 P2 → SEE-1587 → SEE-1594 S2）。

SEE-1594 S2（A04/A05/A06）单一真源链：行级主真值映射（test-attribution/6）
是覆盖与 prefilter 的唯一真源；``see1420-coverage/2``（``<project>/.qa-cache/
gqt-coverage/<key>.json``）只是该映射的**派生缓存**——绑定包含映射内容与
执行证据指纹（A05），映射任一内容变化即显式失效；live projection 消费
selection 已解析的映射路径（A06），不重猜默认。旧 `/1` 手动 store 的
writer/reader/prefilter 层已物理退役（A04）；盘上残留 /1 条目按 schema
分流永不命中（宁 miss 不假过滤）。

- 投影条目携带绑定包（map schema / recipe pristine / 文件 sha / 测试面
  摘要 / 映射内容 sha / 执行证据 sha）——读回要求与当前树和当前映射全
  匹配，任一漂移 = 显式失效（可再投影，绝不假过滤）；
- 覆盖数据是「行级事实」，位点级结论由 mutation 侧按位点行号现推——
  uncovered 不持久化、不进 v2 条目（coverage 刷新后立即生效）。
"""

from __future__ import annotations
from ..digest import canonical_sha256, sha256_file

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ..timing import binary_kind_tag, project_relative, store_key
from .identity import COLD_SCHEME

# 退役的 /1 手动 schema 字面量：仅保留为盘上残留条目的拒绝守卫
# （_projection_cache_basic_reject），不再有任何写入/读取路径。
SCHEMA_COVERAGE = "see1420-coverage/1"
SCHEMA_COVERAGE_PROJECTION = "see1420-coverage/2"
PROJECTION_SOURCE = "attribution_line_facts"

COVERAGE_CACHE_DIRNAME = "gqt-coverage"

# S2.1 MEDIUM-1：map_path 形参哨兵——调用方未解析映射（仅直接调用/旧测试
# 兼容回退默认解析）；显式 None = selection 已运行但无 mapping（如显式
# --tests）→ 显式跳过 map 派生层，绝不重猜默认 map。S2.2：公开常量供
# planner/executor 形参缺省复用（单一哨兵语义，避免第三套漂移）。
# S2.3：runtime_mapping 却无 path = 元数据不一致 → 独立哨兵，消费侧以
# 具名不一致原因披露（绝不冒充 explicit --tests 文案）。
_UNRESOLVED_MAP = object()
MAP_PATH_UNRESOLVED = _UNRESOLVED_MAP
MAP_PATH_METADATA_INCONSISTENT = object()


def coverage_identity(identity: dict | None) -> dict | None:
    """coverage 通道身份（§SPEC-208 D-2）：exec_scheme 恒 canonical cold。

    coverage 是通道无关事实（行覆盖不依赖执行通道，verdict 等价已证）——
    写侧唯一调用点恒 cold（coverage CLI 无 worker 旗标），读侧（prefilter）
    若按调用方通道身份比对，worker 路由目标对 cold 条目恒 drift → 过滤
    永久失效。本归一让「合法 coverage」以正确身份命中；其余身份成分
    （ops/godot 版本/wiring/gut 哈希）仍全匹配校验——身份不符仍禁用并
    披露（宁 miss 不假绿）。manifest/store2 键不经此函数（维持通道身份，
    D-1 语义不动）。"""
    if identity is None:
        return None
    return {**identity, "exec_scheme": COLD_SCHEME}


def coverage_cache_path(project_root: str, file_path: str,
                        tests_glob: str | None,
                        binary_kind: str | None = None) -> Path:
    key = store_key(project_root, file_path, "coverage", tests_glob,
                    binary_kind=binary_kind_tag(binary_kind))
    return (Path(str(Path(project_root).resolve())) / ".qa-cache"
            / COVERAGE_CACHE_DIRNAME / f"{key}.json")


def save_coverage(covered_lines: set[int], total_lines: int,
                  project_root: str, file_path: str, tests_glob: str | None,
                  binary_kind: str | None = None,
                  coverage_percent: float | None = None,
                  instrumentable_lines: set[int] | None = None,
                  identity: dict | None = None,
                  projection: dict | None = None) -> bool:
    """投影行级事实落盘（原子写；失败告警一次，best-effort）。

    projection（A05 绑定包）必填：条目恒为 see1420-coverage/2 派生缓存
    （A04：/1 手动 schema 已退役，本写侧不再产出无绑定条目）。
    instrumentable_lines（D3）：本次投影的探针可插桩行集——mutation 前置
    过滤的唯一合法跳过锚。
    identity（SEE-1550 L1）：缓存身份包入档——读回侧要求 fresh ∧ 身份包全
    匹配才允许 prefilter 生效。
    """
    if projection is None:
        raise ValueError(
            "save_coverage requires a projection binding (see1420-coverage/2 "
            "derived cache only; the manual /1 schema is retired)")
    kind = binary_kind_tag(binary_kind)
    rel = project_relative(project_root, file_path)
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        print(f"coverage cache: cannot read {file_path}: {e} — not saved",
              file=sys.stderr)
        return False
    doc = {
        "schema": SCHEMA_COVERAGE_PROJECTION,
        "rel": rel,
        "tests_glob": tests_glob,
        "binary_kind": kind,
        "source_sha1": source_sha1,
        "total_lines": int(total_lines),
        "covered_lines": sorted(int(l) for l in covered_lines),
        "coverage_percent": coverage_percent,
        "recorded_at": time.time(),
        "projection": dict(projection),
    }
    if identity is not None:
        # §SPEC-208：落档身份归一为 coverage 通道身份（cold/canonical）——
        # 现有写侧（coverage/runner 恒 cold）为 no-op，未来任何通道调用都
        # 产出通道无关条目（读侧校验同源）。
        doc["identity"] = coverage_identity(identity)
    if instrumentable_lines is not None:
        doc["instrumentable_lines"] = sorted(int(l) for l in instrumentable_lines)
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        gitignore = path.parent / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n", encoding="utf-8")
        tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(doc, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"coverage cache write failed ({path}): {e} — continuing",
              file=sys.stderr)
        return False


# ---------------------------------------------------------------- SEE-1587
# 同轮插桩行事实投影：test-attribution/6 行事实 → (file, tests_glob) 条目


def tests_face_digest(tests_face: dict) -> str:
    """测试面摘要（投影绑定包成分）：排序稳定 JSON sha256。"""
    canon = {str(k): str(v) for k, v in sorted((tests_face or {}).items())}
    return canonical_sha256(canon)


def _canonical_sha256(obj) -> str:
    """任意 JSON 值的排序稳定 sha256（A05 绑定指纹的统一实现）。"""
    return canonical_sha256(obj)


def current_tests_face(project_root: str | os.PathLike) -> dict[str, str]:
    """tests/ 树全部 .gd 的 rel→sha256 现算（与 truth_select 同一口径）。"""
    root = Path(project_root)
    out: dict[str, str] = {}
    tests = root / "tests"
    if tests.is_dir():
        for p in sorted(tests.rglob("*.gd")):
            try:
                out[str(p.relative_to(root))] = sha256_file(p)
            except OSError:
                continue
    return out


def _projection_binding(doc: dict, rel: str) -> dict | None:
    """投影绑定包：map schema / recipe pristine / 文件 sha / 测试面摘要 /
    映射内容 sha / 执行证据 sha（A05）。doc 缺行级主真值前置（schema 非 /6
    或绑定缺失）→ None（拒绝投影）。"""
    binding = {
        "source": PROJECTION_SOURCE,
        "map_schema": doc.get("schema_version"),
        "map_recipe_pristine": doc.get("recipe_hash_pristine"),
        "map_file_sha256": (doc.get("file_sha256") or {}).get(rel),
        "map_tests_face_sha": tests_face_digest(doc.get("tests_face") or {}),
        "map_content_sha256": _canonical_sha256(doc),
        "execution_evidence_sha256": _canonical_sha256(
            doc.get("arms_state") or {}),
    }
    if not binding["map_recipe_pristine"] or not binding["map_file_sha256"]:
        return None
    return binding


def _projection_map_reject(
        root: Path, doc: dict, rel: str,
        current_recipe_pristine: "str | None",
        tests_face: "dict | None") -> "str | None":
    """行事实投影的前置校验链（写侧，逐项顺序与原因文案保持稳定）：
    map schema / 臂有效性 / 目标文件 sha / recipe pristine / 测试面摘要 /
    探针面。任一失配返回拒绝原因——消费侧回退不过滤；通过返回 None。"""
    from ..measurement.attribution import ATTRIBUTION_SCHEMA
    from .truth_select import arms_valid

    if doc.get("schema_version") != ATTRIBUTION_SCHEMA:
        return (f"attribution map schema "
                f"{doc.get('schema_version')!r} != "
                f"{ATTRIBUTION_SCHEMA!r}")
    if not arms_valid(doc.get("arms_state") or {}):
        return "attribution map arms invalid"
    try:
        file_sha = sha256_file(root / rel)
    except OSError as e:
        return f"cannot read target file: {e}"
    if (doc.get("file_sha256") or {}).get(rel) != file_sha:
        return ("attribution map file binding stale "
                "(target changed since measurement)")
    if not current_recipe_pristine:
        return "current recipe hash unavailable"
    if doc.get("recipe_hash_pristine") != current_recipe_pristine:
        return "attribution map recipe drift"
    face_now = tests_face if tests_face is not None else current_tests_face(
        str(root))
    if tests_face_digest(doc.get("tests_face") or {}) != tests_face_digest(
            face_now):
        return "tests face drift since measurement"
    instrumentable = (doc.get("instrumentable_lines") or {}).get(rel)
    if not isinstance(instrumentable, list) or not instrumentable:
        return "attribution map lacks instrumentable surface for file"
    return None


def project_coverage_from_map(
        project_root: str, doc: dict, target_file: str,
        scripts: "list[str] | set[str]", tests_glob: str,
        *, binary_kind: str | None = None,
        identity: dict | None = None,
        current_recipe_pristine: str | None = None,
        tests_face: dict | None = None,
        ) -> tuple[set[int] | None, str | None]:
    """从行级主真值映射投影 (file, tests_glob) 的 covered 行集并落盘。

    scripts = 该 tests_glob 选择目录承载的测试脚本 res:// 集合（并集语义：
    覆盖行 = 选中测试身份行事实的并集）；行集来自同一轮插桩的行事实，
    零第二次插桩、零 GUT 轮次。

    任一前置不满足 → (None, 原因)——消费侧一律回退不过滤（宁多跑不假
    过滤）：map schema 非 test-attribution/6、臂无效、目标文件 sha 失配、
    recipe pristine 失配、测试面漂移、探针面缺失。"""
    root = Path(project_root).resolve()
    rel = project_relative(str(root), str(Path(target_file).resolve()))
    reason = _projection_map_reject(root, doc, rel, current_recipe_pristine,
                                    tests_face)
    if reason is not None:
        return None, reason
    instrumentable = (doc.get("instrumentable_lines") or {}).get(rel)
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    # A12（SEE-1594）：行事实身份归属禁 basename 猜测并集。注册表身份的
    # 脚本段是 GUT 裸文件名（CollectedScript.get_filename() = path.get_file()），
    # 无法区分同名不同目录脚本；投影归属规则：
    # - 全路径身份（res://...）：与 selection 脚本精确全等；
    # - 裸名身份：唯一同名 selected 脚本才可归属；该裸名在 map tests_face
    #   跨多个目录，或 selection 同名脚本多于一个 → 歧义 fail-loud。
    script_set = {str(s) for s in scripts}
    bare_sel = sorted(s for s in script_set if not s.startswith("res://"))
    if bare_sel:
        return None, (
            "line facts identity mismatch: selection scripts must be full "
            f"res:// paths, got {bare_sel} — full res:// script identity "
            "required (ambiguous attribution)")
    face_dirs: dict[str, set[str]] = {}
    for face_rel in (doc.get("tests_face") or {}):
        face_dirs.setdefault(Path(face_rel).name, set()).add(
            Path(face_rel).parent.as_posix())

    def _match(sp: str) -> "tuple[set[str], str | None]":
        """身份脚本段 → (候选 selected 脚本集, 歧义原因)。S2.1 MEDIUM-2：
        目录一致性在归属函数内部校验——selected 脚本目录必须落在 map
        tests_face 记录的该裸名目录内，不依赖 caller 纪律。"""
        if sp.startswith("res://"):
            return ({sp} if sp in script_set else set()), None
        cands = {s for s in script_set if Path(s).name == sp}
        if len(cands) > 1:
            return set(), (f"same-name script(s) [{sp}] selected from "
                           "multiple directories")
        if len(cands) == 1:
            face = face_dirs.get(sp, set())
            if len(face) > 1:
                return set(), (f"same-name script(s) [{sp}] exist across "
                               f"directories {sorted(face)}")
            # res:// 经 pathlib 会折叠成 res:/——先剥前缀再取目录，
            # 与 tests_face 的 rel 目录同一口径比较
            cand_dir = Path(
                next(iter(cands)).replace("res://", "", 1)).parent.as_posix()
            if cand_dir not in face:
                return set(), (f"same-name script(s) [{sp}] selected from "
                               f"[{cand_dir}] but map tests_face records "
                               f"{sorted(face)} — attribution unverifiable")
        return cands, None

    covered: set[int] = set()
    ambiguity: "str | None" = None
    for ident, by_rel in (arm.get("test_to_lines") or {}).items():
        sp = str(ident).split("::", 1)[0]
        cands, why = _match(sp)
        if why is not None:
            if ambiguity is None:
                ambiguity = why
            continue
        if not cands:
            continue
        covered.update(int(l) for l in (by_rel.get(rel) or ()))
    for script, by_rel in (arm.get("script_to_lines") or {}).items():
        sp = str(script)
        cands, why = _match(sp)
        if why is not None:
            if ambiguity is None:
                ambiguity = why
            continue
        if not cands:
            continue
        covered.update(int(l) for l in ((by_rel or {}).get(rel) or ()))
    if ambiguity is not None:
        return None, (
            f"line facts identity mismatch: {ambiguity} — full res:// "
            "script identity required (ambiguous attribution)")
    inst_set = {int(l) for l in instrumentable}
    covered &= inst_set
    binding = _projection_binding(doc, rel)
    if binding is None:
        return None, "attribution map lacks line-primary bindings"
    covered_pct = (100.0 * len(covered) / len(inst_set)
                   if inst_set else 100.0)
    save_coverage(covered, len(inst_set), str(root), str(root / rel),
                  tests_glob, binary_kind=binary_kind,
                  coverage_percent=round(covered_pct, 2),
                  instrumentable_lines=inst_set, identity=identity,
                  projection=binding)
    return covered, None


def _projection_cache_basic_reject(
        raw: dict, project_root: str, file_path: str, tests_glob: str,
        kind: str, identity: "dict | None") -> "str | None":
    """投影缓存条目的基础 provenance 校验（schema 分流 / rel / glob /
    kind / 身份包 / 源码 sha1）。失配返回显式原因；通过返回 None。"""
    if raw.get("schema") == SCHEMA_COVERAGE:
        return ("coverage cache is a manual measurement entry "
                "(see1420-coverage/1), not a projection")
    if raw.get("schema") != SCHEMA_COVERAGE_PROJECTION:
        return "coverage cache schema mismatch"
    if raw.get("rel") != project_relative(project_root, file_path):
        return "coverage cache path mismatch"
    if raw.get("tests_glob") != tests_glob:
        return "coverage cache tests_glob mismatch (exact-match rule)"
    if raw.get("binary_kind") != kind:
        return "coverage cache binary kind mismatch"
    if identity is not None and raw.get("identity") != coverage_identity(
            identity):
        return ("coverage cache identity drift (fresh + identity-pack "
                "match required — store invalidated)")
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        return f"cannot read target file: {e}"
    if raw.get("source_sha1") != source_sha1:
        return "coverage cache stale (source changed since measurement)"
    return None


def _projection_binding_reject(
        binding: dict, file_path: str,
        current_recipe_pristine: "str | None",
        tests_face: "dict | None", project_root: str,
        current_doc: "dict | None" = None) -> "str | None":
    """投影绑定包校验（目标文件 sha / recipe pristine / 测试面摘要 /
    映射内容 sha / 执行证据 sha）。失配返回显式原因；通过返回 None。"""
    try:
        file_sha = sha256_file(file_path)
    except OSError as e:
        return f"cannot read target file: {e}"
    if binding.get("map_file_sha256") != file_sha:
        return "projection binding stale (target changed since map)"
    if not current_recipe_pristine:
        return "current recipe hash unavailable"
    if binding.get("map_recipe_pristine") != current_recipe_pristine:
        return "projection binding stale (recipe drift)"
    root = Path(project_root).resolve()
    face_now = tests_face if tests_face is not None else current_tests_face(
        str(root))
    if binding.get("map_tests_face_sha") != tests_face_digest(face_now):
        return "projection binding stale (tests face drift)"
    if current_doc is None:
        # A05：绑定含映射内容/执行证据指纹——当前映射不可读即无法验证，
        # 宁 miss 不假命中（绝不绑定无主缓存）。
        return "attribution map unavailable for projection binding check"
    if binding.get("map_content_sha256") != _canonical_sha256(current_doc):
        return ("projection binding stale (attribution map content "
                "changed since projection)")
    if binding.get("execution_evidence_sha256") != _canonical_sha256(
            current_doc.get("arms_state") or {}):
        return ("projection binding stale (execution evidence changed "
                "since projection)")
    return None


def _load_projection_raw(
        project_root: str, file_path: str, tests_glob: str,
        binary_kind=None, identity: dict | None = None, *,
        current_recipe_pristine: str | None = None,
        tests_face: dict | None = None,
        current_doc: "dict | None" = None,
        ) -> tuple[dict | None, str | None]:
    """投影条目读取 + 全量 provenance 校验（load 侧唯一实现）。

    - /1（已退役手动条目，盘上残留）→ (None, 原因)——永不命中；
    - /2 但基础 provenance（rel/glob/kind/身份包/源码 sha1）或绑定包与
      当前树/当前映射任一失配 → (None, 显式失效原因)——显式失效 = 消费
      侧回退不过滤并可再投影，绝不假过滤。"""
    kind = binary_kind_tag(binary_kind)
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no projected coverage for this (file, tests_glob)"
    if not isinstance(raw, dict):
        return None, "coverage cache malformed"
    reason = _projection_cache_basic_reject(raw, project_root, file_path,
                                            tests_glob, kind, identity)
    if reason is not None:
        return None, reason
    binding = raw.get("projection")
    if not isinstance(binding, dict) \
            or binding.get("source") != PROJECTION_SOURCE:
        return None, "coverage cache lacks projection binding"
    reason = _projection_binding_reject(binding, file_path,
                                        current_recipe_pristine, tests_face,
                                        project_root, current_doc=current_doc)
    if reason is not None:
        return None, reason
    return raw, None


def load_projected_coverage(
        project_root: str, file_path: str, tests_glob: str,
        binary_kind=None, identity: dict | None = None, *,
        current_recipe_pristine: str | None = None,
        tests_face: dict | None = None,
        current_doc: "dict | None" = None,
        ) -> tuple[set[int] | None, str | None]:
    """读回投影条目的 covered 行集（/2 唯一读回口径；校验见
    _load_projection_raw——含映射内容/执行证据绑定）。"""
    raw, reason = _load_projection_raw(
        project_root, file_path, tests_glob, binary_kind, identity,
        current_recipe_pristine=current_recipe_pristine, tests_face=tests_face,
        current_doc=current_doc)
    if raw is None:
        return None, reason
    lines = raw.get("covered_lines")
    if not isinstance(lines, list):
        return None, "coverage cache malformed (covered_lines missing)"
    return ({int(l) for l in lines if isinstance(l, (int, float))}, None)


def _prefilter_projection_store(
        project_root: str, file_path: str, tests_glob: str,
        binary_kind, identity: "dict | None", pristine: str,
        current_doc: "dict | None" = None,
        ) -> "tuple[set[int] | None, set[int] | None, str | None]":
    """prefilter 解析链 store 层：see1420-coverage/2 派生缓存条目（绑定包
    含映射内容/执行证据指纹，与当前映射全匹配才可用；A05：单次读取同时
    取 covered 与 instrumentable）。返回 (covered, instrumentable, 原因)；
    covered 非 None 而 instrumentable 为 None = 条目有效但 face 缺失
    （消费侧按 instrumentable_face_missing 披露，不过滤）。"""
    raw, why = _load_projection_raw(
        project_root, file_path, tests_glob, binary_kind, identity,
        current_recipe_pristine=pristine, current_doc=current_doc)
    if raw is None:
        return None, None, why
    lines = raw.get("covered_lines")
    if not isinstance(lines, list):
        return None, None, "coverage cache malformed (covered_lines missing)"
    cov = {int(l) for l in lines if isinstance(l, (int, float))}
    inst = {int(l) for l in raw.get("instrumentable_lines") or []
            if isinstance(l, (int, float))}
    if not inst:
        return cov, None, "instrumentable face unavailable"
    return cov, inst, None


def _prefilter_live_projection(
        project_root: str, file_path: str, tests_glob: str,
        scripts: "list[str] | set[str] | None", binary_kind,
        identity: "dict | None", pristine: str, *,
        doc: "dict | None", map_path: "str | None",
        ) -> "tuple[set[int] | None, set[int] | None, str | None]":
    """prefilter 解析链 live 层：新鲜行级主真值映射现场投影（scripts =
    本次 tests_glob 承载的测试脚本集；零第二次插桩/零 GUT），落盘为 /2
    条目供后续复用。doc/map_path 由调用方传入 selection 已解析的映射
    （A06：本层不重猜默认路径），与 store 层共享同一次加载（A05）。
    返回 (covered, instrumentable, 失败原因)。"""
    if not map_path:
        return None, None, "no attribution map for live projection"
    if doc is None:
        return None, None, "attribution map unusable for live projection"
    if not scripts:
        return None, None, "selection scripts unknown for live projection"
    doc_cov, doc_reason = project_coverage_from_map(
        project_root, doc, file_path, scripts, tests_glob,
        binary_kind=binary_kind, identity=identity,
        current_recipe_pristine=pristine)
    if doc_cov is None:
        return None, None, doc_reason
    rel = project_relative(str(Path(project_root).resolve()),
                           str(Path(file_path).resolve()))
    doc_inst = doc.get("instrumentable_lines", {}).get(rel)
    if isinstance(doc_inst, list) and doc_inst:
        return doc_cov, {int(l) for l in doc_inst}, None
    return None, None, "map lacks instrumentable surface"


def selection_map_path(selection_meta: dict) -> "str | None | object":
    """selection_meta → prefilter map_path（S2.2/S2.3/S3 单一出处）。

    - source=runtime_mapping 但无 resolved map path（mapping 为 None 或
      无 path 的 dict 均算）→ MAP_PATH_METADATA_INCONSISTENT（元数据不一致
      哨兵：消费侧以具名不一致原因披露，绝不误报 explicit --tests、绝不
      默认重猜）；
    - 其余按既有语义返回 path 或 None（显式 tests/全脸 = selection 无映射，
      explicit-None 披露归其专属）。"""
    sel_mapping = selection_meta.get("mapping")
    map_path = (sel_mapping or {}).get("path")
    if not map_path and selection_meta.get("source") == "runtime_mapping":
        return MAP_PATH_METADATA_INCONSISTENT
    return map_path


def apply_coverage_prefilter(executed, file_path, project_root, tests_glob,
                             tests_scripts, binary_kind, identity,
                             reuse_coverage, disclosures,
                             map_path=_UNRESOLVED_MAP):
    """A07（SEE-1594 S3）：mutation coverage 前置过滤的**单一应用 helper**
    （planner/executor 共用，禁止复制后再分叉）。

    语义单点：reuse 关断即全量；解析链经 resolve_coverage_for_prefilter；
    miss = stderr 告警 + 不过滤（宁多跑不假阴性），disclosures 只在提供时
    写入；D3 锚 = uncovered ∈ instrumentable ∧ ∉ covered——不可插桩行上的
    位点留在执行面。返回 (uncovered, pool)。"""
    if not reuse_coverage:
        return [], executed
    cov, inst, reason, source = resolve_coverage_for_prefilter(
        project_root, file_path, tests_glob, scripts=tests_scripts,
        binary_kind=binary_kind, identity=identity, map_path=map_path)
    if cov is None or inst is None:
        print(f"mutation: coverage store miss ({reason}) for {file_path} "
              f"— running unfiltered", file=sys.stderr)
        if disclosures is not None:
            disclosures["coverage_prefilter"] = source or "store_miss"
        return [], executed
    if disclosures is not None:
        disclosures["coverage_prefilter"] = source
    uncovered = [m for m in executed
                 if m.line not in cov and m.line in inst]
    pool = [m for m in executed
            if m.line in cov or m.line not in inst]
    return uncovered, pool


COVERAGE_PREFILTER_MISS_STATES = ("store_miss", "instrumentable_face_missing")
COVERAGE_PREFILTER_SOURCES = ("projection_store", "projection_live")


def coverage_prefilter_summary(source, uncovered, covered_executed) -> dict:
    """A08（SEE-1594 S3）：summary.coverage_prefilter dict 的唯一 writer
    （serial 与 orchestrator/worker 聚合同源；键集即契约形状，miss 态为
    str 由调用方按 MISS_STATES 判定后直传）。"""
    return {"source": source, "uncovered": uncovered,
            "covered_executed": covered_executed}


def resolve_coverage_for_prefilter(
        project_root: str, file_path: str, tests_glob: str | None,
        scripts: "list[str] | set[str] | None" = None,
        binary_kind=None, identity: dict | None = None,
        map_path: "str | None | object" = MAP_PATH_UNRESOLVED,
        ) -> tuple[set[int] | None, set[int] | None, str | None, str | None]:
    """mutation prefilter 统一解析链（SEE-1587 消费面单一出处；
    SEE-1594 S2/S2.1 单真源化）。

    返回 (covered, instrumentable, reason, source)：
    1. `projection_store`——`see1420-coverage/2` 派生缓存（绑定含映射内容
       与执行证据指纹，与当前映射全匹配才可用）；
    2. `projection_live`——新鲜行级主真值映射现场投影（scripts = 本次
       tests_glob 承载的测试脚本集；零第二次插桩/零 GUT），落盘为 /2 条目
       供后续复用。

    map_path（A06/S2.1）三态：
    - 未传（哨兵）：直接调用/旧测试兼容——回退默认解析；
    - 路径：selection 实际解析的映射（executor/planner 传
      `selection_meta["mapping"]["path"]`），原样消费；
    - 显式 None：selection 已运行但无 mapping（显式 `--tests` 等）——
      **显式跳过 store/live 并披露**，绝不重猜默认 map（生产流单真源不靠
      "恰好一致"）。
    两层共享同一次映射加载（A05 合并读取）。旧 `/1` 手动 store 已退役
    （A04），盘上残留条目按 schema 分流永不命中。

    任一层失败都继续下沉，全部失败原因**逐层保留**（reason 聚合，身份漂移
    /绑定漂移不因下沉而不可见）；全部不可用 → (None, None, 原因,
    state)——state ∈ {store_miss, instrumentable_face_missing}，调用方回退
    不过滤（宁多跑不假过滤）。"""
    if tests_glob is None:
        return None, None, "no tests_glob (full-face audit)", "store_miss"
    from . import truth_select as _truth

    root = str(Path(project_root).resolve())
    reasons: list[str] = []
    doc: dict | None = None
    if map_path is _UNRESOLVED_MAP:
        resolved = _truth.map_path(root)
        if not resolved:
            reasons.append("no attribution map for projection")
    elif map_path is MAP_PATH_METADATA_INCONSISTENT:
        resolved = None
        reasons.append("selection metadata inconsistent: source="
                       "runtime_mapping without resolved map path — "
                       "map-derived coverage prefilter skipped")
    elif map_path:
        resolved = map_path
    else:
        resolved = None
        reasons.append("selection resolved no attribution map (explicit "
                       "--tests override) — map-derived coverage prefilter "
                       "skipped")
    if resolved:
        try:
            doc = _truth.load_map(resolved)
        except Exception as e:  # noqa: BLE001 — 不可用即下沉，不吞掉原因
            reasons.append(f"attribution map unusable: {e}")
    pristine = _truth._fresh_recipe_hash(root)
    proj_cov, proj_inst, proj_why = _prefilter_projection_store(
        project_root, file_path, tests_glob, binary_kind, identity, pristine,
        current_doc=doc)
    if proj_cov is not None and proj_inst is not None:
        return proj_cov, proj_inst, None, "projection_store"
    if proj_why:
        reasons.append(f"projection store: {proj_why}")
    live_cov, live_inst, live_why = _prefilter_live_projection(
        project_root, file_path, tests_glob, scripts, binary_kind, identity,
        pristine, doc=doc, map_path=resolved)
    if live_cov is not None:
        return live_cov, live_inst, None, "projection_live"
    if live_why:
        reasons.append(f"live projection: {live_why}")
    saw_cov = proj_cov is not None
    state = "instrumentable_face_missing" if saw_cov else "store_miss"
    return None, None, "; ".join(reasons) or "coverage unavailable", state
