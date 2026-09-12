# gqt 接口契约（AC-DECPL-005）

> 权威来源：`src/godot_qa_toolkit/cli.py`（SEE-1268 M1/M2 落地）。本文档化机器可判的输入/输出约定，消费方（agent、CI、其他脚本）以此为准。实现与本文冲突时以代码为准并回改本文档。

## 1. 插件定位声明

godot-qa-toolkit 是一个**Godot 项目专用的确定性 QA 测试工具插件**（五阶段工具链：Gherkin runner、complexity gate、mutation runner、coverage runner、Godot determinism 逻辑）。

与 godot-mcp 运行时库的分层边界（Owner 2026-09-09 两库拆分裁定）：

| | godot-mcp（① 运行时库） | godot-qa-toolkit（② 工具库，本库） |
|---|---|---|
| 职责 | 运行时驱动：addon 内核 + node proxy/server + launch/ 控制面（启动/驱动真实 editor） | 测试手段：对任何 Godot 项目做测试度量与判定 |
| 与被测项目关系 | 以运行时身份接入项目（MCP 端口、lease、协议） | 以**只读离线工具**身份消费项目（`--project-root` 显式传入） |
| Godot/GUT 依赖 | 通过被驱动 editor 运行 | 通过被测项目自带的 `addons/gut/` 跑测试；工具自身零 Godot 依赖 |

解耦红线（本库自我约束）：

- **零代码级耦合**：`src/` 不引用、不探测任何具体游戏项目（无环境变量探测、无向上找配置文件）。被测项目的一切信息只经 CLI 参数进入。
- **消费方显式传参**：`--project-root` 是唯一的被测项目入口；调用方自己知道项目在哪，本库不做任何发现式猜测。
- 历史注释中的具体项目实证（SEE-1268 Revy QA）已改中性表述（AC-DECPL-005：`grep -rn KOL src/` = 0）。

## 2. 调用形态

```
gqt <subcommand> [args]        # console script（pyproject [project.scripts]）
python -m godot_qa_toolkit.cli <subcommand> [args]   # 等价入口
```

四子命令：`gherkin` / `complexity` / `mutation` / `coverage`。

## 3. 统一输出 JSON 契约

所有子命令在 **stdout** 输出恰好一个 JSON 对象（`ensure_ascii=False`，缩进 2），带换行结尾：

```json
{
  "tool": "<gherkin|complexity|mutation|coverage>",
  "ok": true,
  "summary": { },
  "failures": [ ]
}
```

- `tool`：子命令名，恒定。
- `ok`：机器判定总结论（P0' 原则——结论确定可判，工具内不做启发式打分）。
- `summary`：各工具专属度量（见 §4），结构随工具不同。
- `failures`：失败明细数组，`ok=true` 时为 `[]`。每项至少含 `reason` 字符串。

各工具的额外顶层字段：

| 工具 | 额外字段 | 说明 |
|---|---|---|
| gherkin | `feature` | Feature 名；语法解析失败时为 `null` |
| complexity | `config`、`warnings` | 生效阈值 `{warn_complexity, max_complexity}`；仅告警项（不进 failures） |

错误兜底：stdout 无 JSON（JSON 解析失败）= 工具实现 bug 或进程级崩溃，消费方按「不可信」处理，而非按 fail。

## 4. 各子命令契约

### 4.1 `gqt gherkin <feature> [--steps <module.py>]`

- 输入：`.feature` 文件路径；可选 steps 模块路径（模块须暴露 `register(registry)`，registry 来自 `godot_qa_toolkit.gherkin.runner.Registry`）。
- 不给 `--steps`：每个 step 都 unmatched → 整体 fail 判定（合法行为，非错误）。
- 语法错误（GherkinSyntaxError）：`feature: null`、`summary: {total:0, passed:0, failed:1}`、`failures[0].reason` 含 `path:line:` 前缀、exit 1。
- 成功输出：

```json
{"tool":"gherkin","ok":true,"feature":"<name>",
 "summary":{"total":N,"passed":P,"failed":F},
 "failures":[{"scenario":"...","line":N,"keyword":"Then","text":"...","reason":"..."}]}
```

### 4.2 `gqt complexity <paths...> [--warn 10] [--max 15]`

- 输入：一个或多个 `.gd` 文件/目录。
- 阈值语义：`> max` → violation（fail）；`warn < cc <= max` → warning（不 fail）。`--max` 降到 `--warn` 之下时 warn 与其合并（`warn = min(warn, max)`）。
- 不可转换文件（gd2py 失败）计为 failure（unmeasured = fail signal，绝不静默跳过）。
- `ok = 无 violation 且无 unparseable`。
- 输出：`summary: {functions, max_seen, violations, warnings, unparseable}`；`failures` = violations（含 file/name/line/complexity/threshold）+ unparseable；`warnings` 同构独立数组。

### 4.3 `gqt mutation <file.gd> --project-root <root> [--budget 50] [--timeout 60]`

- 前置：`<root>/addons/gut/gut_cmdln.gd` 必须存在；`godot` 可执行文件必须在 PATH。缺失任一 → 统一 JSON failure（exit 1，`failures[0].reason` 含 `not found`）。
- 执行：baseline GUT（原文件）→ 逐 mutant 改写目标文件跑 GUT（每次后恢复原文件，最终态与原状一致）。
- **kill 判定 = 失败测试名集合相对 baseline 的差集**，不看 exit code——被测项目基线在无头环境可能自带 pre-existing failures，按 rc 判定会全部误杀。
- 中止契约（baseline 本身不可信时，mutant 数据不产出）：

```json
{"tool":"mutation","ok":false,
 "summary":{"file":"...","run_error":true,"error":"..."},
 "failures":[{"reason":"baseline GUT timed out after Ns ..."}]}
```
  注意：该形态下 `summary` 无 `killed` 等键（消费方不要按默认 0 读取）。
- 正常输出：`summary: {file, mutants, killed, survived, timeout, run_errors, kill_rate, budget}`；`failures` = 非 killed 的 mutant 明细（verdict ∈ survived/timeout/run_error）。
- `ok = survived==0 且 timeout==0 且 run_errors==0`。

### 4.4 `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout 120]`

- 前置与 mutation 相同（GUT + PATH 上的 godot）。
- 执行：目标 .gd 行级插桩（每可执行语句前插探针调用 + 文件尾部探针函数）→ headless GUT → 读 `<root>/qa-coverage-hits.txt` 命中行 → 行覆盖%。原文件运行后恢复。
- 副作用边界：探针只写 `<project-root>/qa-coverage-hits.txt`（gitignored，幂等覆盖），不修改测试代码/其他被测代码。
- godot 进程级失败（脚本没加载）≠ 真实 0% 覆盖 → `summary.run_error: true`，与 mutation 同款中止契约。
- 正常输出：`summary: {file, total_lines, covered_lines, coverage_percent, min_percent}`；未达标时 `failures[0]` 附 `covered/total/uncovered_lines`（截断 20 行）。
- `ok = coverage_percent >= min_percent`。

## 5. exit code 语义

| exit | 语义 | 触发 |
|---|---|---|
| 0 | 判定通过（`ok=true`） | 任意子命令 |
| 1 | 判定失败（`ok=false`，stdout 有 JSON） | verdict fail / run_error 中止 / 目标文件或 GUT runner 不存在 |
| 2 | 用法/装载错误，**stdout 无 JSON** | argparse 参数错误；`gherkin --steps` 模块加载失败（stderr 报 `cannot load steps module`） |

约定：exit 2 是「调用方自己错了」，exit 1 是「被测对象判为失败」。消费方自动化只应区分 0 与非 0，并解析 stdout JSON 细分 1 的形态。

## 6. `--project-root` 边界约定

- 必填于 `mutation` / `coverage`；必须指向含 `addons/gut/gut_cmdln.gd` 的 Godot 项目根。
- 工具在该 root 下运行 `godot --headless --path <root> -s res://addons/gut/gut_cmdln.gd -gdir=res://tests/ -gexit`（`-s` 必须是 res:// 形式，绝对路径会被 Godot 拒绝加载——见 mutation/runner.py 注释）。
- 工具可能写入的路径仅限：目标 .gd（临时改写、必然恢复）与 `<root>/qa-coverage-hits.txt`（coverage 探针）。除此之外零写入。
- timeout 语义：单次 GUT 全套测试的 wall-clock 上限；被测项目基线超过 timeout 时 mutation 返回 run_error 中止（数据不可信优于慢数据）。
