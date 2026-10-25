"""契约变异器（SEE-1426 P3，§SPEC-1426-11；SEE-1468 /2 类型族扩展）。

矩阵 v2 全覆盖：
- 数值：±1 / 边界外推（0→-1、边界值外推一位）
- 布尔：翻转（"true"<->"false"）
- 状态枚举：声明域内邻值切换（b→a / b→c）；域外从不生成；域未声明的
  Then 绑定在 parse 期已被拒（parser fail-fast）
- /2 float_near:<tol>：±max(1, 2×tol)（容差数值步长，定案 §2）
- /2 not_contains：退化空串（"" 被一切串包含 → 断言必挂 = 确定性 kill）
- /2 in_set：退化坍缩集（空集不含任何成员 → 断言必挂 = 确定性 kill）
- /2 signal_count：整数 ±1
- /2 str：零变异——默认豁免（Owner 裁决 3：证明有判别力才 per-suite
  显式启用；「str 恒豁免」为可接受正常结局）
- `# mutation: exempt` 场景：typed cell 矩阵整体豁免，unchanged 槽基线
  污染探针照跑（exempt ≠ skip）

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

# /2 float_near 带参声明形态：float_near:<tol>
_FLOAT_NEAR_RE = re.compile(r"^float_near:([+-]?(?:\d+\.?\d*|\.\d+))$")

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


def _float_near_mutants(cell: str, tol: float) -> list[tuple[str, str]]:
    """/2 容差数值：±max(1, 2×tol)（定案 §2 固定步长）。步长 ≥ 2×tol 使
    |mutant−actual| ≥ 步长−tol ≥ tol——容忍边界重合的极端点（tol 恰使
    步长=2×tol 且 actual 恰在边界）为已知理论残差，不额外扩步。"""
    try:
        v = float(cell)
    except ValueError:
        return []
    step = max(1.0, 2.0 * tol)
    is_int = "." not in cell
    out: list[tuple[str, str]] = []
    for direction, tag in ((1, "+step"), (-1, "-step")):
        m = v + direction * step
        out.append((str(int(m)) if is_int and float(m).is_integer() else repr(m),
                    tag))
    return out


def _declared_mutants(cell: str, spec: str) -> list[tuple[str, str, str]]:
    """/2 声明类型算子分流（str → 零变异 = 默认豁免，Owner 裁决 3）。"""
    if spec == "str":
        return []
    enum_m = _ENUM_RE.match(spec)
    if enum_m:
        return [(m, "enum", r) for m, r in _enum_mutants(cell, spec)]
    if spec == "bool":
        return [(m, "boolean", r) for m, r in _boolean_mutants(cell)]
    fn_m = _FLOAT_NEAR_RE.match(spec)
    if fn_m:
        return [(m, "float_near", r)
                for m, r in _float_near_mutants(cell, float(fn_m.group(1)))]
    if spec == "not_contains":
        # 退化空串：正确实现的 contains 断言对 "" 恒失败 → 确定性 kill。
        return [("", "not_contains", "degenerate-empty-needle")]
    if spec == "in_set":
        # 退化坍缩集："" 分裂为 {""}，非空 actual 不为其成员 → 确定性 kill。
        return [("", "in_set", "set-collapsed")]
    if spec == "signal_count":
        if _NUMERIC_RE.match(cell):
            return [(m, "signal_count", r)
                    for m, r in _numeric_mutants(cell)]
        return []
    return _untyped_mutants(cell, spec)


def mutate_case(case: dict) -> list[Mutant]:
    """一个 IR case → mutants。期望值绑定面全矩阵 + unchanged 槽基线。

    展开后的 Then 步骤文本已无 <x>（parse 期替换成 cell 值）——变异目标
    从类型声明反推：types 里声明的占位符，其 cell 是本 case 的期望值面
    （Then 文本里的具体值就是该 cell 的展开）。非数值/布尔/枚举声明的
    类型不产变异（保守：变异器只动它认识的面）。
    `# mutation: exempt` 场景（/2）：typed 矩阵豁免，unchanged 槽照跑
    （exempt ≠ skip——豁免 case 在 mutate 轮仍执行未变异基线）。
    """
    mutants: list[Mutant] = []
    values = case.get("values", {})
    types = case.get("types", {})
    if not case.get("mutation_exempt", False):
        for name, spec in types.items():
            cell = values.get(name, "")
            for mutated, kind, rationale in _declared_mutants(cell, spec):
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
