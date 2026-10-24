# 新增检查器配方（collector → gate → cli lazy import）

本库的每个"检查器"（complexity / arch）都遵循同一个三层结构。新检查器立项时
照此落地即可接入统一 JSON 契约与 CLI，无需改动既有工具。

## 三层职责

| 层 | 位置 | 职责 |
|---|---|---|
| **collector** | `src/godot_qa_toolkit/<tool>/collector.py` | 只读、纯函数地**产出结构化违规事实**（file/line/rule/message），不做判定 |
| **gate** | `src/godot_qa_toolkit/<tool>/gate.py` | 把 collector 事实**归并为统一 JSON 契约** `{tool, ok, summary, failures}`，做 pass/fail 判定 |
| **cli** | `src/godot_qa_toolkit/cli.py` 的 `cmd_<tool>` | **lazy import** gate 并 `_emit(...)` 输出 |

参照实现：`complexity/collector.py` + `complexity/gate.py`，以及后续加入的
`arch/collector.py` + `arch/gate.py`（`docs/` 无现成 recipe 前以复杂度为最简
范本——collector 单函数收集、gate 单函数判定）。

## 关键纪律

1. **collector 不判定**：只产出事实；`unparseable` 等"不可测即 fail"信号也由
   gate 归并为 failure（绝不静默跳过）。
2. **gate 统一形态**：`ok = 无 violation 且无 unparseable`；`failures` 每项至少含
   `reason`；输出经 `_emit` 走 stdout JSON（`ensure_ascii=False`、缩进 2、换行结尾）。
3. **cli 必须 lazy import**（`§SPEC-114` 最小 CI 面）：检查器通常拉 gdtoolkit 等
   heavy 依赖，`cmd_*` 处理器内部才 `from .<tool>.gate import ...`——保证无该依赖的
   环境（如只需 `gqt gherkin` 的极简 runner）也能启动 CLI。
4. 规则 id / verdict 枚举保持**开放集合**（契约 §7 加法演进）：消费方过滤时须容忍
   未知值，新增规则只加不减。

## 落地步骤

1. 新建 `src/godot_qa_toolkit/<tool>/collector.py`（纯收集）与 `gate.py`（判定 + JSON）。
2. 在 `cli.py` 加 `sub.add_parser("<tool>", ...)` 与 `cmd_<tool>`（lazy import gate）。
3. 写单测（collector 反例 + gate 契约），并在契约文档 `docs/contract.md` 增一节子命令契约。
4. 如需 CI 守护，参照 `.github/workflows/ci-pytest.yml` 的 complexity 自消费门。