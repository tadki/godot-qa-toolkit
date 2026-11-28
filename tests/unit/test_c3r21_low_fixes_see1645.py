"""SEE-1645 C3-R21：QA 退回两条 LOW 的常驻回归。

- LOW ①：`measure scope` 对不存在的项目根具名失败（§SPEC-031——缺
  目录不是「未配置走默认」，不得 ok=true + defaults_used）。
- LOW ②：`status` 因素标签与载荷一致——生产内容改动的漂移只出一条
  content: 因素（载荷 changed:<file>），不再重复出标签语义相悖的
  new_production: 因素（同函数在 content 探针内已被调用一次）。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest


# ───────────────────────────── LOW ① ─────────────────────────────
def test_measure_scope_missing_root_named_failure(tmp_path, capsys):
    """不存在的项目根 → 具名 JSON（ok=false、error 含 not found）、
    exit 1；不得 ok=true + defaults_used。"""
    from godot_qa_toolkit.cli import build_parser

    missing = tmp_path / "no-such-project"
    args = build_parser().parse_args(
        ["measure", "scope", "--project-root", str(missing)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 1
    payload = json.loads(buf.getvalue())
    assert payload.get("ok") is False
    summary = payload.get("summary") or {}
    assert "not found" in str(summary.get("error")), summary
    assert summary.get("defaults_used") is not True


# ───────────────────────────── LOW ② ─────────────────────────────
def test_status_production_content_factor_label_matches_payload(
        tmp_path):
    """生产内容改动：唯一漂移因素标签为 content:（载荷 changed:<file>
    语义一致）；不再重复出现标签相悖的 new_production: 因素。"""
    import hashlib
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).parent))
    from attribution_fixtures import install_recipe
    from gherkin_fixtures import ensure_addon_metadata

    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    install_recipe(proj)
    ensure_addon_metadata(proj)
    (proj / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (proj / "tests" / "unit" / "test_a.gd").write_text(
        "extends GutTest\n", encoding="utf-8")
    (proj / "prod.gd").write_text("extends Node\n", encoding="utf-8")
    (proj / "data").mkdir()
    content = proj / "data" / "table.csv"
    content.write_text("k,v\n1,2\n", encoding="utf-8")
    (proj / ".gqt-scope.json").write_text(json.dumps({
        "production": {"include": [{"kind": "dir", "value": "data"}]}}),
        encoding="utf-8")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    doc = {
        "schema_version": "test-attribution/7",
        "report_recipe_hash": "synthetic",
        "recipe_hash_pristine": "synthetic",
        "project_root": str(proj),
        "identity": "res://script::test_name",
        "causal": {"enabled": False},
        "file_sha256": {"prod.gd": sha(proj / "prod.gd")},
        "target_files": ["prod.gd"],
        "tests_face": {"tests/unit/test_a.gd": sha(
            proj / "tests" / "unit" / "test_a.gd")},
        "production_content_sha256": {"data/table.csv": sha(content)},
        "arms": {"gut_full": {}},
        "arms_state": {"gut_full": {"state": "valid", "bundles": 1,
                                    "pairs": 0, "probes_fired": 1,
                                    "vacuous": False}},
        "test_verdicts": {},
        "feature_sha256": {},
        "notes": [],
    }
    cache = proj / ".qa-cache"
    cache.mkdir()
    (cache / "attribution-map.json").write_text(json.dumps(doc),
                                                encoding="utf-8")
    # 生产内容改动（非 .gd、非新增/删除 → 纯内容修改漂移）
    content.write_text("k,v\n1,2,3\n", encoding="utf-8")
    from godot_qa_toolkit.mutation.map_contract import status

    st = status(str(proj), ["prod.gd"])
    factors = st.get("factors") or []
    drift = [f for f in factors if "production content drift" in f]
    assert drift, factors
    assert all(f.startswith("content:") for f in drift), factors
    assert not any(f.startswith("new_production:") for f in factors), \
        factors
    assert st.get("fresh") is False
