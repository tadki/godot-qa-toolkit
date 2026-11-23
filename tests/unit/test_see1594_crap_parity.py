"""SEE-1594 §SPEC-013：CRAP 与 coverage/attribution 投影 parity。

判据（同一 run 内，机器可判）：
- **fid 集**：CRAP records 每文件 fid 集 == union per_fid fid 集（不增不减）；
- **行集 / raw coverage**：scored record 的 covered/total == union per_fid
  行逐值相等（无二次计算漂移、无过时投影）；
- **score/status**：score == crap_line_score(cc, covered/total)＝formula 单点；
  scored ⇔ score 非空且无矛盾（contract 构造期 fail-loud 已护栏）；
  record 与 crap_line 的 coverage_semantics 一致；
- **错误语义**：union invalid → 无 scored 记录、score_status 落具名失败
  车道、comparable=false——不得静默出分。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess as sp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

_CANONICAL = Path(__file__).resolve().parent / "test_canonical_report_see1546.py"


def _load_canonical():
    spec = importlib.util.spec_from_file_location("canonical_fixtures",
                                                  _CANONICAL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_subprocess():
    orig = sp.run
    yield
    sp.run = orig


def _report(tmp_path: Path, fake_cls="FakeGodot"):
    from godot_qa_toolkit.measurement.orchestrator import run_measurement
    t = _load_canonical()
    project = t.project.__wrapped__(tmp_path)
    recipe, _ = t._recipe(project, tmp_path)
    sp.run = getattr(t, fake_cls)(project)
    # SEE-1623 H4：双臂 union 机制测试显式走恢复路径（acceptance 默认
    # 暂停契约下占位臂无 bundles_data，union 交集恒空）。
    env_key = "GQT_MEASUREMENT_GHERKIN_DISABLED"
    old = os.environ.get(env_key)
    os.environ[env_key] = "0"
    try:
        report = run_measurement(project, t._targets(project), recipe)
    finally:
        if old is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = old
    return report


class TestCrapParityScored:
    def test_fid_sets_match_union_per_fid(self, tmp_path):
        """fid 集：CRAP records per file == union per_fid（不增不减）。"""
        report = _report(tmp_path)
        assert report["union"]["valid"] is True
        union_fids: dict[str, set] = {}
        for u in report["unions"]:
            union_fids[u["file"]] = {row["fid"]
                                     for row in u["union"]["per_fid"]}
        crap_fids: dict[str, set] = {}
        for rec in report["crap"]["records"]:
            rel = "res://" + rec["function_id"].split("::", 1)[0]
            crap_fids.setdefault(rel, set()).add(rec["function_id"])
        for rel, fids in union_fids.items():
            assert crap_fids.get(rel) == fids, (
                f"{rel}: CRAP fid 集与 union per_fid 不一致")

    def test_raw_coverage_matches_union_rows(self, tmp_path):
        """行集/raw coverage：scored record 的 covered/total 与 union
        per_fid 逐值相等（唯一来源，无重复计算）。"""
        report = _report(tmp_path)
        rows: dict[tuple[str, str], dict] = {}
        for u in report["unions"]:
            for row in u["union"]["per_fid"]:
                rows[(u["file"], row["fid"])] = row
        checked = 0
        for rec in report["crap"]["records"]:
            if rec["score_status"] != "scored":
                continue
            rel = "res://" + rec["function_id"].split("::", 1)[0]
            row = rows[(rel, rec["function_id"])]
            assert rec["covered"] == int(row["covered"])
            assert rec["total"] == int(row["total"])
            assert rec["crap_line"]["covered"] == rec["covered"]
            assert rec["crap_line"]["total"] == rec["total"]
            checked += 1
        assert checked > 0, "至少一个 scored 记录参与 parity"

    def test_score_is_formula_single_point(self, tmp_path):
        """score/status：score == crap_line_score(cc, covered/total)；
        全仓 crap_line_score 定义唯一（formula 单点）。"""
        from godot_qa_toolkit.risk.formula import crap_line_score
        report = _report(tmp_path)
        for rec in report["crap"]["records"]:
            cl = rec["crap_line"]
            if rec["score_status"] == "scored":
                expect = crap_line_score(
                    rec["cc"], rec["covered"] / rec["total"])
                assert cl["score"] == expect
                assert cl["score_status"] == "scored"
            else:
                assert cl["score"] is None
            assert cl["coverage_semantics"] == rec["coverage_semantics"]
        # formula 单点：def crap_line_score 仅存于 risk/formula.py
        src = Path(__file__).resolve().parents[2] / "src"
        sites = [p for p in src.rglob("*.py")
                 if "def crap_line_score(" in p.read_text(encoding="utf-8")]
        assert len(sites) == 1 and sites[0].name == "formula.py"


class TestCrapParityInvalidSemantics:
    def test_invalid_union_never_scores(self, tmp_path):
        """错误语义：union invalid（新增失败泵）→ 无 scored、具名车道、
        comparable=false——不静默出分。"""
        report = _report(tmp_path, fake_cls="FailingConfigFaceGodot")
        assert report["union"]["valid"] is False
        assert report["crap"]["comparable"] is False
        assert report["crap"]["comparable_reason"] == "new_test_failures"
        for rec in report["crap"]["records"]:
            assert rec["score_status"] != "scored", (
                f"invalid union 仍出分: {rec['function_id']}")
            assert rec["crap_line"]["score"] is None
        lanes = report["crap"]["summary"]
        assert lanes.get("scored", 0) == 0
