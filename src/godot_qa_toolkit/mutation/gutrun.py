"""GUT 输出判定原语（SEE-1420 Phase 1 自 runner.py 迁出——<800 行纪律）。

kill 判定的机器判据面：godot 进程级失败识别 + 失败测试名解析。与
coverage/runner.py 的同族副本语义保持一致（两侧刻意不共享实现——契约
冻结面不同，历史 QA 判据分别锚定）。
"""

from __future__ import annotations

import re

# GUT 的命令行入口必须以 res:// 形式传给 `godot -s`：Godot 对绝对路径的 -s
# 一律报 "Attempt to open script ... File not found"（Revy QA FAIL 实证），
# 文件真实存在也一样——res:// 是唯一可靠形式。
GUT_SCRIPT_RES_PATH = "res://addons/gut/gut_cmdln.gd"

# godot 进程级失败的特征（与"测试跑过但失败"截然不同——绝不能计入 kill）。
_GODOT_LAUNCH_ERROR_MARKERS = (
    "attempt to open script",
    "file not found",
    "failed loading resource",
    "error: failed to load script",
)

# GUT 测试流证据：运行流 `* test_x` 行 / Run Summary 段 `- test_x` 行 / 逐测试
# 结果标记 [passed]/[Failed]/[Pending]/[Risky]。与 _parse_failing_tests 同族
# token——kill 判定能看到的流，本守卫必须承认"测试确实跑了"。
_GUT_RUN_EVIDENCE = re.compile(
    r"(?mi)^[*-]\s+test_\S+|\[(?:passed|failed|pending|risky)\]")


def _looks_like_godot_launch_failure(output: str) -> bool:
    """true 仅当：启动失败特征在场 ∧ 输出中无任何 GUT 测试流证据。

    SEE-1400 §SPEC-012（Revy 实测 CRITICAL）：整段输出子串匹配会被项目
    既有资源加载告警污染——变异轮 rc=1 且失败差集真实存在时被错杀成
    run_error。判定不得对 stdout/stderr 截断窗口形态敏感：测试流证据
    在场时，告警字样一律按背景噪声处理，继续走 kill/survive 分类。
    """
    low = output.lower()
    if not any(marker in low for marker in _GODOT_LAUNCH_ERROR_MARKERS):
        return False
    return _GUT_RUN_EVIDENCE.search(output) is None


def parse_failures_with_scripts(gut_output: str) -> set[tuple[str, str]]:
    """§SPEC-P2-R1-D2：脚本限定的失败集合 {(script, test_name)}。

    GUT 运行流中每个脚本先打印完整路径（`res://…/test_x.gd`，可选
    `.Inner` 后缀），其后的 `* test` / `[Failed]` 归属该脚本。回到
    Run Summary 段（`- test` 无脚本上下文）时归属降级为通配 `*`——
    通配仅用于「同名绿测豁免」的保守回退，绝不用于跨脚本误删。
    无任何脚本标题时返回空集（调用方回退裸名语义）。"""
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    out: set[tuple[str, str]] = set()
    current_script = ""
    current_test = None
    in_summary = False
    for raw in gut_output.splitlines():
        line = ansi.sub("", raw).replace("\r", "")
        stripped = line.strip()
        if stripped.startswith("res://") and ".gd" in stripped:
            current_script = stripped
            current_test = None
            continue
        if "Run Summary" in stripped or stripped.startswith("Totals"):
            in_summary = True
            current_test = None
            continue
        m = re.match(r"^[*-]\s+(test_\S+)", stripped)
        if m:
            current_test = m.group(1)
            continue
        if "[Failed]" in line and current_test:
            out.add((current_script if not in_summary else "*", current_test))
    return out


def _parse_failing_tests(gut_output: str) -> set[str]:
    """从 GUT 输出解析失败测试名集合。

    双格式兼容（硬化 实机实证）：
    - 运行流：'* test_xxx' 开启一个测试，其后 [Failed] 行归属它；
    - Run Summary 段（GUT 4.6 win64 实测）：'- test_xxx' 后跟 [Failed] 行，
      但输出被截断后往往只剩该段——单靠运行流正则会漏掉全部失败。
    用测试名集合做 kill 判定——exit code 只能说明"有没有失败"，无法区分
    pre-existing 失败与 mutant 引入的新失败（SEE-1268 Revy QA 实证）。
    """
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    failing: set[str] = set()
    current = None
    for raw in gut_output.splitlines():
        line = ansi.sub("", raw).replace("\r", "")
        m = re.match(r"^[*-]\s+(test_\S+)", line)
        if m:
            current = m.group(1)
            continue
        if "[Failed]" in line and current:
            failing.add(current)
    return failing


def executed_script_headers(gut_output: str) -> set[str]:
    """C3-R23（V1）：运行流里实际执行脚本的 `res://` 头集合（与
    parse_failures_with_scripts 同一识别规则）。执行账按脚本归因的
    「谁真的跑了」证据——同名碰撞下 `-gselect` 按 basename 可能选中
    多个物理脚本，只有头集能区分；无头（旧形态/截断只剩摘要）→ 空集
    （调用方按保守并集回退，不放宽既有方向）。"""
    ansi = re.compile(r"\x1b\[[0-9;]*m")
    out: set[str] = set()
    for raw in gut_output.splitlines():
        stripped = ansi.sub("", raw).replace("\r", "").strip()
        if stripped.startswith("res://") and ".gd" in stripped:
            out.add(stripped)
    return out


def script_evidence(gut_output: str) -> dict:
    """C3-R24（N2/§SPEC-038⑥）：截断前一次性提取的脚本级执行证据——
    执行的脚本头集、(script, name) 失败对、失败名集（JSON 友好形态，
    随执行账保存）。增量重跑的执行账必须在**完整输出**上解析（部分
    截断会把前一脚本的头/失败切掉，同名前脚本被误判「未执行」继承旧
    判定 = 假 kill 方向）；out 允许截断只作披露。三个解析器单源复用，
    识别规则不另起一份。"""
    return {"executed": sorted(executed_script_headers(gut_output)),
            "pairs": sorted([s, n] for s, n in
                            parse_failures_with_scripts(gut_output)),
            "failing": sorted(_parse_failing_tests(gut_output))}
