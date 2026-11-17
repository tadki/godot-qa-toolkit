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
    rel = path.resolve().relative_to(root).as_posix()

    # 映射是唯一真源：缺失时按 mutation 同款路径构建（mode=off 时只读）。
    if truth.mode() != "off":
        truth.ensure_map(str(root), [str(path)])

    try:
        sel = truth.resolve_selection(str(root), str(path))
    except truth.MutationSelectionError as e:  # fail-loud，不回退 GUT
        raise CoverageMappingError(
            getattr(e, "reason", None) or "attribution_selection_unavailable",
            str(e)) from e
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

    scripts = list(sel.get("tests") or [])
    face = tests_glob or "res://tests/"
    if tests_glob:
        scripts = [s for s in scripts if str(s).startswith(tests_glob)]

    bk = binary_kind or detect_binary_kind()
    covered, reason = project_coverage_from_map(
        str(root), doc, str(path), scripts, face,
        binary_kind=bk,
        identity=identity if identity is not None else identity_pack(str(root), bk),
        current_recipe_pristine=truth._fresh_recipe_hash(str(root)),
        tests_face=current_tests_face(str(root)))
    if covered is None:
        raise CoverageMappingError("projection_unavailable", reason)

    inst = {int(l) for l in
            ((doc.get("instrumentable_lines") or {}).get(rel) or [])}
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
