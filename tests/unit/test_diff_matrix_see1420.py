"""§SPEC-1420-06：差分跳过规则矩阵（定案 D4，表驱动逐格断言）。

未改+killed→skip；未改+survived→永远重跑；timeout/timeout_pinned→同
survived 永远重跑；scope 改写→该 scope 全跑；header 变更→全文件全跑；
hash 全同→零执行速断（module 速断）。附 §04 的全签名校验降级断言。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from godot_qa_toolkit.mutation.diffengine import plan_diff, signature_matches
from godot_qa_toolkit.mutation.ops import _collect_mutations
from godot_qa_toolkit.mutation.scopemanifest import build_manifest

SRC = ("func add(a, b):\n"
       "\treturn a + b\n")

MANIFEST = {"header_hash": "H", "scopes": {"func:add": "S1"}}
PRIOR = {"verdict": "killed", "reason": "r", "scope_hash": "S1",
         "line": 2, "column": 11, "kind": "AOR", "original": "+",
         "mutated": "-", "timeout_streak": 0}


def _mutants():
    _, _, tagged, _ = build_manifest(SRC, "x.gd", _collect_mutations(SRC, "x.gd"))
    return tagged


def _first(mutants):
    return mutants[0]


class TestDiffMatrix:
    def _plan(self, *, old=MANIFEST, scopes=None, header="H", prior=None,
              mutate_all=False):
        mutants = _mutants()
        return plan_diff(header, scopes or {"func:add": "S1"}, old, mutants,
                         prior if prior is not None else
                         {_first(mutants).stable_id: dict(PRIOR)},
                         mutate_all=mutate_all)

    def test_unchanged_killed_skips(self):
        plan = self._plan()
        assert _first(_mutants()).stable_id in plan.reused
        assert plan.triggers[_first(_mutants()).stable_id] == "reused:killed"

    def test_unchanged_survived_always_retries(self):
        prior = {**PRIOR, "verdict": "survived"}
        plan = self._plan(prior={_first(_mutants()).stable_id: prior})
        sid = _first(_mutants()).stable_id
        assert sid not in plan.reused
        assert plan.triggers[sid] == "retry:survived"

    def test_timeout_always_retries(self):
        """timeout 重跑是 streak 递增的唯一通道（D5）。"""
        prior = {**PRIOR, "verdict": "timeout", "timeout_streak": 1}
        plan = self._plan(prior={_first(_mutants()).stable_id: prior})
        assert plan.triggers[_first(_mutants()).stable_id] == "retry:timeout"

    def test_timeout_pinned_always_retries(self):
        """pinned 永远重跑——防解除路径失效变永久毒条目（Harti S2'）。"""
        prior = {**PRIOR, "verdict": "timeout_pinned", "timeout_streak": 2}
        plan = self._plan(prior={_first(_mutants()).stable_id: prior})
        assert plan.triggers[_first(_mutants()).stable_id] == \
            "retry:timeout_pinned"

    def test_scope_change_runs_whole_scope(self):
        plan = self._plan(scopes={"func:add": "S2"})
        sid = _first(_mutants()).stable_id
        assert plan.triggers[sid] == "scope_changed"
        assert sid not in plan.reused

    def test_new_scope_runs(self):
        plan = self._plan(scopes={"func:add": "S1", "other": "S9"})
        # add 未变 → 复用路径不受新 scope 影响；换全新 scope 判定：
        plan2 = self._plan(scopes={"func:add": "S1"}, old={"header_hash": "H",
                                                           "scopes": {}})
        assert plan2.triggers[_first(_mutants()).stable_id] == "scope_new"

    def test_header_change_runs_whole_file(self):
        plan = self._plan(header="H2")
        sid = _first(_mutants()).stable_id
        assert plan.header_changed is True
        assert plan.triggers[sid] == "header_changed"
        assert sid not in plan.reused

    def test_all_same_zero_execution(self):
        """module 速断：hash 全同 + 全 killed → executed 为空（零执行）。"""
        mutants = _mutants()
        prior = {m.stable_id: {**PRIOR, "line": m.line, "column": m.column,
                               "kind": m.kind, "original": m.original,
                               "mutated": m.mutated} for m in mutants}
        plan = self._plan(prior=prior)
        assert plan.executed == []
        assert len(plan.reused) == len(mutants)

    def test_mutate_all_executes_everything(self):
        plan = self._plan(mutate_all=True)
        assert plan.diff_active is False
        assert len(plan.executed) == len(_mutants())
        assert plan.reused == {}

    def test_no_prior_manifest_full_run(self):
        plan = self._plan(old=None)
        assert plan.diff_active is False
        assert len(plan.executed) == len(_mutants())


class TestSignatureDrift:
    def test_signature_mismatch_refuses_reuse(self):
        """D3：id 仅为寻址提示——全签名校验失败 = 拒绝复用 + 降级重跑。"""
        mutants = _mutants()
        m = _first(mutants)
        prior = {**PRIOR, "line": m.line + 10}  # 行漂移但 id 相同
        plan = plan_diff("H", {"func:add": "S1"}, MANIFEST, mutants, {m.stable_id: prior})
        assert plan.triggers[m.stable_id] == "signature_drift"
        assert m in plan.executed

    def test_scope_hash_drift_refuses_reuse(self):
        """同签名异语义（scope 内容变了但 5 元组未变）→ scope_hash 校验拒绝。"""
        mutants = _mutants()
        m = _first(mutants)
        prior = {**PRIOR, "scope_hash": "STALE"}
        plan = plan_diff("H", {"func:add": "S1"}, MANIFEST, mutants, {m.stable_id: prior})
        assert plan.triggers[m.stable_id] == "scope_hash_drift"

    def test_signature_matches_pure(self):
        m = _first(_mutants())
        entry = {k: getattr(m, k) for k in
                 ("line", "column", "kind", "original", "mutated")}
        assert signature_matches(entry, m) is True
        assert signature_matches({**entry, "mutated": "X"}, m) is False
