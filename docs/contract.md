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

七个子命令：`gherkin` / `arch` / `accept` / `complexity` / `mutation` / `coverage` / `doctor`（SEE-1426 起 doctor 随自检能力加入）。

## 3. 统一输出 JSON 契约

所有子命令在 **stdout** 输出恰好一个 JSON 对象（`ensure_ascii=False`，缩进 2），带换行结尾：

> **doctor 例外**（SEE-1463 登记）：`gqt doctor` 自检 JSON 打 **stderr**（不产出判据 JSON），stdout 恒空——它与其它子命令不同，不判被测对象而是判定「editable 安装 vs 当前 worktree gitlink」的安装一致性，结果属于诊断而非判据；错配时 exit 2（见 §5）。

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

### 4.2 `gqt complexity <paths...> [--warn 10] [--max 15]`

- 输入：一个或多个 `.gd` 文件/目录。
- 阈值语义：`> max` → violation（fail）；`warn < cc <= max` → warning（不 fail）。`--max` 降到 `--warn` 之下时 warn 与其合并（`warn = min(warn, max)`）。
- 不可转换文件（gd2py 失败）计为 failure（unmeasured = fail signal，绝不静默跳过）。
- `ok = 无 violation 且无 unparseable`。
- 输出：`summary: {functions, max_seen, violations, warnings, unparseable}`；`failures` = violations（含 file/name/line/complexity/threshold）+ unparseable；`warnings` 同构独立数组。

### 4.2 `gqt arch <paths...>`（SEE-1426 P1）

- 输入：一个或多个 `.gd` 文件/目录（gdtoolkit AST 静态检查）。
- 规则 id 开放枚举（contract §7 加法演进），v1：`core/extends-whitelist`（extends 白名单：RefCounted/Resource 等纯数据基类）、`no-engine-api`（禁引用 Node/SceneTree/Engine/Input/get_tree/get_node 等引擎全局/场景树入口，含类型注解形态）、`no-bare-global-random`（禁裸全局随机 randi/randf/randfn/range 版本/randomize；文件内显式 `RandomNumberGenerator` 在场时成员调用形式放行）。
- 输出：`summary: {files, violations, unparseable, by_rule}`；`failures` = violations（file/line/rule/message）+ unparseable（不可解析 = fail signal）。`ok` = 无违规且无 unparseable。
- exit 0/1，统一 JSON 契约。

### 4.2a `gqt accept compile <feature> --project-root <root> [--write] [--no-clean]`（SEE-1426 P2；SEE-1468 /2）

- `.feature` → 版本化 IR（`ops_version = "see1468-accept/2"`）→ 数据驱动 GUT 测试（运行时读 IR，逐 case 一个 GUT 测试方法）→ 归因 manifest（scenario→test_name，非 ASCII 场景名标识符化 + 序号保障唯一）。
- **ops_version major bump 纪律（/2）**：schema 键集或调用约定变更 = major bump；旧版本 manifest 在 run 侧 replay_check 严格相等校验下 run_error（fail-fast，零新增机制代码）。
- IR schema（/2）：`{ops_version, feature, source_path, cases:[{scenario, scenario_line, example_row_line, values, steps, types, then_unchanged, mutation_exempt, test_name}]}`。schema 冻结五件套（Examples 表一等公民、占位符 `<x>` 归一化、`# types:` 类型声明含 `enum{...}`/`float_near:<tol>` 域与 `str`/`bool`/`not_contains`/`in_set`/`signal_count` 族（声明类型与 cell 形态失配 = parse error；`in_set` 成员以逗号分隔、**周边空白由 compile 期归一化剥离**（QA F2：IR 存归一化 cell，parse 面与模板 naive split 同源）、**成员内含逗号不可表达**）、`# unchanged:` 反向断言槽（/2 放宽多段点路径 `a.b.c`）、manifest 哈希绑定）+ `# mutation: exempt` scenario 级指令（每 scenario 恰好一行，重复 = parse error）全部在 parse 期 fail-fast（违规 = GherkinSyntaxError → JSON 契约 ok:false）。
- 产物三件：`acceptance/ir/<key>.json` + `acceptance/generated/test_<key>.gd`（GUT 目录扫描按 `test_` 前缀过滤——非 test_ 前缀脚本被忽略；**/2 起 run_step 全量 await 化**——GDScript await 对非协程返回值透传，同步 harness 零破坏，场景生命周期步骤可为协程）+ `acceptance/generated/<key>.manifest.json`（含 ir_hash/generated_hash/worker_hash sha256 绑定 + attribution 表）。worker 生成物（`accept_worker.gd`）NDJSON 循环协程化（`_drive()` fire-and-forget，协议不变）。
- 写入仅限 §6 白名单两目录；先清空再生成（`--no-clean` 退路）；写入 opt-in（`--write`），`summary.wrote` 逐文件披露；GUT runner 缺失 = JSON 契约 ok:false。

### 4.2b `gqt accept run <dir> --project-root <root> --key <feature-key> [--timeout 120]`（SEE-1426 P2）

- 执行前三件哈希绑定重放校验：manifest 的 ir_hash/generated_hash 与磁盘实际 sha256 必须一致，任一失配 = run_error（`summary.run_error: true`，无告警放行档）；GUT runner 缺失同理。**重锚防线（§SPEC-1426-24）**：run 侧把 manifest.source_path 指向的源 `.feature` 现场重编译核对 IR 哈希一致——源文件缺失或漂移 = run_error。**tests_executed 守卫（§SPEC-1426-24）**：GUT 输出零测试流而 exit 0 = 判定失败（`summary.tests_executed: 0` + 显式 failure，不是绿）。
- 执行：一次性 `godot --headless` 子进程跑 GUT（`-gdir=res://acceptance/generated`），跨进程槽位闸包裹（复用 mutation 的 GlobalGodotGate）。
- 输出：统一契约回译——失败测试名经 manifest attribution 对回 scenario/line，失败 reason 附 GUT `[Failed]` 断言明细（§SPEC-1426-25，含 unchanged slot 违规文本）；无法归因的失败显式披露（`scenario: null` + `not attributable` reason）。`summary.{replay_checked, ir_hash, exit_code, tests_executed, wrote}`。

### 4.2c `gqt accept mutate <dir> --project-root <root> --key <feature-key> [--timeout 120] [--worker]`（SEE-1426 P3）

- 全量契约变异闭环（v1 无 stamp，每轮全量、无增量跳过）：对 IR 全量产 mutants → 逐 mutant 写临时变异 IR（磁盘 IR 每轮后恢复原样）→ headless 跑同一套生成测试 → 挂 = kill；活 = survived 即打回（`ok=false` + survived 明细）。
- 变异矩阵（§SPEC-1426-11；/2 扩展）：数值 ±1 + 边界外推（0→-1）、布尔翻转、枚举声明域内邻值切换（域外从不生成；域未声明的 Then 绑定在 parse 期已被拒）、`float_near:<tol>` ±max(1, 2×tol)、`not_contains` **断言语义翻转**（not_contains → contains：载荷不变，mutant 经 `case.mutated_not_contains` 通道翻转发该 cell 断言极性，harness 按契约消费——基线绿 ⇒ actual 真不含 needle ⇒ 翻转断言必假 = 确定性 kill，与检查惯用法无关；QA F1 裁定）、`in_set` 退化坍缩集（模板 `split(",", false)` 下 `"" → []` → 必挂）、`signal_count` ±1；**`str` 零变异 = 默认豁免**（Owner 裁决 3：启用 str ±1 须 per-suite 显式声明，未启用前「str 恒豁免」为正常结局）；unchanged 槽纳入矩阵（基线污染探针）。
- **豁免轮（/2，exempt ≠ skip）**：`# mutation: exempt` 场景的 typed 矩阵不产 mutants，但其 case 在 mutate 侧执行一次未变异基线轮（先于 mutant 轮）：绿 = 逐 case `exempt_verified`、挂 = 逐 case run_error 计入门禁；未变异基线任何 case 挂 = fail-fast 跳过 mutant 轮（`summary.exempt_baseline_failed: true`）。纯豁免特性（零 mutants + 有豁免 case）走基线轮收口，不触发 `no mutants generated`。
- 三层防伪（§SPEC-1426-12）：① 变异器对抗性金测（70/71 fixture 恰好 ±1/边界/邻值，单测面）；② 哨兵核对——GUT 输出零测试流 = 执行链失真 = run_error 面失败；③ unchanged 槽变异必挂（挂不掉 = survived 打回）。零 Then 绑定且零 unchanged 且零豁免的 IR = run_error（`no mutants generated`）。
- `--worker`（§SPEC-1426-14）：opt-in **真常驻通道**——compile 产出的 worker 生成物（`acceptance/generated/accept_worker.gd`，受先清空再生成管辖 + `worker_hash` 哈希绑定）以单个 godot 进程服务全矩阵：stdin NDJSON 请求行（`{mutant_id, ir_file}` 小载荷，恒 <1024B——`OS.read_string_from_stdin()` 行缓冲上限 1024B，QA-1 实证）+ 变异 IR 经 `.qa-cache/gqt-accept-worker/` 轮转临时文件（FileAccess 读，零行缓冲限制，用后即删、通道结束清目录）；每 mutant 结束硬纪律 = 释放全部 testing_api 实例 + 重建（reload）+ 自检探针断言复位，不符即 run_error verdict。读侧带超时（复用 `--timeout` 为每轮 verdict 等待上限，超时 = _WorkerDead → 剩余 mutant 记 run_error，MEDIUM-1 修复）。验收 = batch 与 worker 两条真实代码路径的差分对照 verdict 一致性（实机口径：IR ≥1678B 量级、非挂死、ok/killed/survived 三项相等）。
- 输出：`summary.{feature, feature_key, ops_version, mutants, killed, survived, run_errors, full_matrix, worker, exempt_cases, exempt_results}`；`failures` = survived（含 target/kind/original/mutated/rationale）+ run_errors。`exempt_cases` == 源 .feature 内 `# mutation: exempt` 指令行数（V4 机器判据），`exempt_results` 逐 case 具名披露（`exempt_verified` / `run_error: ...`）。

### 4.3 `gqt mutation <file.gd> --project-root <root> [--budget 50] [--timeout 60] [--tests <res://glob>] [--dry-run] [--resume] [--mutation-warning N] [--timeout-factor F] [--mutate-all] [--mutation <id>] [--lines N] [--reuse-coverage]`

- **默认并行（SEE-1413）**：`--jobs` 缺省时单文件同样走并发 per-mutant 切片路径（默认 jobs = 上限 `max(2, cpu//2)`，复用 executor.default_jobs）。缺省单文件 CLI 顶层契约保持既有 flat 形态（`{"tool","ok","summary","failures"}`），`summary` 新增 `jobs` 键（串行=1，并行=该文件实际分片数）——只增不减，既有键集零变化；多文件及显式 `--jobs > 1` 的 `{"tool","ok","jobs","results"}` 编排信封不变，其 `results[i].summary.jobs` 同样记录该文件实际分片数。dry-run 的每文件 `jobs=1`；无 chunk 的 no-sites/setup-error settled 记录为 `jobs=0`。
- **自动退串行（SEE-1413）**：单文件变异点总数 ≤ 生效 jobs 时自动走既有串行路径（判定在变异点收集后、任何 GUT / prewarm / 共享 baseline 预跑之前——少变异场景并发固定开销倒挂），报告形态与串行一致（`jobs=1`）。显式 `--jobs 1` 仍强制串行。`--jobs` 帮助文本已修正为 per-file AND per-mutant slicing（单文件同样生效）。
- **timing store 语义（SEE-1413）**：退串行路径与 `--jobs 1` 串行快路径接线 TimingStore（预算协商/采样不丢）；并发分片路径不接线（worker 分片跳过 store 读写是 SEE-1356 L1 既有契约，adaptive timeout 只放大不收紧）。

- 前置：`<root>/addons/gut/gut_cmdln.gd` 必须存在；`godot` 可执行文件必须在 PATH。缺失任一 → 统一 JSON failure（exit 1，`failures[0].reason` 含 `not found`）。
- 执行：baseline GUT（原文件，**永远实测**）→ 差分规划 → 逐 mutant 内存注入跑 GUT（磁盘目标文件全程保持原始内容，每次后恢复）。
- **kill 判定 = 失败测试名集合相对 baseline 的差集**，不看 exit code——被测项目基线在无头环境可能自带 pre-existing failures，按 rc 判定会全部误杀。
- **差分默认开启（SEE-1420 定案 D4，行为变更）**：v2 resume manifest 在场即差分——未改 scope 且上次 killed/suspect/invalid_mutant → 复用跳过；survived / timeout / timeout_pinned / run_error → **永远重跑**（survived 永不缓存信任——红线）。`--mutate-all` 显式退路（全位点执行）。**CI 迁移点**：升级后首跑全量重跑一次（见下 resume v2）；此后 CI 红绿面由「每次全量」变为「差分 retry 面」——survived 位点仍每轮重跑，红不会因缓存变绿。
- **scope manifest / 稳定 id（定案 D1/D2/D3）**：scope 集 = 顶层 func + 内部 class 整体黑盒 + module（非 func 顶层声明）；header hash 只收非 func 顶层声明、func 变更绝不触发全文件重跑（单向保守）；无位点 scope 不进 manifest。位点 id = `<file>#<scope_id>#<n>`（scope_id = `func:`/`class:` kind 前缀 + 语义名，同 kind 同名消歧 `func:foo#2`——跨 kind 同名 func/class 不碰撞（Final Review MEDIUM-2）；n 为 scope 内 1-based 扫描序），**id 仅为寻址提示**——复用前必过全签名 (line, column, kind, original, mutated) + scope hash 双校验，任一漂移 = stderr 告警 + 拒绝复用 + 降级重跑，绝不静默按 id 取条目。
- **`# gqt: no-mutate`（定案 L6）**：注释过滤。marker 在顶层 func 定义行（含行尾注释）或其紧邻上方 → 函数级（抑制至下一顶层声明）；其余位置行级（仅 marker 所在行）。被过滤位点不产 mutant、其 scope 不进 manifest。
- **resume v2 store（一次 schema bump，OPS_VERSION `see1420/1`）**：
  - 落盘：`<root>/.qa-cache/gqt-mutation-resume-v2/<file-key>/manifest.json`（per-file 索引层：schema/header hash/含位点 scope hash 表；**收尾方单点写**）+ `<file-key>/<stable-id>.json`（per-mutant 条目：verdict + 全签名五元组 + scope_hash + `timeout_streak`；tmp+os.replace 原子写，各 worker 各写各 id，无跨 worker 竞态）。
  - file-key = sha1(项目内相对路径 | tests_glob | OPS_VERSION | binary_kind)。**无 budget**（D6：预算是每 run 执行上限，不是 verdict 身份成分）。
  - **resume key 负面清单（定案 D6/D5 红线）**：`--mutation-warning`、`--timeout-factor`、`timeout_streak` **绝不进任何 resume key**——调配置不得引发全库重跑。
  - **v1 目录（`gqt-mutation-resume`）不读不写不删**；在场时 stderr 一次性显式披露。**CI 迁移点**：升级后首个 v2 run 全量重跑（一次性失效，不迁移不脏合并）。**SEE-1463 §SPEC-003**：v1 resume 生产路径（`mutation/resume.py`）已随本批物理删除，其专属用例与 `see1400_resume.feature` 一并退役——resume 落盘仅剩 v2（`resume2.py`）；**v1 目录的在场披露逻辑由 `resume2.disclose_v1_superseded` 保留**（只披露、不读不写不删），老项目 `.qa-cache/` 内遗留 v1 目录仍被一次性披露。
- **timeout_pinned（定案 D5，N=2）**：resume 条目增 `timeout_streak`（连续 timeout 跨 run 计数）。复用位点重判仍 timeout → +1；**streak ≥ 2 → 独立 verdict `timeout_pinned`**：永不并入 killed、**不计入 ok 门禁失败档**、报告持续可见并披露降级来源（failures reason 含 `timeout_pinned after N consecutive timeouts`）；任一后续 run 非超时（survived/killed 均合法）→ 解除清零。**毒 run 守卫**：单 run timeout ≥50% 且 ≥5 条 → 本窗口 timeout 不落 streak，报告披露（stderr `timeout streak suspended: suspected infra run`；守卫窗口=串行整 run / 并发 worker 分片）。
- **budget-on-retry（定案 D6）**：budget 按**差分后 retry 数**计（截断保序，拒轮转）。差分在场 run 与 `--mutate-all` run 同键集披露 `summary.retry_total`（差分 retry 总数）/ `executed`（**实际执行数**——静态预检剔除 invalid_mutant 后真实跑 GUT 的位点数）/ `untouched_ranges`（未触达位点的行号区间列表 `[[a,b],...]`）+ `diff_skipped`（被跳过位点 id 全列；mutate-all 下为空列）。位点数 > budget 时同步披露 `total_sites`（截断前全量）。
- 可选 flag（向后兼容——省略即既有行为，差分除外，见 CI 迁移点）：
  - `--tests <res://glob>`：受影响测试子集（SEE-1312/1400 语义不变）。
  - `--dry-run`：只清点不跑 GUT（零调用契约不变）。**SEE-1420 §18 升级**：manifest 在场时输出差分计数预估 `summary.{diff_available, retry_estimate, executed_estimate}`（无历史时 `diff_available: false` 且 retry_estimate = 全位点数）。
  - `--resume`（SEE-1400 遗留）：语义已被差分默认开启吸收；仅保留 `summary.resumed` 键在场性。store 落盘面已迁移 v2。
  - `--mutation-warning N`（默认 50）：按**截断前**全量位点数判定，超阈值 stderr 告警 + `summary.attention`（建议拆分字符串）。不进任何 resume key。
  - `--timeout-factor F`（默认 2.0）：覆盖内置 `_TIMEOUT_SCALE`，自适应 per-mutant 上限 = `max(timeout, baseline×F+1)`（只放大不收紧；`summary.adaptive_timeout_s` 可查）。不进任何 resume key。
  - `--mutation <file>#<scope_id>#<n>` / `--lines N`（P3 卡尺，单文件）：单点重跑——强制新执行（**绕过差分复用与 coverage 前置过滤**——随机抽查被剔位点防假阴性的公平通道），1 baseline + N selected GUT 调用。无命中 = 显式错误契约（ok=false，`failures[0].reason` 含 id/line），绝不静默空跑。
  - `--reuse-coverage`（P2）：按 `gqt coverage` 最近一次成功 run 的行级事实（`<root>/.qa-cache/gqt-coverage/<key>.json`，provenance 校验：schema/项目内相对路径/源码 sha1/tests_glob 精确匹配/二进制类型——任一不符 = 告警 + **不过滤**继续跑）前置过滤：跳过锚 = **可插桩 ∧ 未覆盖**——`uncovered` 位点（failures 显式标记 `uncovered` verdict、不执行、不计入 ok 门禁、不消耗 budget——过滤先于 budget）；**不可插桩行（成员初始化器/参数默认值/内部类体等探针面外）上的位点一律留在执行面**（修复 Revy 实机假阴性），可插桩集缺失（旧 schema 缓存）= 告警 + 不过滤（宁多跑不假阴性）。coverage 刷新后立即生效（uncovered 不持久化）。
- 中止契约（baseline 本身不可信时，mutant 数据不产出）：

```json
{"tool":"mutation","ok":false,
 "summary":{"file":"...","run_error":true,"error":"..."},
 "failures":[{"reason":"baseline GUT timed out after Ns ..."}]}
```
  注意：该形态下 `summary` 无 `killed` 等键（消费方不要按默认 0 读取）。
- 正常输出：`summary: {file, mutants, killed, survived, timeout, run_errors, kill_rate, budget}` + SEE-1312 `{suspect, invalid_mutants, adaptive_timeout_s}` + SEE-1420 按模式增量 `{resumed, diff_skipped, retry_total, executed, untouched_ranges, total_sites, attention, timeout_pinned, coverage_prefilter, selected}`（仅在对应语义在场时出现）；`failures` = 非 killed 的 mutant 明细，每项增 **`id`** 列（verdict ∈ survived/timeout/run_error/suspect/invalid_mutant/**timeout_pinned**/**uncovered**）。
- **validity 契约**（SEE-1312 + SEE-1420）：`invalid_mutant` = 语法预检失败（不跑 GUT）；`suspect` = baseline 脏 ∧ 差集空；`timeout_pinned` = 跨 run 连续 timeout ≥2 的降级独立档；`uncovered` = reused coverage 判定该行未被测试覆盖（quality 信号，非工具失败）。
- `ok = survived==0 且 timeout==0 且 run_errors==0 且 suspect==0`——`timeout_pinned` 与 `uncovered` 均不计入门禁失败档（pinned=持续可见的存疑档；uncovered=质量信号）；`invalid_mutant` 不参与 ok 判定。
- 变异点 `kind` 枚举（**开放集合，按前缀归类**——消费方过滤 kind 时须容忍未知值）：`AOR` / `ROR` / `UOI` / `boundary` / `TERNARY`（cond_not + arm_swap）/ `GUARD_NOT`（守卫取反；守卫表达式内含可变异算子时去重跳过）/ `LOGICAL`（and⇄or）/ `MEMBERSHIP`（in→not in；`not in` 因 AST 无位置信息暂只支持单向）/ `IS_NOT`（is 取反）。verdict 枚举同为开放集合（容忍未来新增档位）。
- **契约示例（判据命令，可执行）**：

```bash
PYTHONPATH=src python -m godot_qa_toolkit.cli mutation <file>.gd --project-root <root> \
  --mutation-warning 2 --budget 2 --tests res://tests/ --dry-run
# → summary.attention + summary.total_sites（截断前全量）+ retry_estimate

PYTHONPATH=src python -m godot_qa_toolkit.cli mutation <file>.gd --project-root <root> \
  --timeout-factor 3.0 --tests res://tests/
# → summary.adaptive_timeout_s = max(timeout, baseline×3.0+1)

PYTHONPATH=src python -m godot_qa_toolkit.cli mutation <file>.gd --project-root <root> \
  --mutation "<file>#<scope_id>#<n>" --tests res://tests/
# → 1 baseline + 1 mutant；ok/verdict 秒级确认
```

### 4.4 `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout 120] [--tests <res://dir-or-.gd>]`

- 前置与 mutation 相同（GUT + PATH 上的 godot）。
- `--tests`（SEE-1367 §SPEC-017）：受影响测试子集（目录如 `res://tests/unit/time/`，或单脚本路径——走 `-gselect`，GUT 9.6 实测 `-gtest` 会全量展开），替代全量 `-gdir=res://tests/` 以支持逐套件归因度量；省略 = 全量（行为不变）。与 mutation `--tests` 同语义。
- 执行：目标 .gd **AST 语句级插桩**（每可执行语句前插探针调用 + 文件尾部探针函数；语句跨多行时探针只落起始行——SEE-1312 修复跨行分组表达式 parse error；**内部类 class_def 整体不插桩**——探针函数在外层类作用域，内部类方法的 self 无法解析，插进去 = 目标文件整体拒载，SEE-1367 硬化 反例实证）→ 生成期 re-parse 预检 → headless GUT → 读 sink 命中 → 行覆盖%。原文件运行后恢复。
- **capability 契约**（SEE-1312）：
  - 插桩产物不合法 → 生成期即 `run_error`（拒绝落盘跑假数据），`failures[0].reason` 含 `instrumentation produced unparseable source`。
  - 探针数据面为 **ID-keyed**（整数 probe_id → runner 侧 manifest 行号映射）；sink 路径移出数据面。
  - sink：`user://qa-coverage-hits-<pid>.txt`（run 唯一，追加写语义）。runner 按平台标准 user 目录读取（Linux `~/.local/share/godot/app_userdata/<project name>/`，win64 `%APPDATA%\Godot\app_userdata\<project name>\`），并兼容项目根同名文件回退。
- 哨兵（SEE-1312）：
  - GUT 正常退出但探针未点火（sink 缺失/空）→ `ok=false` + `summary.run_error: true` + `probes_fired: 0`——「探针从未点火」与「真实 0% 覆盖」必须可区分。
  - 零可执行行 → `ok=true` + `summary.vacuous: true`（空集覆盖不是满分证据）。
- **comparability 契约**（SEE-1312 分支覆盖 v1 = 纯推导层，observational 不进 ok 判定）：`summary` 新增 `branch_total / branch_covered / branch_coverage_percent`（行探针命中 + AST 分支结构推导；if/elif/else/match case 各计 1 分支）。语义只回答「分支是否被执行」，不回答「是否被断言守护」。
- godot 进程级失败（脚本没加载）≠ 真实 0% 覆盖 → `summary.run_error: true`，与 mutation 同款中止契约。**§SPEC-1420-21 收紧**：中止判定 = `rc≠0 ∧ 无 GUT 测试流证据`（错误 marker 不再是必要条件——godot 加载早期退出无输出无 marker 的形态同样中止，不再降级为 `probes_fired: 0` 哨兵掩盖根因）；项目资源告警在场但 GUT 证据正则命中的正常轮不受影响。
- **行级事实落盘（SEE-1420 P2）**：成功 run（探针点火 >0）额外原子写 `<root>/.qa-cache/gqt-coverage/<key>.json`——stdout 契约的超集（多带全量 `covered_lines` + `instrumentable_lines`（本次 run 的探针可插桩行集）+ provenance：schema `see1420-coverage/1`/项目内相对路径/源码 sha1/tests_glob/binary_kind），供 mutation `--reuse-coverage` 消费；provenance 不符 = 缓存作废重测。stdout 契约零变化。
- 正常输出：`summary: {file, total_lines, covered_lines, coverage_percent, min_percent}` + SEE-1312 新增 `{probes_fired, branch_total, branch_covered, branch_coverage_percent}`；未达标时 `failures[0]` 附 `covered/total/uncovered_lines`（截断 20 行）。
- `ok = coverage_percent >= min_percent`（probes_fired=0 与 run_error 时强制 ok=false）。

### 4.5 `gqt doctor`（SEE-1463 契约登记）

- 用途：自检「editable 安装的 godot-qa-toolkit 是否仍指向当前 worktree gitlink」的安装一致性，供消费方在插件升级/重排后核对。
- 判定对象是**安装一致性**（诊断），非被测项目的判据——因此自检 JSON 打 **stderr**、stdout 恒空，与 §3「各子命令 stdout 恰好一个 JSON」的判据通道分叉（例外理由见 §3）。
- 输出：stderr 一个 JSON（`{"status":"ok"|"mismatch", ...}`，mismatch 时附 `editable_location`/`toolkit_root`/`gitlink_head`/`fix_hint`）。
- exit：一致 → 0；安装错配（self-check 判定 install mismatch）→ **exit 2**，stdout 无 JSON（§5 的「调用方/环境错」档，不判被测对象）。

## 5. exit code 语义

| exit | 语义 | 触发 |
|---|---|---|
| 0 | 判定通过（`ok=true`） | 任意子命令 |
| 1 | 判定失败（`ok=false`，stdout 有 JSON） | verdict fail / run_error 中止 / 目标文件或 GUT runner 不存在 |
| 2 | 用法/装载错误，**stdout 无 JSON** | argparse 参数错误；`gherkin --steps` 模块加载失败（stderr 报 `cannot load steps module`）；`doctor` 安装错配（自检 JSON 打 stderr、stdout 恒空） |

约定：exit 2 是「调用方自己错了」，exit 1 是「被测对象判为失败」。消费方自动化只应区分 0 与非 0，并解析 stdout JSON 细分 1 的形态。

## 6. `--project-root` 边界约定

- 必填于 `mutation` / `coverage`；必须指向含 `addons/gut/gut_cmdln.gd` 的 Godot 项目根。
- 工具在该 root 下运行 `godot --headless --path <root> -s res://addons/gut/gut_cmdln.gd [-gdir=<tests_glob>] -gexit`（`-s` 必须是 res:// 形式，绝对路径会被 Godot 拒绝加载——见 mutation/runner.py 注释）。
- 工具可能写入的路径仅限：目标 .gd（临时改写、必然恢复）、coverage 探针 sink（`user://qa-coverage-hits-<pid>.txt`，SEE-1312 起为 user:// 优先 + run 唯一；兼容读项目根同名文件），以及 `<root>/.qa-cache/` 下的工具自有缓存（gqt-timing 预算锚、gqt-mutation-resume-v2 断点续跑（manifest 索引层 + per-id 条目）、gqt-mutation-resume（v1，代码已随 SEE-1463 删除；目录不读不写不删，在场由 resume2 一次性披露）、gqt-coverage 行级覆盖事实（SEE-1420 P2）、gqt-godot-slots 并发闸锁文件——均自含 .gitignore）。
- **生成类持久写入档（SEE-1426，Owner 批准的 §6 首次扩张）**：`gqt accept compile --write` 可写 `<root>/acceptance/ir/` 与 `<root>/acceptance/generated/` 两目录（gitignored 一次性产物；先清空再生成；写入 opt-in，`summary.wrote` 逐文件披露；写入前置校验 `<root>/addons/gut/gut_cmdln.gd` 在场）。除此之外零写入。
- timeout 语义：单次 GUT 全套测试的 wall-clock 上限；被测项目基线超过 timeout 时 mutation 返回 run_error 中止（数据不可信优于慢数据）。SEE-1312 起 mutation per-mutant 上限自适应 = max(显式 timeout, baseline 实测耗时 × F)（SEE-1420 起 F 可由 `--timeout-factor` 覆盖，默认 2.0 不变；只放大不收紧，`summary.adaptive_timeout_s` 可查）。SEE-1400 起全部 godot 子进程（mutation baseline/per-mutant 与 coverage）经跨进程 flock 槽位闸（容量 env `GQT_GLOBAL_GODOT_SLOTS`，缺省 max(2,cpu//2)；等槽时间不计入 timeout；fcntl 缺失平台告警一次后放行不阻塞）。

## 7. 契约增量分类（SEE-1312 plan-debate 定案：四类归并）

本 issue 的全部契约变更按四类归并，**均为加法、向后兼容**（消费方只解析已声明字段则零破坏）：

| 类别 | 内容 |
|---|---|
| **validity**（有效性） | mutation `invalid_mutant` verdict（语法预检）；coverage 生成期 re-parse 预检 run_error；`suspect` 标签（baseline 脏 ∧ 差集空） |
| **classification**（分类） | mutant `kind` 开放枚举扩展（TERNARY/GUARD_NOT/LOGICAL/MEMBERSHIP/IS_NOT）；`summary.suspect` / `summary.invalid_mutants` 计数 |
| **capability**（能力） | coverage AST 语句级插桩 + ID-keyed 数据面 + user:// run 唯一 sink；`probes_fired` / `vacuous` 哨兵；mutation `--dry-run` / `--tests` / 自适应 timeout（`adaptive_timeout_s`） |
| **comparability**（可比性） | coverage `branch_total` / `branch_covered` / `branch_coverage_percent`（v1 推导层，observational——不进 ok 判定；语义=「分支被执行」非「被断言守护」） |

## 8. deferred 登记（SEE-1463 §SPEC-004）

以下模块当前**生产零调用方**，状态显式为 **deferred**——未接线进任何判据
报告链，消费方不得预期其输出出现在任一子命令 `summary` 中：

| 模块 | 现状（唯一进入方式） | deferred 触发条件（演进方向） |
|---|---|---|
| `fsprobe.py`（FsBaselineStore / probe_environment / recommend_projection） | 零写盘、只读探测；本批不接线 | 待 mutation 报告链决策是否采用 fs 启动税投影时接线——届时将其 `recommend_projection` 输出并入报告的 `summary.fs_projection`，并在 cli/collector 层落地，需配套基线 store 与运行期探测的成套新功能 |
| `mutation/gdself_stats.py` | 仅 `python -m godot_qa_toolkit.mutation.gdself_stats --project-root <KOL>` 主动执行的一次性诊断脚本 | 无日常接线义务；gdself 正向索引立项且需定期量化收益时重估为周期工具 |

已撤回的误导性承诺：fsprobe docstring 原称「报告 `summary.fs_projection` 形态」，
上文（§4.3 mutation 正常输出）**不包含** `fs_projection` 键——以本条为准，该接口属后续
演进，不属于当前契约。
