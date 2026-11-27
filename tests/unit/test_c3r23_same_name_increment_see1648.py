"""SEE-1648 C3-R23 V1：同名碰撞 + 增量只重跑其一的判定表守恒。

§SPEC-030/025：两个目录同名脚本同名测试（一绿一红）时，增量腿按
basename `-gselect` 重跑，执行账与判定重算此前按**名字并集**归因——
重跑腿里 b/ 的失败会把 a/ 的绿测一并标红（绿杀手被 skip-red 误跳过，
假 survive 方向）；若重跑腿只执行了其中一个物理脚本，另一个的判定
可能被对方的执行账改写。回归钉住：

- 执行账按**脚本头证据**（GUT 输出 `res://…` 头 + parse_failures_with_scripts）
  逐脚本归因——绿测不随同名红测误标红；
- 重跑腿未执行的物理脚本：基线判定**原样继承**（不被对方执行账改写）；
- 输出无脚本头（旧形态/截断只剩摘要）→ 保守名集合并集（不放宽既有
  保守方向）；
- 真机（真实 Godot）同名夹具建图 → 只改其一触发增量 → 两个身份各自
  保留判定与边、map 源基线红集只含真红（skip-red 只命中真红）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

A_IDENT = "res://tests/a/test_green.gd::test_sign_positive"
B_IDENT = "res://tests/b/test_green.gd::test_sign_positive"

# 真实 GUT 运行流形态：两脚本各自的头 + 失败标记 + Run Summary 段
OUT_BOTH_STREAMS = (
    "Godot Engine v4.6.2.stable.official.\n"
    "res://tests/a/test_green.gd\n"
    "* test_sign_positive\n"
    "res://tests/b/test_green.gd\n"
    "* test_sign_positive\n"
    "[Failed] test_sign_positive\n"
    "\n"
    "Run Summary\n"
    "- test_sign_positive\n"
    "[Failed] test_sign_positive\n"
)


def _baseline_doc() -> dict:
    return {
        "test_verdicts": {
            A_IDENT: {"verdict": "green"},
            B_IDENT: {"verdict": "red", "classification": "pre_existing",
                      "script": "res://tests/b/test_green.gd"},
        },
    }


def _tests_face() -> dict:
    import hashlib

    def sha(text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()

    return {"tests/a/test_green.gd": sha("extends GutTest\n"),
            "tests/b/test_green.gd": sha("extends GutTest\n")}


def _merge(baseline: dict, executions: list[dict],
           rerun: set[str]) -> dict:
    from godot_qa_toolkit.measurement.increment import _merged_test_verdicts

    merged = _merged_test_verdicts(
        baseline, executions, rerun, set(), set(), None, set(), set(),
        tests_face=_tests_face(),
        arm_identities=[A_IDENT, B_IDENT],
        collisions=[])
    assert merged is not None
    return merged


# ─────────── 单元：执行账按脚本头逐脚本归因 ───────────
def test_verdict_merge_same_name_attributed_per_script():
    """重跑腿一次 -gselect 跑了两个同名脚本（输出含两个脚本头）：
    a/ 通过、b/ 失败 → a/ 保持 green、b/ 保持 red（0d14f98 上按名字
    并集归因 → a/ 被误标红，本测为红）。"""
    merged = _merge(_baseline_doc(),
                    [{"script": "test_green.gd", "rc": 1,
                      "out": OUT_BOTH_STREAMS}],
                    {"test_green.gd"})
    assert merged[A_IDENT]["verdict"] == "green", \
        {k: v.get("verdict") for k, v in merged.items()}
    assert merged[B_IDENT]["verdict"] == "red", \
        {k: v.get("verdict") for k, v in merged.items()}
    assert merged[B_IDENT]["classification"] == "pre_existing"
    assert merged[B_IDENT]["script"] == "res://tests/b/test_green.gd"


def test_verdict_merge_same_name_reverse_failures():
    """反向：a/ 失败、b/ 通过（脚本头互换）→ a/ red/new、b/ green。"""
    out = (
        "res://tests/a/test_green.gd\n"
        "* test_sign_positive\n"
        "[Failed] test_sign_positive\n"
        "res://tests/b/test_green.gd\n"
        "* test_sign_positive\n"
        "\n"
        "Run Summary\n"
        "- test_sign_positive\n"
        "[Failed] test_sign_positive\n"
    )
    merged = _merge(_baseline_doc(),
                    [{"script": "test_green.gd", "rc": 1, "out": out}],
                    {"test_green.gd"})
    assert merged[A_IDENT]["verdict"] == "red", \
        {k: v.get("verdict") for k, v in merged.items()}
    assert merged[B_IDENT]["verdict"] == "green", \
        {k: v.get("verdict") for k, v in merged.items()}


def test_verdict_merge_nonexecuted_same_name_inherits_baseline():
    """重跑腿只执行了其中一个物理脚本（输出只有 a/ 的头）：b/ 未执行
    → 基线判定原样继承（红不被 a/ 的执行账洗白；0d14f98 上 b/ 会被
    a/ 的账重算 → 本测为红）。"""
    out_only_a = (
        "res://tests/a/test_green.gd\n"
        "* test_sign_positive\n"
        "\n"
        "Run Summary\n"
    )
    merged = _merge(_baseline_doc(),
                    [{"script": "test_green.gd", "rc": 0,
                      "out": out_only_a}],
                    {"test_green.gd"})
    assert merged[B_IDENT]["verdict"] == "red", \
        {k: v.get("verdict") for k, v in merged.items()}
    assert merged[A_IDENT]["verdict"] == "green", \
        {k: v.get("verdict") for k, v in merged.items()}


def test_verdict_merge_no_script_headers_conservative_union():
    """输出无脚本头（旧形态/截断只剩摘要）→ 保守名集合并集：红保持红、
    绿误标红方向与既有行为一致（不放宽既有保守方向）。"""
    out_summary_only = (
        "Run Summary\n"
        "- test_sign_positive\n"
        "[Failed] test_sign_positive\n"
    )
    merged = _merge(_baseline_doc(),
                    [{"script": "test_green.gd", "rc": 1,
                      "out": out_summary_only}],
                    {"test_green.gd"})
    assert merged[B_IDENT]["verdict"] == "red"
    # 保守方向：无法归因 → 名并集（绿测一并标红与 0d14f98 行为一致）
    assert merged[A_IDENT]["verdict"] == "red", \
        {k: v.get("verdict") for k, v in merged.items()}


def test_verdict_add_new_identities_same_name_concrete_only():
    """Case A（执行账失败名不在宇宙，arm_identities 空）：有脚本头证据
    时按 (script, name) 具体归因——只给真正失败的 b/ 建 canonical red
    身份，a/ 不被凭空建红/建绿。"""
    import godot_qa_toolkit.measurement.increment as inc

    merged: dict = {}
    per_script = inc._verdict_per_script(
        [{"script": "test_green.gd", "rc": 1, "out": OUT_BOTH_STREAMS}])
    inc._verdict_add_new_identities(
        merged, per_script, {"test_green.gd"}, _tests_face(),
        [])
    assert merged.get(B_IDENT, {}).get("verdict") == "red", merged
    assert merged.get(B_IDENT, {}).get("script") == \
        "res://tests/b/test_green.gd", merged
    assert A_IDENT not in merged, \
        ("未失败的 a/ 不得凭空建红", merged.get(A_IDENT))


def test_verdict_add_new_identities_universe_per_script():
    """宇宙新身份补表（arm_identities 在场）：同名两身份按各自脚本执行
    账判定——a/ green、b/ red 且 script 字段为 b/ 完整路径。"""
    import godot_qa_toolkit.measurement.increment as inc

    merged: dict = {}
    per_script = inc._verdict_per_script(
        [{"script": "test_green.gd", "rc": 1, "out": OUT_BOTH_STREAMS}])
    inc._verdict_add_new_identities(
        merged, per_script, {"test_green.gd"}, _tests_face(),
        [A_IDENT, B_IDENT])
    assert merged.get(A_IDENT, {}).get("verdict") == "green", merged
    assert merged.get(B_IDENT, {}).get("verdict") == "red", merged
    assert merged.get(B_IDENT, {}).get("script") == \
        "res://tests/b/test_green.gd", merged


# ─────────── arm 边合并：同名两身份的边在重跑后保留 ───────────
def test_merge_arm_same_name_edges_replaced_by_fresh_both():
    """fresh 腿带两个同名身份（-gselect 两个物理脚本都执行）→ 两个身份
    的边都由 fresh 替换、不丢边（守恒）。"""
    from godot_qa_toolkit.measurement.increment import _merge_arm

    line = {"calc_sign.gd": [3, 4]}
    arm = {"test_to_lines": {A_IDENT: dict(line), B_IDENT: dict(line)},
           "script_to_lines": {}}
    fresh = {"test_to_files": {A_IDENT: ["calc_sign.gd"],
                               B_IDENT: ["calc_sign.gd"]},
             "script_to_files": {},
             "test_to_lines": {A_IDENT: dict(line), B_IDENT: dict(line)},
             "script_to_lines": {}}
    merged = _merge_arm(arm, fresh, set())
    got = merged.get("test_to_lines") or {}
    assert A_IDENT in got and B_IDENT in got, sorted(got)


# ─────────── 真机：同名夹具建图 → 增量 → 判定表守恒 ───────────
FORMS_TEMPLATE = Path(__file__).parent.parent / "fixtures" / "see1550_forms"


def _real_godot_bin() -> "str | None":
    import os
    import shutil
    import subprocess as _sp

    cand = os.environ.get("GQT_GODOT_BIN") or shutil.which("godot")
    if not cand or not Path(cand).is_file():
        return None
    try:
        r = _sp.run([str(cand), "--version"], capture_output=True,
                    text=True, timeout=60)
    except (OSError, _sp.TimeoutExpired):
        return None
    if "stub" in (r.stdout or ""):
        return None
    return str(cand)


def _gut_addon_src() -> "Path | None":
    import os

    for cand in (os.environ.get("GQT_FORMS_GUT_DIR", ""),
                 "/mnt/d/GodotProjects/king-of-likes/addons/gut",
                 str(Path.home() / "tools" / "KingOfLikes-Godot" / "addons"
                     / "gut")):
        p = Path(cand) if cand else None
        if p is not None and (p / "gut_cmdln.gd").is_file():
            return p
    return None


def _build_same_name_project(tmp_path: Path) -> Path:
    """真实 Godot 项目：tests/a 与 tests/b 同 basename 同名测试，a/ 绿、
    b/ 红（断言反转），生产 calc_sign.gd；另加 30 个 filler 产品文件 +
    各自触达的测试——把 rerun 面稀释到 5% 阈值以下，增量腿必须真实走
    incremental（否则 rerun_surface_too_large_fallback 的全量回退会把
    V1 的增量路径掩盖成全量、缺陷不显现）。"""
    import shutil
    import sys as _sys

    gut_src = _gut_addon_src()
    if gut_src is None:
        pytest.skip("real GUT addon unavailable")
    root = tmp_path / "proj"
    shutil.copytree(FORMS_TEMPLATE, root)
    shutil.copytree(gut_src, root / "addons" / "gut")
    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    install_recipe(root)
    ensure_addon_metadata(root)
    include = [{"kind": "file", "value": "calc_sign.gd"}]
    (root / "calc_sign.gd").write_text(
        "extends RefCounted\nfunc sign(x: int) -> int:\n"
        "\tif x > 0:\n\t\treturn 1\n\treturn -1\n", encoding="utf-8")
    (root / "tests" / "a").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "b").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "fill").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "a" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), 1)\n",
        encoding="utf-8")
    (root / "tests" / "b" / "test_green.gd").write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), -1)\n",
        encoding="utf-8")
    for i in range(30):
        name = f"calc_{i:02d}"
        (root / f"{name}.gd").write_text(
            f"extends RefCounted\nfunc add(a: int) -> int:\n"
            f"\treturn a + {i}\n", encoding="utf-8")
        include.append({"kind": "file", "value": f"{name}.gd"})
        (root / "tests" / "fill" / f"test_{name}.gd").write_text(
            f'extends GutTest\nconst C = preload("res://{name}.gd")\n'
            f"func test_add_{i:02d}():\n"
            f"\tassert_eq(C.new().add(1), {i + 1})\n", encoding="utf-8")
    (root / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": include}}),
        encoding="utf-8")
    return root


def _build_map(root: Path, monkeypatch) -> dict:
    import subprocess as sp

    real_godot = _real_godot_bin()
    if real_godot is None:
        pytest.skip("real godot binary unavailable")
    monkeypatch.setenv("GQT_GODOT_BIN", real_godot)
    monkeypatch.setenv("GQT_ATTRIBUTION_MAP_MODE", "auto")
    monkeypatch.setenv("GQT_AUTO_IMPORT", "1")
    sp.run([real_godot, "--headless", "--import", "--path", str(root)],
           capture_output=True, timeout=550)
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True
    return json.loads((root / ".qa-cache" / "attribution-map.json")
                      .read_text(encoding="utf-8"))


def test_same_name_increment_keeps_verdicts_and_edges(tmp_path,
                                                      monkeypatch):
    """真机全链：建图 → 只改 a/ 脚本（注释）触发增量 → 两个身份判定
    各自保留（a 绿 / b 红）、arm 双边在场、map 源基线红集只含真红
    （skip-red 只命中真红、绿杀手不被误跳过）。"""
    root = _build_same_name_project(tmp_path)
    _build_map(root, monkeypatch)
    a_script = root / "tests" / "a" / "test_green.gd"
    a_script.write_text(
        'extends GutTest\nconst C = preload("res://calc_sign.gd")\n'
        "# touched: trigger test_changed increment\n"
        "func test_sign_positive():\n\tassert_eq(C.new().sign(5), 1)\n",
        encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import refresh

    out = refresh(str(root), ["calc_sign.gd"], timeout_s=500)
    assert out.get("fresh") is True
    doc = json.loads((root / ".qa-cache" / "attribution-map.json")
                     .read_text(encoding="utf-8"))
    # ⓪ 增量路径必须真实走过 incremental（全量回退会掩盖 V1 的增量
    # 语义——rerun 面已用 filler 稀释到阈值以下）
    inc_meta = doc.get("increment") or {}
    assert inc_meta.get("mode") == "incremental", inc_meta
    assert inc_meta.get("rerun_scripts") == ["test_green.gd"], inc_meta
    table = doc.get("test_verdicts") or {}
    # ① 判定守恒：a/ 绿（未被 b/ 的失败污染）、b/ 红（判定不丢）
    assert table.get(A_IDENT, {}).get("verdict") == "green", \
        {k: v.get("verdict") for k, v in table.items()}
    assert table.get(B_IDENT, {}).get("verdict") == "red", \
        {k: v.get("verdict") for k, v in table.items()}
    # ② arm 双边在场（同名两身份的行事实都保留）
    t2l = ((doc.get("arms") or {}).get("gut_full") or {}) \
        .get("test_to_lines") or {}
    assert A_IDENT in t2l and B_IDENT in t2l, sorted(t2l)
    assert (t2l.get(A_IDENT) or {}).get("calc_sign.gd"), t2l
    assert (t2l.get(B_IDENT) or {}).get("calc_sign.gd"), t2l
    # ③ map 源基线红集只含真红（skip-red 只命中真红；绿杀手留在 face）
    from godot_qa_toolkit.mutation.verdict_source import map_baseline_snapshots

    baselines, _failures = map_baseline_snapshots(
        ["calc_sign.gd"], str(root), None)
    snap = baselines[None]
    assert snap.get("red_pairs") == \
        [("res://tests/b/test_green.gd", "test_sign_positive")], \
        snap.get("red_pairs")
