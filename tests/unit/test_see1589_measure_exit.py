"""SEE-1589 §SPEC-005（A-M1）：measure run 区分「映射可用、union 未认证」。

判据：
- union 不可认证但 attribution map 可用（arms pairs>0 / probes>0）→
  exit 0、ok=true、`summary.certification=uncertified`、
  `attribution_usable=true`、`attribution_map` 路径在场；不得以失败退出码
  诱导调用方丢弃可用产物；
- 无可用映射且 union 失败 → 仍 exit 1 / ok=false（失败语义不弱化）。
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

import godot_qa_toolkit.cli as cli_mod
from godot_qa_toolkit.cli import build_parser


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "utils").mkdir(parents=True)
    (proj / "utils" / "calc.gd").write_text("func add(a, b):\n\treturn a + b\n")
    recipe = tmp_path / "recipe.json"
    recipe.write_text("{}", encoding="utf-8")
    return proj, recipe


def _map_doc(pairs: int) -> dict:
    return {
        "schema_version": "test-attribution/6",
        "arms_state": {"gut_full": {"state": "invalid", "bundles": 3,
                                    "pairs": pairs, "probes_fired": pairs,
                                    "vacuous": False}},
    }


def _run(tmp_path, monkeypatch, *, ok: bool, pairs: int):
    proj, recipe = _project(tmp_path)
    out = tmp_path / "report.json"
    amap = tmp_path / "map.json"
    amap.write_text(json.dumps(_map_doc(pairs)), encoding="utf-8")
    report = {"report_version": "measurement-report/3", "ok": ok,
              "project_root": str(proj), "arms": []}
    monkeypatch.setattr(cli_mod, "_apply_godot_bin_or_contract",
                        lambda *a, **k: None)
    monkeypatch.setattr(cli_mod, "_godot_bin_ready",
                        lambda *a, **k: ("godot", True, ""))
    from godot_qa_toolkit.measurement import orchestrator as orch
    from godot_qa_toolkit.measurement import recipe as recipe_mod
    monkeypatch.setattr(orch, "run_measurement",
                        lambda *a, **k: dict(report))
    monkeypatch.setattr(recipe_mod, "verify_recipe", lambda *a, **k: (True, ""))
    args = build_parser().parse_args([
        "measure", "run", str(proj / "utils"),
        "--project-root", str(proj), "--recipe", str(recipe),
        "--out", str(out), "--test-attribution",
        "--test-attribution-out", str(amap)])
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = args.func(args)
    return code, json.loads(buf.getvalue()), amap


def test_usable_map_with_uncertified_union_is_not_discarded(tmp_path,
                                                            monkeypatch):
    code, payload, amap = _run(tmp_path, monkeypatch, ok=False, pairs=12)
    assert code == 0, "可用映射不得被失败退出码误导丢弃"
    assert payload["ok"] is True
    summary = payload["summary"]
    assert summary["certification"] == "uncertified"
    assert summary["union_certified"] is False
    assert summary["attribution_usable"] is True
    assert summary["attribution_map"] == str(amap)
    assert payload["failures"] == []


def test_no_usable_map_still_fails(tmp_path, monkeypatch):
    code, payload, _amap = _run(tmp_path, monkeypatch, ok=False, pairs=0)
    assert code == 1
    assert payload["ok"] is False
    assert payload["summary"]["attribution_usable"] is False
    assert payload["summary"]["certification"] == "unusable"


def test_certified_report_stays_ok(tmp_path, monkeypatch):
    code, payload, _amap = _run(tmp_path, monkeypatch, ok=True, pairs=12)
    assert code == 0 and payload["ok"] is True
    assert payload["summary"]["certification"] == "certified"
    assert payload["summary"]["union_certified"] is True
