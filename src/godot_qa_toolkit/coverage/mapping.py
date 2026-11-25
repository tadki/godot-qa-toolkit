"""coverage 的行级映射唯一真源消费（SEE-1594 §SPEC-025）。

目标：direct `measure coverage` / `measure risk` / mutation prefilter 都消费
**同一**行级归属映射（test-attribution/6），不再有第二套 direct GUT 执行路径。

本模块是唯一的覆盖消费实现，复用既有单一来源链（不复制 projection）：
- `truth_select.ensure_map` —— 缺失时构建映射（映射网是核心部件，需要时跑）；
- `truth_select.resolve_selection` —— 新鲜性判定 + 失效面增量更新 + 无法验证
  即 fail-loud；
- `coverlink.project_coverage_from_map` —— 由行事实投影 covered 行集（零第二
  次插桩、零 GUT）。

无法取得新鲜/可用映射时抛 `CoverageMappingError`（fail-loud，绝不静默回退到
旧 direct GUT）。
"""

from __future__ import annotations

from pathlib import Path

from ..mutation import truth_select as truth
from .instrument import _collect_executable_lines


class CoverageMappingError(RuntimeError):
    """映射不可用 / 投影前置失配（fail-loud，禁止静默回退 direct GUT）。"""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _mapping_ensure(project_root: Path, path: Path, timeout_s: int | None) -> None:
    """映射自动构建（mutation 同款路径；mode=off 只读跳过）。裸名二进制
    不在 PATH → 具名 fail-loud；构建失败转 CoverageMappingError（CLI 恒出
    可解析 JSON，绝不静默回退旧 direct GUT）。"""
    from ..mutation.inject import godot_command
    import shutil as _shutil

    root = Path(project_root).resolve()
    if truth.mode() == "off":
        return
    _cmd = godot_command()
    if "/" not in _cmd and "\\" not in _cmd and _shutil.which(_cmd) is None:
        raise CoverageMappingError(
            "godot_binary_unusable",
            f"godot binary not found in PATH: {_cmd} "
            f"(set GQT_GODOT_BIN or --godot-bin)")
    # §SPEC-032/D1：ensure/续新委托同一契约（refresh；create/increment/
    # full 决策 + 锁内复检 + 装填后校验单源）。refresh 失败且已存在 map
    # 时回退既有 ensure_map（复用/续新语义保留——resolve 侧续新链照旧）；
    # 无 map 时 refresh 失败即具名失败（virgin 构建失败不静默）。
    from ..mutation.map_contract import refresh

    map_file = root / ".qa-cache" / "attribution-map.json"
    try:
        refresh(str(root), [str(path)], timeout_s=timeout_s or 600)
        return
    except Exception as renewal_err:  # noqa: BLE001 — 回退判定见下
        # 已存在 map：refresh 续新失败（环境受限/最小夹具）→ 回退既有
        # ensure_map（复用 no-op；续新由 resolve 侧既有链承担——其余一律
        # 不变）；无 map 的 virgin 构建失败不静默（具名转域错误）。
        if map_file.is_file():
            try:
                truth.ensure_map(str(root), [str(path)],
                                 timeout_s=timeout_s)
                return
            except Exception as e:  # noqa: BLE001
                raise CoverageMappingError(
                    "attribution_map_build_failed",
                    f"{type(e).__name__}: {e}") from e
        raise CoverageMappingError(
            "attribution_map_build_failed",
            f"{type(renewal_err).__name__}: {renewal_err}") from renewal_err


def _mapping_select(root: Path, path: Path, timeout_s: int | None) -> dict:
    """选面决策（fail-loud，不回退 GUT；畸形 arms_state 转具名域错误）。"""
    try:
        sel = truth.resolve_selection(str(root), str(path), timeout_s=timeout_s)
    except truth.MutationSelectionError as e:
        # SEE-1623 M10/F4：detail 用 e.detail（str(e) 已含 reason 前缀，
        # 再拼一次产生 auto_update_still_stale:… 双写）。
        raise CoverageMappingError(
            getattr(e, "reason", None) or "attribution_selection_unavailable",
            getattr(e, "detail", None) or str(e)) from e
    except (TypeError, ValueError, AttributeError) as e:
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"{type(e).__name__}: {e}") from e
    if sel.get("source") != "runtime_mapping":
        raise CoverageMappingError(
            sel.get("reason") or "attribution_selection_unavailable",
            "no fresh line-level mapping for target")
    return sel


def _mapping_doc(root: Path) -> tuple:
    """映射文档装载 + 顶层 instrumentable_lines 形状守卫（投影前验证——
    防止 _projection_map_reject 的 AttributeError 被兜底 except 遮蔽成
    projection_failed，§SPEC-025/027 域命名）。返回 (map_path, doc)。"""
    map_path = truth.map_path(str(root))
    if not map_path:
        raise CoverageMappingError(
            "no_attribution_map",
            "build it first: `gqt measure run --test-attribution`")
    try:
        doc = truth.load_map(map_path)
    except truth.SelectionError as e:
        raise CoverageMappingError("attribution_map_unusable", str(e)) from e
    inst_all = doc.get("instrumentable_lines")
    if inst_all is None:
        inst_all = {}
    if not isinstance(inst_all, dict):
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"instrumentable_lines top-level malformed: "
            f"{type(inst_all).__name__}")
    return map_path, doc


def _mapping_project(root: Path, path: Path, rel: str, doc: dict,
                     scripts: list, tests_glob: "str | None", binary_kind,
                     identity, tests_face) -> set:
    """投影 covered 行集（coverlink 单源）；失败转具名域错误。"""
    from ..mutation.coverlink import project_coverage_from_map

    covered, why = project_coverage_from_map(
        str(root), doc, str(path), scripts, tests_glob,
        binary_kind=binary_kind, identity=identity,
        current_recipe_pristine=(doc.get("recipe_hash_pristine")),
        tests_face=tests_face)
    if covered is None:
        raise CoverageMappingError("projection_failed", why or "unknown")
    return covered


def _mapping_report(path: Path, covered: set, inst: set, scripts: list,
                    tests_glob: "str | None", face: "str | None",
                    map_path, gherkin: "dict | None" = None) -> dict:
    """投影结果聚合（total/pct/契约键集）。SEE-1631：gherkin 臂命中
    披露块（不进 covered 行集——投影保持 gut_full 单源口径）。"""
    total = len(inst)
    cov = len(covered)
    pct = round(100.0 * cov / total, 2) if total else 100.0
    out = {
        "file": str(path), "covered_lines": covered,
        "instrumentable_lines": inst, "total_lines": total,
        "covered_count": cov, "coverage_percent": pct,
        "scripts": scripts, "tests_glob": tests_glob, "face": face,
        "map_path": str(map_path),
        "source": "line_mapping_projection",
    }
    if gherkin is not None:
        out["gherkin"] = gherkin
    return out


def _gherkin_projection_lines(garm: dict, rel: str) -> set:
    """目标文件上的 gherkin 命中行集（test_to_lines ∪ script_to_lines
    双车道并集）。"""
    glines: set[int] = set()
    for _ident, by_rel in (garm.get("test_to_lines") or {}).items():
        glines |= set((by_rel or {}).get(rel) or [])
    for _script, by_rel in (garm.get("script_to_lines") or {}).items():
        glines |= set((by_rel or {}).get(rel) or [])
    return glines


def _gherkin_projection_disclosure(doc: dict, rel: str,
                                   covered: set) -> "dict | None":
    """SEE-1631 §SPEC-005：coverage 投影的 gherkin 命中披露（行集/百分比
    parity 不变；仅披露 gherkin 覆盖行与 gut 投影的差集）。
    SEE-1631 §SPEC-015：gherkin 臂 state!=valid 时不得把其边当命中——
    披露转为具名 unavailable，行集不出场；valid 时披露不变。"""
    garm = (doc.get("arms") or {}).get("gherkin")
    if not isinstance(garm, dict):
        return None
    if not (garm.get("test_to_lines") or garm.get("script_to_lines")):
        return None
    gh_state = ((doc.get("arms_state") or {}).get("gherkin") or {})
    if gh_state.get("state") != "valid":
        return {"unavailable": True,
                "state": gh_state.get("state"),
                "invalid_reason": gh_state.get("invalid_reason"),
                "covered_lines": [], "gherkin_only_lines": [],
                "consumption": ("not disclosed: invalid gherkin arm edges "
                                "are not truth (state=%s)"
                                % (gh_state.get("state"),))}
    glines = _gherkin_projection_lines(garm, rel)
    if not glines:
        return None
    return {"covered_lines": sorted(glines),
            "gherkin_only_lines": sorted(glines - set(covered)),
            "consumption": ("disclosed_only: projection stays gut_full "
                            "(single-source caliber unchanged)")}


def _mapping_target(project_root: Path, file_path: str) -> tuple:
    """目标锚定 + rel 解析 + 源码读入。返回 (path, rel, src)。"""
    from ..source_io import anchor_to_project_root

    root = Path(project_root).resolve()
    path = Path(anchor_to_project_root(file_path, str(root)))
    if not path.is_file():
        raise FileNotFoundError(f"coverage target not found: {file_path}")
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError as e:
        raise CoverageMappingError("target_outside_project", str(e)) from e
    return path, rel, path.read_text(encoding="utf-8")


def filter_selection_face(scripts, tests_glob: str) -> list:
    """SEE-1623 M9：显式 tests 面过滤 + 空选择面哨兵（单一出处）。

    面过滤按边界匹配：目录以 `face/` 为前缀，单文件精确相等。过滤后
    0 脚本 = 空 选择面（glob 写错或映射面不含该面）→ 具名
    CoverageMappingError——绝不静默产出权威外观的 0.0%（F1 缺陷）。"""
    face = tests_glob
    prefix = face if face.endswith("/") else face + "/"

    def _in_face(s: str) -> bool:
        s = str(s)
        return s == face or s.startswith(prefix)

    out = [s for s in scripts if _in_face(s)]
    if not out:
        raise CoverageMappingError(
            f"empty selection face: tests_glob {face!r} matched no "
            "test scripts in the mapping selection — refusing "
            "authoritative 0.0% (named sentinel; check the glob or "
            "omit --tests to cover the mapped face)")
    return out


def project_file_coverage(
    project_root: str | Path,
    file_path: str,
    tests_glob: str | None = None,
    *,
    binary_kind: str | None = None,
    identity: dict | None = None,
    timeout_s: int | None = None,
) -> dict:
    """由行级映射投影单文件覆盖（唯一消费实现，零 GUT/零第二次插桩）。

    返回 {file, covered_lines(set), instrumentable_lines(set), total_lines,
    covered_count, coverage_percent, scripts, tests_glob, face, map_path,
    source}。映射缺失/失效不可自动恢复 → `CoverageMappingError`。"""
    from ..mutation.coverlink import current_tests_face

    root = Path(project_root).resolve()
    path, rel, src = _mapping_target(project_root, file_path)
    if not _collect_executable_lines(src):
        return {"file": str(path), "covered_lines": set(),
                "instrumentable_lines": set(), "total_lines": 0,
                "covered_count": 0, "coverage_percent": 100.0,
                "scripts": [], "tests_glob": tests_glob, "face": None,
                "map_path": None, "source": "vacuous"}
    _mapping_ensure(root, path, timeout_s)
    sel = _mapping_select(root, path, timeout_s)
    map_path, doc = _mapping_doc(root)
    tests_face = current_tests_face(str(root))
    scripts = list(sel.get("tests") or [])
    face = tests_glob or "res://tests/"
    if tests_glob:
        # SEE-1623 M9：空选择面具名哨兵（filter_selection_face 单一出处）。
        scripts = filter_selection_face(scripts, tests_glob)
    covered = _mapping_project(root, path, rel, doc, scripts, tests_glob,
                               binary_kind, identity, tests_face)
    inst = set(doc.get("instrumentable_lines", {}).get(rel, []))
    return _mapping_report(path, covered, inst, scripts, tests_glob, face,
                           map_path,
                           gherkin=_gherkin_projection_disclosure(
                               doc, rel, covered))