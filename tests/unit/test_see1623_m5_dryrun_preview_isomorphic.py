"""SEE-1623 M5 — dry-run 行预览与真实运行同构行为钉。

根因（SEE-1622 Bachi BUG-10、Harti 障碍9）：dry-run 的 `--lines` 预览
计数未应用行过滤（预览 mutants=全量 vs 真实=选中数），位点清点与真实
卡尺直接矛盾；全部无位点时 dry-run 仍 exit 0（真实运行才显式失败）。

行为契约：预览计数 = 选中位点数（与真实执行同谓词、同错误文本——
selection_preview 单一判定出处）；全无位点 dry-run 显式失败（ok=false，
与真实运行 C3c 同语义）。
"""

from __future__ import annotations

from godot_qa_toolkit.mutation.ops import Mutant
from godot_qa_toolkit.mutation.planner import selection_preview


def _mutants():
    return [Mutant(file="x.gd", line=ln, column=3, operator="+",
                   original="+", mutated="-", kind="AOR", scope_id="f",
                   n=i, stable_id=f"x.gd#f#{i}")
            for i, ln in enumerate((6, 6, 6, 12, 12, 20), start=1)]


class TestSelectionPreview:
    def test_lines_selection_counts_selected_only(self):
        executed, disc = selection_preview(_mutants(), None, [6])
        assert len(executed) == 3
        assert disc["lines_selection"]["hit"] == [6]
        assert disc["lines_selection"]["skipped_no_site"] == []

    def test_lines_mixed_hit_and_skip_disclosed(self):
        executed, disc = selection_preview(_mutants(), None, [6, 7])
        assert len(executed) == 3
        assert disc["lines_selection"]["skipped_no_site"] == [7]

    def test_all_no_site_raises_same_named_error_as_real_run(self):
        import pytest
        with pytest.raises(ValueError) as ei:
            selection_preview(_mutants(), None, [999999])
        assert "matched no mutation sites" in str(ei.value)

    def test_stale_id_raises_same_named_error_as_real_run(self):
        import pytest
        with pytest.raises(ValueError) as ei:
            selection_preview(_mutants(), ["x.gd#f#999"], None)
        assert "matched no mutation sites" in str(ei.value)


class TestDryRunEstimateIsomorphic:
    def test_dry_run_estimate_uses_selected_mutants(self, tmp_path):
        """预览计数 = 选中位点数（真实执行 mutants=3 的同构预览）。"""
        from godot_qa_toolkit.mutation import runner as runner_mod

        src = tmp_path / "calc.gd"
        src.write_text("extends RefCounted\n\n"
                       "func f(a, b):\n"
                       "\treturn a + b\n\n"
                       "func g(a, b):\n"
                       "\treturn a - b\n")
        r = runner_mod.run_mutation(
            str(src), str(tmp_path), dry_run=True, select_lines=[4],
            select_mode="full")
        s = r["summary"]
        assert r["ok"] is True
        # 该源 L4 恰 3 个 AOR 位点（另一函数 L7 的 3 个被行过滤排除——
        # 此前预览计数用全量 6，与真实卡尺矛盾）
        assert s["mutants"] == 3
        assert s["lines_selection"]["hit"] == [4]
        assert s["executed_estimate"] == 3

    def test_dry_run_all_no_site_named_failure(self, tmp_path):
        """全无位点 dry-run 显式失败（此前 exit 0 假成功）。"""
        from godot_qa_toolkit.mutation import runner as runner_mod

        src = tmp_path / "calc.gd"
        src.write_text("extends RefCounted\n\n"
                       "func f(a, b):\n"
                       "\treturn a + b\n")
        r = runner_mod.run_mutation(
            str(src), str(tmp_path), dry_run=True, select_lines=[999],
            select_mode="full")
        assert r["ok"] is False
        assert "matched no mutation sites" in r["summary"]["error"]
        assert r["failures"]
