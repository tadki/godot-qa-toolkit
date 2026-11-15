"""SEE-1420 Phase 3 — coverage 前置过滤的持久化链路（定案 L3/D7，P2）。

复用自家 JSON 落盘（拒绝 LCOV 外部格式）：
- ``<project>/.qa-cache/gqt-coverage/<key>.json``——`gqt measure coverage`
  成功 run 的扩展落盘（stdout 契约的超集：多带全量 covered_lines；stdout 契约不动）；
- ``--reuse-coverage`` 读回时做 provenance 校验：schema + 项目内相对路径 +
  源码 sha1 + tests_glob 精确匹配 + 二进制类型——任一不符 = 无该数据
  （mutation 侧告警后【不过滤】继续跑，宁多跑不假阴性）。
- 覆盖数据是「行级事实」，位点级结论由 mutation 侧按位点行号现推——
  uncovered 不持久化、不进 v2 条目（coverage 刷新后立即生效）。

SEE-1587 — 同轮插桩 Coverage Store 投影（see1420-coverage/2）：
- 行级主真值映射（test-attribution/6）携带 per-test covered line sets，
  按 mutation 实际测试集合（选择目录 tests_glob）做行集并集即得该 glob 的
  行覆盖——纯派生，零第二次插桩、零 GUT 轮次；
- 投影条目升 schema 为 ``see1420-coverage/2`` 并携带绑定包（map schema /
  recipe pristine / 文件 sha / 测试面摘要）——旧 prefilter 读 /2 一律
  schema mismatch 不过滤（fail-safe）；/2 读回要求绑定包与当前树全匹配，
  任一漂移 = 显式失效（可再投影，绝不假过滤）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from ..timing import binary_kind_tag, project_relative, store_key
from .identity import COLD_SCHEME

SCHEMA_COVERAGE = "see1420-coverage/1"
# SEE-1587：同轮插桩行事实投影条目。/1 = 手动 measure coverage 产物；
# /2 = attribution 行事实投影产物。两态互不冒充（读侧按 schema 分流）。
SCHEMA_COVERAGE_PROJECTION = "see1420-coverage/2"
PROJECTION_SOURCE = "attribution_line_facts"

COVERAGE_CACHE_DIRNAME = "gqt-coverage"


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
    """coverage 成功 run 的行级事实落盘（原子写；失败告警一次，best-effort）。

    instrumentable_lines（D3）：本次 run 的探针可插桩行集——mutation 前置
    过滤的唯一合法跳过锚。None = 旧 schema 形态（读回侧按无该集降级）。
    identity（SEE-1550 L1）：缓存身份包入档——读回侧要求 fresh ∧ 身份包全
    匹配才允许 prefilter 生效。
    projection（SEE-1587）：非 None 时本条目是 attribution 行事实投影，
    schema 升为 see1420-coverage/2 并携带绑定包——与手动 measure coverage
    条目（/1）互不冒充。
    """
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
        "schema": (SCHEMA_COVERAGE_PROJECTION if projection is not None
                   else SCHEMA_COVERAGE),
        "rel": rel,
        "tests_glob": tests_glob,
        "binary_kind": kind,
        "source_sha1": source_sha1,
        "total_lines": int(total_lines),
        "covered_lines": sorted(int(l) for l in covered_lines),
        "coverage_percent": coverage_percent,
        "recorded_at": time.time(),
    }
    if projection is not None:
        doc["projection"] = dict(projection)
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


def load_reusable_coverage(project_root: str, file_path: str,
                           tests_glob: str | None, binary_kind=None,
                           identity: dict | None = None
                           ) -> tuple[set[int] | None, str | None]:
    """读回可复用的 covered 行集；provenance 不符 = (None, 原因)。

    源码 sha1 在读回时对当前文件实测（落盘后源码变更即失效——行级事实
    必须锚定同一份源码）。vacuous（零可执行行）按无数据降级——过滤无意义。
    identity（SEE-1550 L1）：给出时要求条目身份包全匹配——任一漂移（含
    旧 schema 无 identity）= (None, 漂移原因)，prefilter 禁用并披露。
    """
    lines, _doc_or_reason = _load_coverage_doc(project_root, file_path,
                                               tests_glob, binary_kind,
                                               identity=identity)
    if lines is None:
        return None, _doc_or_reason
    return lines, None


def load_instrumentable_lines(project_root: str, file_path: str,
                              tests_glob: str | None, binary_kind=None,
                              identity: dict | None = None
                              ) -> tuple[set[int] | None, str | None]:
    """D3：读回可插桩行集（provenance 同 load_reusable_coverage）。

    旧 schema（无 instrumentable_lines 键）返回 None——调用方按
    「整文件视为可插桩」的既有语义降级（不假阴性也不扩面）。
    """
    _, doc_or_reason = _load_coverage_doc(project_root, file_path, tests_glob,
                                          binary_kind, identity=identity)
    if isinstance(doc_or_reason, str):
        return None, doc_or_reason
    raw = doc_or_reason
    inst = raw.get("instrumentable_lines")
    if not isinstance(inst, list):
        return None, "coverage cache lacks instrumentable_lines (old schema)"
    return {int(l) for l in inst if isinstance(l, (int, float))}, None


def _load_coverage_doc(project_root: str, file_path: str,
                       tests_glob: str | None, binary_kind=None,
                       identity: dict | None = None
                       ) -> tuple[set[int] | None, dict | str | None]:
    """provenance 校验 + (covered_lines, doc-or-reason) 复合读取。"""
    kind = binary_kind_tag(binary_kind)
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no cached coverage for this (file, tests_glob)"
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA_COVERAGE:
        return None, "coverage cache schema mismatch"
    if raw.get("rel") != project_relative(project_root, file_path):
        return None, "coverage cache path mismatch"
    if raw.get("tests_glob") != tests_glob:
        return None, "coverage cache tests_glob mismatch (exact-match rule)"
    if raw.get("binary_kind") != kind:
        return None, "coverage cache binary kind mismatch"
    if identity is not None and raw.get("identity") != coverage_identity(
            identity):
        return None, ("coverage cache identity drift (fresh + identity-pack "
                      "match required — store invalidated)")
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        return None, f"cannot read target file: {e}"
    if raw.get("source_sha1") != source_sha1:
        return None, "coverage cache stale (source changed since measurement)"
    lines = raw.get("covered_lines")
    if not isinstance(lines, list):
        return None, "coverage cache malformed (covered_lines missing)"
    return ({int(l) for l in lines if isinstance(l, (int, float))}, raw)


# ---------------------------------------------------------------- SEE-1587
# 同轮插桩行事实投影：test-attribution/6 行事实 → (file, tests_glob) 条目


def tests_face_digest(tests_face: dict) -> str:
    """测试面摘要（投影绑定包成分）：排序稳定 JSON sha256。"""
    canon = {str(k): str(v) for k, v in sorted((tests_face or {}).items())}
    return hashlib.sha256(json.dumps(
        canon, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def current_tests_face(project_root: str | os.PathLike) -> dict[str, str]:
    """tests/ 树全部 .gd 的 rel→sha256 现算（与 truth_select 同一口径）。"""
    root = Path(project_root)
    out: dict[str, str] = {}
    tests = root / "tests"
    if tests.is_dir():
        for p in sorted(tests.rglob("*.gd")):
            try:
                out[str(p.relative_to(root))] = hashlib.sha256(
                    p.read_bytes()).hexdigest()
            except OSError:
                continue
    return out


def _projection_binding(doc: dict, rel: str) -> dict | None:
    """投影绑定包：map schema / recipe pristine / 文件 sha / 测试面摘要。
    doc 缺行级主真值前置（schema 非 /6 或绑定缺失）→ None（拒绝投影）。"""
    binding = {
        "source": PROJECTION_SOURCE,
        "map_schema": doc.get("schema_version"),
        "map_recipe_pristine": doc.get("recipe_hash_pristine"),
        "map_file_sha256": (doc.get("file_sha256") or {}).get(rel),
        "map_tests_face_sha": tests_face_digest(doc.get("tests_face") or {}),
    }
    if not binding["map_recipe_pristine"] or not binding["map_file_sha256"]:
        return None
    return binding


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
    from ..measurement.orchestrator import ATTRIBUTION_SCHEMA
    from .truth_select import arms_valid

    root = Path(project_root).resolve()
    if doc.get("schema_version") != ATTRIBUTION_SCHEMA:
        return None, (f"attribution map schema "
                      f"{doc.get('schema_version')!r} != "
                      f"{ATTRIBUTION_SCHEMA!r}")
    if not arms_valid(doc.get("arms_state") or {}):
        return None, "attribution map arms invalid"
    rel = project_relative(str(root), str(Path(target_file).resolve()))
    try:
        file_sha = hashlib.sha256(
            (root / rel).read_bytes()).hexdigest()
    except OSError as e:
        return None, f"cannot read target file: {e}"
    if (doc.get("file_sha256") or {}).get(rel) != file_sha:
        return None, ("attribution map file binding stale "
                      "(target changed since measurement)")
    if not current_recipe_pristine:
        return None, "current recipe hash unavailable"
    if doc.get("recipe_hash_pristine") != current_recipe_pristine:
        return None, "attribution map recipe drift"
    face_now = tests_face if tests_face is not None else current_tests_face(
        str(root))
    if tests_face_digest(doc.get("tests_face") or {}) != tests_face_digest(
            face_now):
        return None, "tests face drift since measurement"
    instrumentable = (doc.get("instrumentable_lines") or {}).get(rel)
    if not isinstance(instrumentable, list) or not instrumentable:
        return None, "attribution map lacks instrumentable surface for file"
    arm = (doc.get("arms") or {}).get("gut_full") or {}
    script_names = {Path(s).name for s in scripts}
    covered: set[int] = set()
    for ident, by_rel in (arm.get("test_to_lines") or {}).items():
        if ident.split("::", 1)[0] not in script_names:
            continue
        covered.update(int(l) for l in (by_rel.get(rel) or ()))
    for script in script_names:
        covered.update(int(l)
                       for l in ((arm.get("script_to_lines") or {})
                                 .get(script, {}) or {}).get(rel, ()) or ())
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


def load_projected_coverage(
        project_root: str, file_path: str, tests_glob: str,
        binary_kind=None, identity: dict | None = None, *,
        current_recipe_pristine: str | None = None,
        tests_face: dict | None = None,
        ) -> tuple[set[int] | None, str | None]:
    """读回投影条目（see1420-coverage/2 专属；SEE-1587 消费面入口）。

    - /1（手动 measure coverage 条目）→ (None, 原因)——投影消费面不用
      手动条目，两态互不冒充；
    - /2 但基础 provenance（rel/glob/kind/身份包/源码 sha1）或绑定包与
      当前树任一失配 → (None, 显式失效原因)——显式失效 = 消费侧回退
      不过滤并可再投影，绝不假过滤。"""
    kind = binary_kind_tag(binary_kind)
    path = coverage_cache_path(project_root, file_path, tests_glob,
                               binary_kind=kind)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no projected coverage for this (file, tests_glob)"
    if not isinstance(raw, dict):
        return None, "coverage cache malformed"
    if raw.get("schema") == SCHEMA_COVERAGE:
        return None, ("coverage cache is a manual measurement entry "
                      "(see1420-coverage/1), not a projection")
    if raw.get("schema") != SCHEMA_COVERAGE_PROJECTION:
        return None, "coverage cache schema mismatch"
    if raw.get("rel") != project_relative(project_root, file_path):
        return None, "coverage cache path mismatch"
    if raw.get("tests_glob") != tests_glob:
        return None, "coverage cache tests_glob mismatch (exact-match rule)"
    if raw.get("binary_kind") != kind:
        return None, "coverage cache binary kind mismatch"
    if identity is not None and raw.get("identity") != coverage_identity(
            identity):
        return None, ("coverage cache identity drift (fresh + identity-pack "
                      "match required — store invalidated)")
    try:
        source_sha1 = hashlib.sha1(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        return None, f"cannot read target file: {e}"
    if raw.get("source_sha1") != source_sha1:
        return None, "coverage cache stale (source changed since measurement)"
    binding = raw.get("projection")
    if not isinstance(binding, dict) \
            or binding.get("source") != PROJECTION_SOURCE:
        return None, "coverage cache lacks projection binding"
    try:
        file_sha = hashlib.sha256(
            Path(file_path).read_bytes()).hexdigest()
    except OSError as e:
        return None, f"cannot read target file: {e}"
    if binding.get("map_file_sha256") != file_sha:
        return None, "projection binding stale (target changed since map)"
    if not current_recipe_pristine:
        return None, "current recipe hash unavailable"
    if binding.get("map_recipe_pristine") != current_recipe_pristine:
        return None, "projection binding stale (recipe drift)"
    root = Path(project_root).resolve()
    face_now = tests_face if tests_face is not None else current_tests_face(
        str(root))
    if binding.get("map_tests_face_sha") != tests_face_digest(face_now):
        return None, "projection binding stale (tests face drift)"
    lines = raw.get("covered_lines")
    if not isinstance(lines, list):
        return None, "coverage cache malformed (covered_lines missing)"
    return ({int(l) for l in lines if isinstance(l, (int, float))}, None)
