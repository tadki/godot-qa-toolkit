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


class CoverageMappingError(RuntimeError):
    """映射不可用 / 投影前置失配（fail-loud，禁止静默回退 direct GUT）。"""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


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
    source}。映射缺失/失效不可自动恢复 → `CoverageMappingError`。
    """
    from ..source_io import anchor_to_project_root
    from ..mutation import truth_select as truth
    from ..mutation.identity import identity_pack
    from ..mutation.inject import detect_binary_kind
    from ..mutation.coverlink import (current_tests_face,
                                       project_coverage_from_map)

    root = Path(project_root).resolve()
    path = Path(anchor_to_project_root(file_path, str(root)))
    if not path.is_file():
        raise FileNotFoundError(f"coverage target not found: {file_path}")
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError as e:
        # 非域异常（root-relative 失败）也必须转具名域错误，不能裸 traceback。
        raise CoverageMappingError(
            "target_outside_project_root",
            f"{path} is not under project root {root}") from e

    # vacuous：无可执行行 → 没有行事实可言，无需映射/无需执行即满分 vacuous
    # （不需要 map，因此不走下方“无法验证新鲜即 fail-loud”路径）。
    from ..source_io import read_gd_source as _read_source
    src, read_error = _read_source(str(path))
    if read_error is None:
        from .runner import _collect_executable_lines
        if not _collect_executable_lines(src):
            return {"file": str(path), "covered_lines": set(),
                    "instrumentable_lines": set(), "total_lines": 0,
                    "covered_count": 0, "coverage_percent": 100.0,
                    "scripts": [], "tests_glob": tests_glob,
                    "face": tests_glob or "res://tests/", "map_path": None,
                    "source": "line_mapping_projection", "vacuous": True}

    # 映射是唯一真源：缺失时按 mutation 同款路径构建（mode=off 时只读）。
    if truth.mode() != "off":
        # 映射构建需要真实 Godot：裸名二进制不在 PATH → 具名 fail-loud
        # （与 spawn_godot 的措辞一致），不进入 measurement 留下备份残留。
        import shutil as _shutil

        from ..mutation.inject import godot_command
        _cmd = godot_command()
        if "/" not in _cmd and "\\" not in _cmd and _shutil.which(_cmd) is None:
            raise CoverageMappingError(
                "godot_binary_unusable",
                f"godot binary not found in PATH: {_cmd} "
                f"(set GQT_GODOT_BIN or --godot-bin)")
        try:
            truth.ensure_map(str(root), [str(path)], timeout_s=timeout_s)
        except Exception as e:  # noqa: BLE001 — 映射构建失败转具名域错误，
            # 保证 CLI 恒出可解析 JSON（无裸 traceback）；坏 Godot/构建失败等
            # 均在此收口，绝不静默回退旧 direct GUT。
            raise CoverageMappingError(
                "attribution_map_build_failed",
                f"{type(e).__name__}: {e}") from e

    try:
        sel = truth.resolve_selection(str(root), str(path), timeout_s=timeout_s)
    except truth.MutationSelectionError as e:  # fail-loud，不回退 GUT
        raise CoverageMappingError(
            getattr(e, "reason", None) or "attribution_selection_unavailable",
            str(e)) from e
    except (TypeError, ValueError, AttributeError) as e:
        # 畸形 arms_state 容器（如 bundles 为 dict）在 truth_select 内裸抛
        # TypeError/ValueError → 转具名域错误（§SPEC-025/027 反例钉）。
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"{type(e).__name__}: {e}") from e
    if sel.get("source") != "runtime_mapping":
        raise CoverageMappingError(
            sel.get("reason") or "attribution_selection_unavailable",
            "no fresh line-level mapping for target")

    map_path = truth.map_path(str(root))
    if not map_path:
        raise CoverageMappingError(
            "no_attribution_map",
            "build it first: `gqt measure run --test-attribution`")
    try:
        doc = truth.load_map(map_path)
    except truth.SelectionError as e:
        raise CoverageMappingError("attribution_map_unusable", str(e)) from e

    # 顶层 instrumentable_lines 必须在投影前验证：_projection_map_reject 会
    # 对 truthy 非 dict 裸抛 AttributeError，再被投影兜底 except 转发成
    # projection_failed，遮蔽本处具名收口（§SPEC-025/027 域命名）。
    inst_all = doc.get("instrumentable_lines")
    if inst_all is None:
        inst_all = {}
    if not isinstance(inst_all, dict):
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"instrumentable_lines top-level malformed: "
            f"{type(inst_all).__name__}")

    scripts = list(sel.get("tests") or [])
    face = tests_glob or "res://tests/"
    if tests_glob:
        # 面过滤按边界匹配：目录必须以 `face/` 为前缀（`res://tests/unit`
        # 不得扩到 `res://tests/unit_compat/`）；单文件精确相等。
        prefix = face if face.endswith("/") else face + "/"

        def _in_face(s: str) -> bool:
            s = str(s)
            return s == face or s.startswith(prefix)

        scripts = [s for s in scripts if _in_face(s)]

    bk = binary_kind or detect_binary_kind()
    try:
        covered, reason = project_coverage_from_map(
            str(root), doc, str(path), scripts, face,
            binary_kind=bk,
            identity=identity if identity is not None else identity_pack(str(root), bk),
            current_recipe_pristine=truth._fresh_recipe_hash(str(root)),
            tests_face=current_tests_face(str(root)))
    except CoverageMappingError:
        raise
    except Exception as e:  # noqa: BLE001 — 畸形 map 的任何异常都转具名域错误
        raise CoverageMappingError(
            "projection_failed",
            f"{type(e).__name__}: {e}") from e
    if covered is None:
        raise CoverageMappingError("projection_unavailable", reason)

    inst_raw = inst_all.get(rel)
    if not isinstance(inst_raw, (list, tuple, set)):
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"instrumentable_lines for {rel} malformed: {inst_raw!r}")
    try:
        inst = {int(l) for l in inst_raw}
    except (TypeError, ValueError) as e:
        raise CoverageMappingError(
            "attribution_map_malformed",
            f"instrumentable_lines for {rel} malformed: {e}") from e
    total = len(inst)
    cov = len(covered & inst) if inst else len(covered)
    pct = round(100.0 * cov / total, 2) if total else 100.0
    return {
        "file": str(path),
        "covered_lines": covered,
        "instrumentable_lines": inst,
        "total_lines": total,
        "covered_count": cov,
        "coverage_percent": pct,
        "scripts": scripts,
        "tests_glob": tests_glob,
        "face": face,
        "map_path": map_path,
        "source": "line_mapping_projection",
    }
