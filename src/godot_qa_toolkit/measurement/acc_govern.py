"""SEE-1615 §SPEC-ACC-GOVERN —— acceptance 对等治理（暂停期间就位）。

Archi 方案 A–D 已批：不照抄 GUT，按 acceptance 自身结构治理——

- 九类 failure_class（逐类可达、具名）：
  scenario_assert_failure / parse_error / scenario_timeout / replay_check /
  snapshot_reconciliation / not_converged / non_deterministic_replay /
  unattributed / no_evidence；
- 红语义四象限：同上下文无插桩 replay 区分「既有红（吸收）」与「新增红
  （阻断）」；replay 间签名不同或红转绿 → non_deterministic_replay，
  **不得豁免**；
- 不耐受身份：`--intolerant-tests` 增 `acceptance_scenarios`（feature +
  scenario + scenario_line / example_row_line），中断报告可生成声明草稿；
- 证据块四态 valid/partial/unavailable/not_applicable（acceptance partial
  ≠ map partial，union 完整性显式降级）；空证据必须先证执行再算收敛
  （防假收敛）。

共享进程分片恢复不在本项（SEE-1619）。
"""

from __future__ import annotations

ACCEPTANCE_FAILURE_CLASSES = (
    "scenario_assert_failure",
    "parse_error",
    "scenario_timeout",
    "replay_check",
    "snapshot_reconciliation",
    "not_converged",
    "non_deterministic_replay",
    "unattributed",
    "no_evidence",
)

_RED_QUADRANTS = ("existing_red", "new_red", "non_deterministic_replay",
                  "green")


def acceptance_identity(feature: str, scenario: str,
                        scenario_line: int | None = None,
                        example_row_line: int | None = None) -> str:
    """(feature, scenario) + 行号身份——acceptance 不耐受声明与失败归属
    的稳定键（场景名可跨 feature 重名，行号区分参数化 example 行）。"""
    base = f"{feature}::{scenario}"
    if scenario_line is not None:
        base += f"@{scenario_line}"
    if example_row_line is not None:
        base += f"#{example_row_line}"
    return base


def classify_acceptance_failure(*, executed: bool = True,
                                parse_ok: bool = True,
                                timed_out: bool = False,
                                replay_failed: bool = False,
                                reconciliation_ok: bool = True,
                                converged: bool | None = True,
                                nondeterministic: bool = False,
                                attributed: bool = True,
                                assert_failed: bool = False) -> str:
    """九类 failure_class 分派（优先级：无证据 → 解析 → 超时 → 重放 →
    对账 → 不收敛 → 非确定 → 未归因 → 断言失败）。

    空证据（executed=False）：先证执行再谈收敛——直接 no_evidence，绝不
    报 converged。"""
    if not executed:
        return "no_evidence"
    if not parse_ok:
        return "parse_error"
    if timed_out:
        return "scenario_timeout"
    if replay_failed:
        return "replay_check"
    if not reconciliation_ok:
        return "snapshot_reconciliation"
    if converged is False:
        return "not_converged"
    if nondeterministic:
        return "non_deterministic_replay"
    if not attributed:
        return "unattributed"
    if assert_failed:
        return "scenario_assert_failure"
    return "no_evidence"


def classify_red_quadrant(identity: str,
                          existing_red_signatures: "dict[str, str]",
                          replay_signatures: "list[str]") -> str:
    """同上下文无插桩 replay 的四象限红分类。

    - replay 签名集为空 → no_evidence（不得当绿学）；
    - 签名在 replay 间不一致 → non_deterministic_replay（不得豁免）；
    - 所有 replay 绿但既有基线红 → non_deterministic_replay（红转绿，
      同样不得豁免——基线不稳不能吸收新增失败）；
    - 全部 replay 红且与既有基线红签名一致 → existing_red（可吸收）；
    - 全部 replay 红但不在基线红集（或签名不同）→ new_red（阻断）。
    """
    if not replay_signatures:
        return "no_evidence"
    if len(set(replay_signatures)) > 1:
        return "non_deterministic_replay"
    sig = replay_signatures[0]
    if sig == "green":
        if identity in existing_red_signatures:
            return "non_deterministic_replay"  # 红转绿：基线不稳，不豁免
        return "green"
    baseline_sig = existing_red_signatures.get(identity)
    if baseline_sig is None:
        return "new_red"
    if baseline_sig != sig:
        return "non_deterministic_replay"  # 签名不同：不可按名吸收
    return "existing_red"


def acceptance_evidence_block(*, executed: bool, converged: bool | None,
                              rounds: int, partial_scenarios: int = 0,
                              attempted: int = 0) -> dict:
    """acceptance 证据块四态。

    - unavailable：未执行（暂停/未跑）——**不判收敛**；
    - not_applicable：无可测场景（attempted=0）；
    - partial：部分场景未收敛/被排除（acceptance partial ≠ map partial，
      union 完整性显式降级 disclosed）；
    - valid：全场景执行且收敛；空证据绝不 valid（防假收敛）。
    """
    if not executed:
        return {"state": "unavailable", "converged": None,
                "reason": "acceptance arm did not execute"}
    if attempted == 0:
        return {"state": "not_applicable", "converged": None,
                "reason": "no acceptance scenarios attempted"}
    if partial_scenarios > 0 or converged is False or rounds <= 0:
        return {"state": "partial", "converged": False,
                "rounds": rounds,
                "partial_scenarios": partial_scenarios,
                "union_integrity": "degraded",
                "note": "acceptance partial != map partial; union "
                        "completeness explicitly degraded"}
    return {"state": "valid", "converged": True, "rounds": rounds,
            "union_integrity": "full"}


def intolerant_draft(entries: "list[dict]") -> dict:
    """gherkin_scenarios 不耐受声明草稿（与 --intolerant-tests 的
    gherkin_scenarios 数组同构，可直接保存使用）。"""
    scenarios = []
    for e in entries:
        item = {"feature": e["feature"], "scenario": e["scenario"],
                "reason": e.get("reason") or
                "suspected_gherkin_intolerance: passes uninstrumented "
                "replay, fails instrumented run — confirm to exempt or "
                "investigate"}
        if e.get("scenario_line") is not None:
            item["scenario_line"] = int(e["scenario_line"])
        if e.get("example_row_line") is not None:
            item["example_row_line"] = int(e["example_row_line"])
        scenarios.append(item)
    return {"gherkin_scenarios": scenarios}


def parse_gherkin_scenarios(cfg: dict) -> list[dict]:
    """`gherkin_scenarios` 声明解析与校验（身份键必填、行号可选）。

    旧键 `acceptance_scenarios` 已退役：仍存在 → 具名 fail-loud（禁止
    静默忽略导致新键被丢弃为空集）。"""
    if "acceptance_scenarios" in cfg:
        raise ValueError(
            "intolerant 'acceptance_scenarios' is retired — use "
            "'gherkin_scenarios' (fail-loud: the retired key is never "
            "ignored silently)")
    decl = cfg.get("gherkin_scenarios", [])
    if not isinstance(decl, list):
        raise ValueError("intolerant 'gherkin_scenarios' must be a list")
    out = []
    for item in decl:
        if not isinstance(item, dict):
            raise ValueError("gherkin_scenario declaration must be an "
                             f"object, got {item!r}")
        for key in ("feature", "scenario", "reason"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(
                    f"gherkin_scenario missing non-empty {key!r} "
                    "(declarations without a name/reason are a silent-"
                    "exemption shortcut)")
        entry = {"feature": item["feature"], "scenario": item["scenario"],
                 "reason": item["reason"]}
        for k in ("scenario_line", "example_row_line"):
            if item.get(k) is not None:
                entry[k] = int(item[k])
        out.append(entry)
    return out
