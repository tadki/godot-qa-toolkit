"""SEE-1631 §SPEC-020：mutation 判定基线的单一真源——attribution map 的
逐测试 verdict 表。

`baseline_source()`：`GQT_MUTATION_BASELINE_SOURCE` ∈ {map（默认）, cold}。
map 模式下 `_precompute_baselines` 不再冷启动 GUT——按 map 的 verdict 表
合成与既有快照同形的 baseline（rc/tail/elapsed），全部下游（`_loop_baseline`
复用、`_parse_failing_tests`、`_BASELINE_RED_MAP`、worker baseline_failures、
自适应超时锚）零改动消费。map 缺失/过期/与当前树不一致 → 具名
`BaselineSourceError` fail-loud（不静默退化全脸）；cold 为显式 opt-out。
parity 门（首 mutant 双通道预检）与 re-canary/cold 回退链保持原语义；
map 源下 parity 失配由 runner 升级为具名 fail-loud（见 runner）。
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_BASELINE_SOURCE = "GQT_MUTATION_BASELINE_SOURCE"

_SNAPSHOT_SOURCE = "map"


class BaselineSourceError(Exception):
    """map 源基线不可用（缺失/过期/与当前树不一致）——具名 fail-loud。"""


def baseline_source() -> str:
    raw = os.environ.get(ENV_BASELINE_SOURCE, "map").strip().lower()
    return raw if raw in ("map", "cold") else "map"


def _load_map_doc(project_root: str) -> dict:
    from .truth_select import MutationSelectionError, load_map, map_path

    path = map_path(Path(project_root).resolve())
    if not path:
        raise BaselineSourceError(
            "baseline_source=map: no attribution map at the default path — "
            "build one via `gqt measure run --test-attribution` / "
            "`measure map-update` (fail-loud: silent full-face fallback is "
            "forbidden by §SPEC-020)")
    try:
        return load_map(path)
    except MutationSelectionError as e:
        raise BaselineSourceError(
            f"baseline_source=map: attribution map unusable: {e}") from e


def _assert_fresh(project_root: str, doc: dict, files: list[str]) -> None:
    """map 与当前树一致性门：schema/臂执行证据/recipe pristine/目标文件
    sha/测试面漂移——任一失配即具名 fail-loud。"""
    import hashlib

    from ..measurement.attribution import ATTRIBUTION_SCHEMA
    from .truth_select import _fresh_recipe_hash

    if doc.get("schema_version") != ATTRIBUTION_SCHEMA:
        raise BaselineSourceError(
            f"baseline_source=map: schema {doc.get('schema_version')!r} "
            f"!= {ATTRIBUTION_SCHEMA!r}")
    states = doc.get("arms_state") or {}
    if not states or all(st.get("state") != "valid"
                         for st in states.values()):
        summary = {k: st.get("state") for k, st in states.items()}
        raise BaselineSourceError(
            f"baseline_source=map: no valid measurement arm in map "
            f"(arms_state={summary})")
    pristine = _fresh_recipe_hash(str(Path(project_root).resolve()))
    if pristine is None or pristine != doc.get("recipe_hash_pristine"):
        raise BaselineSourceError(
            "baseline_source=map: recipe_hash_pristine stale vs current "
            "tree — rebuild the map")
    face = doc.get("tests_face") or {}
    drift = []
    for rel, want in face.items():
        p = Path(project_root) / rel
        if not p.is_file():
            drift.append(f"removed:{rel}")
            continue
        have = hashlib.sha256(p.read_bytes()).hexdigest()
        if have != want:
            drift.append(f"changed:{rel}")
    if drift:
        raise BaselineSourceError(
            f"baseline_source=map: tests_face drift — {sorted(drift)[:6]}")
    shas = doc.get("file_sha256") or {}
    for raw in files:
        p = Path(raw)
        rel = str(p.resolve().relative_to(Path(project_root).resolve())) \
            if p.is_absolute() else str(p)
        want = shas.get(rel)
        if want and p.is_file():
            have = hashlib.sha256(p.read_bytes()).hexdigest()
            if have != want:
                raise BaselineSourceError(
                    f"baseline_source=map: target file drift — {rel}")


def _red_entries(doc: dict) -> "dict[str, tuple[str, str]]":
    """mutation 判定只消费可执行测试面（gut_full：`res://` 全路径与测试
    脚本 basename 两种形态；gherkin 身份不可执行、只披露不参与）。"""
    out: dict[str, tuple[str, str]] = {}
    for ident, entry in (doc.get("test_verdicts") or {}).items():
        if str(ident).startswith("gherkin://"):
            continue
        if entry.get("verdict") == "red":
            out[ident] = (entry.get("classification") or "new", ident)
    return out


def _synthesize_tail(red_pairs: "set[tuple[str, str]]") -> str:
    """合成 GUT 输出形态的 tail：`res://script` 头 + `* test` + `[Failed]`
    行——`_parse_failing_tests` / `parse_failures_with_scripts` 逐位可解析
    （下游零改动消费）。"""
    lines: list[str] = []
    by_script: dict[str, list[str]] = {}
    for (script, name) in sorted(red_pairs):
        by_script.setdefault(script, []).append(name)
    for script, names in sorted(by_script.items()):
        lines.append(script)
        for name in names:
            lines.append(f"* {name}")
            lines.append(f"[Failed] {name}")
    lines.append("Run Summary")
    for script, names in sorted(by_script.items()):
        for name in names:
            lines.append(f"- {name}")
            lines.append(f"[Failed] {name}")
    return "\n".join(lines) + "\n"


def map_baseline_snapshots(files: list[str], project_root: str,
                           tests_glob: str | None,
                           select_mode: str = "full") -> "tuple[dict, dict]":
    """map verdict 表 → per-glob 合成基线快照（与 `_run_baseline_snapshot`
    同形：{rc, tail, elapsed}；elapsed=0 → 自适应超时锚回落参数值）。

    返回 (baselines, failures)：baselines 键与既有消费一致（scoped glob /
    None=全量面）；failures 恒空（map 源不产生"尝试失败"——不可用直接
    fail-loud）。tests_glob 过滤：None = 全部 gut 身份；非 None = 脚本
    前缀在 glob 目录内的身份。"""
    doc = _load_map_doc(project_root)
    _assert_fresh(project_root, doc, files)
    reds = _red_entries(doc)
    prefix = None
    selection_dir = False
    if tests_glob:
        base = str(tests_glob)
        if base.startswith("res://"):
            base = base[len("res://"):]
        if ".gqt-attr-select/" in base:
            # 选面目录只承载选中脚本的 wrapper（wrapper 名 = 原脚本名）——
            # 按 basename 对应，不做目录前缀过滤。
            selection_dir = True
        else:
            prefix = base.rstrip("/") + "/"
    face_pairs: set[tuple[str, str]] = set()
    for ident, (cls, _full) in reds.items():
        script = ident.split("::", 1)[0]
        rel = script[len("res://"):] if script.startswith("res://") else script
        if prefix is not None and not rel.startswith(prefix):
            continue
        face_pairs.add((script, ident.split("::", 1)[1]))
    baselines: dict = {}
    tail = _synthesize_tail(face_pairs)
    key = tests_glob if tests_glob is not None else None
    baselines[key] = {"rc": (1 if face_pairs else 0), "tail": tail,
                      "elapsed": 0.0, "source": _SNAPSHOT_SOURCE,
                      "red_names": sorted(n for _s, n in face_pairs),
                      "red_pairs": sorted(face_pairs)}
    return baselines, {}
