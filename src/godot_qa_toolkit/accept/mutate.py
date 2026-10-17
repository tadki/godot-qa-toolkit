"""契约变异器（SEE-1426 P3，§SPEC-1426-11）：读 IR 产 mutant 期望值。

矩阵 v1 全覆盖：
- 数值：±1 / 边界外推（0→-1、边界值外推一位）
- 布尔：翻转（"true"<->"false"）
- 状态枚举：声明域内邻值切换（b→a / b→c）；域外从不生成；域未声明的
  Then 绑定在 parse 期已被拒（parser fail-fast）

变异只改 Then 绑定占位符对应的期望 cell（values），步骤文本展开由生成
测试运行时从 IR 读取——同一套生成测试既跑交付验收也跑变异轮（断言主体
单一化）。unchanged 槽字段同进变异矩阵（改基线预期，污染必须被抓）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import OPS_VERSION

# 枚举域声明形态：enum{a,b,c}
_ENUM_RE = re.compile(r"^enum\{([^}]*)\}$")

# 数值 cell 判定（与 parser 的 _NUMERIC_CELL_RE 同语义：int/float/负数）。
_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


@dataclass(frozen=True)
class Mutant:
    mutant_id: str
    case_test_name: str
    target: str        # 占位符名（values 键）或 unchanged 路径
    kind: str          # numeric|boolean|enum|unchanged
    original: str
    mutated: str
    rationale: str     # ±1 / boundary / flip / neighbor


def _numeric_mutants(cell: str) -> list[tuple[str, str]]:
    """数值 cell → [(mutated, rationale)]：±1 + 边界外推。

    0-cell 的边界外推（0→-1）与 -1 算子产出同值——rationale 合并为
    "-1+boundary"，mutant_id（按 mutated 值构成）保持唯一（披露①去重，
    §SPEC-1426-11 计数一致性）。
    """
    out: list[tuple[str, str]] = []
    try:
        v = float(cell)
    except ValueError:
        return out
    is_int = "." not in cell
    for delta, tag in ((1, "+1"), (-1, "-1")):
        m = v + delta
        out.append((str(int(m)) if is_int and float(m).is_integer() else repr(m),
                    tag))
    # 边界外推：0 → -1（越过下界）；与 -1 算子同值则合并 rationale
    if v == 0:
        for i, (mutated, r) in enumerate(out):
            if mutated == "-1":
                out[i] = ("-1", f"{r}+boundary")
                break
        else:
            out.append(("-1", "boundary"))
    return out


def _boolean_mutants(cell: str) -> list[tuple[str, str]]:
    low = cell.strip().lower()
    if low == "true":
        return [("false", "flip")]
    if low == "false":
        return [("true", "flip")]
    # 无类型声明的字面 true/false cell：按布尔翻转（Gherkin 布尔只有两形）。
    return []


def _enum_mutants(cell: str, domain: str) -> list[tuple[str, str]]:
    """域内邻值切换；cell 不在域内 = 不生成（域外从不生成）。"""
    m = _ENUM_RE.match(domain)
    if not m:
        return []
    members = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if cell not in members:
        return []
    out: list[tuple[str, str]] = []
    idx = members.index(cell)
    if idx > 0:
        out.append((members[idx - 1], "neighbor"))
    if idx < len(members) - 1:
        out.append((members[idx + 1], "neighbor"))
    return out


def mutate_case(case: dict) -> list[Mutant]:
    """一个 IR case → mutants。期望值绑定面全矩阵 + unchanged 槽基线。

    展开后的 Then 步骤文本已无 <x>（parse 期替换成 cell 值）——变异目标
    从类型声明反推：types 里声明的占位符，其 cell 是本 case 的期望值面
    （Then 文本里的具体值就是该 cell 的展开）。非数值/布尔/枚举声明的
    类型不产变异（保守：变异器只动它认识的面）。
    """
    mutants: list[Mutant] = []
    values = case.get("values", {})
    types = case.get("types", {})
    for name, spec in types.items():
        cell = values.get(name, "")
        cands: list[tuple[str, str, str]] = []
        enum_m = _ENUM_RE.match(spec)
        if enum_m:
            cands = [(m, "enum", r) for m, r in _enum_mutants(cell, spec)]
        else:
            cands = _untyped_mutants(cell, spec)
        for mutated, kind, rationale in cands:
            mutants.append(Mutant(
                mutant_id=f"{case['test_name']}#{name}={mutated}",
                case_test_name=case["test_name"],
                target=name, kind=kind,
                original=cell, mutated=mutated, rationale=rationale))
    mutants.extend(_unchanged_mutants(case))
    return mutants


def _untyped_mutants(cell: str, spec: str) -> list[tuple[str, str, str]]:
    """非枚举声明的变异算子分流：数值 / 布尔（含无类型字面形）。"""
    if _NUMERIC_RE.match(cell):
        return [(m, "numeric", r) for m, r in _numeric_mutants(cell)]
    if cell.strip().lower() in ("true", "false") or spec == "bool":
        return [(m, "boolean", r) for m, r in _boolean_mutants(cell)]
    return []


def _unchanged_mutants(case: dict) -> list[Mutant]:
    """unchanged 槽变异：篡改"必须不变"的基线值——若断言面完备，被污染
    状态必挂；若 unchanged 槽根本没接线，挂不掉 = 存活即打回（§SPEC-1426-12③）。"""
    return [
        Mutant(
            mutant_id=f"{case['test_name']}#unchanged:{u}=polluted",
            case_test_name=case["test_name"],
            target=u, kind="unchanged",
            original="<baseline>", mutated="<polluted>",
            rationale="unchanged-pollution")
        for u in case.get("then_unchanged", [])
    ]


def mutate_ir(ir: dict) -> list[Mutant]:
    """整份 IR → 全量 mutants（v1 无 stamp：每轮全量，无增量跳过）。"""
    mutants: list[Mutant] = []
    for case in ir.get("cases", []):
        mutants.extend(mutate_case(case))
    return mutants
