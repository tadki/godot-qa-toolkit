# gqt 接口契约（AC-DECPL-005）

> 权威来源：`src/godot_qa_toolkit/cli.py`（SEE-1268 M1/M2 落地）。本文档化机器可判的输入/输出约定，消费方（agent、CI、其他脚本）以此为准。实现与本文冲突时以代码为准并回改本文档。

## 1. 插件定位声明

godot-qa-toolkit 是一个**Godot 项目专用的确定性 QA 测试工具插件**（工具链：Gherkin acceptance pipeline、complexity gate、mutation runner、coverage runner、arch guard、doctor 自检）。

> **gherkin 命令退役注记（SEE-1468 M5-b）**：独立 `gherkin` runner / expectations / cli 已物理删除（parser 晋升为 `accept.parser`，判定面迁入 `gqt accept` 与 pytest-bdd）。本契约不再含 `gherkin` 子命令；原 gherkin 场景判定能力由 `gqt accept compile/run/mutate`（见 §4.2a-c）承接。

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

六个子命令：`accept` / `arch` / `complexity` / `mutation` / `coverage` / `doctor`（SEE-1426 起 doctor 随自检能力加入）。原第七个子命令 `gherkin` 已于 SEE-1468 M5-b 物理退役（判定能力由 `gqt accept` 承接，见 §1 退役注记）。

## 3. 统一输出 JSON 契约

所有子命令在 **stdout** 输出恰好一个 JSON 对象（`ensure_ascii=False`，缩进 2），带换行结尾：

> **doctor 例外**（SEE-1463 登记）：`gqt doctor` 自检 JSON 打 **stderr**（不产出判据 JSON），stdout 恒空——它与其它子命令不同，不判被测对象而是判定「editable 安装 vs 当前 worktree gitlink」的安装一致性，结果属于诊断而非判据；错配时 exit 2（见 §5）。

```json
{
  "tool": "<accept|arch|complexity|mutation|coverage>",
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

### 4.2b `gqt accept run <dir> --project-root <root> --key <feature-key> [--timeout 120] [--godot-bin <path>]`（SEE-1426 P2）

- 执行前三件哈希绑定重放校验：manifest 的 ir_hash/generated_hash 与磁盘实际 sha256 必须一致，任一失配 = run_error（`summary.run_error: true`，无告警放行档）；GUT runner 缺失同理。**重锚防线（§SPEC-1426-24）**：run 侧把 manifest.source_path 指向的源 `.feature` 现场重编译核对 IR 哈希一致——源文件缺失或漂移 = run_error。**tests_executed 守卫（§SPEC-1426-24）**：GUT 输出零测试流而 exit 0 = 判定失败（`summary.tests_executed: 0` + 显式 failure，不是绿）。
- 执行：一次性 `godot --headless` 子进程跑 GUT（`-gdir=res://acceptance/generated`），跨进程槽位闸包裹（复用 mutation 的 GlobalGodotGate）。
- 输出：统一契约回译——失败测试名经 manifest attribution 对回 scenario/line，失败 reason 附 GUT `[Failed]` 断言明细（§SPEC-1426-25，含 unchanged slot 违规文本）；无法归因的失败显式披露（`scenario: null` + `not attributable` reason）。`summary.{replay_checked, ir_hash, exit_code, tests_executed, wrote}`。

### 4.2c `gqt accept mutate <dir> --project-root <root> --key <feature-key> [--timeout 120] [--worker] [--godot-bin <path>]`（SEE-1426 P3）

- 全量契约变异闭环（v1 无 stamp，每轮全量、无增量跳过）：对 IR 全量产 mutants → 逐 mutant 写临时变异 IR（磁盘 IR 每轮后恢复原样）→ headless 跑同一套生成测试 → 挂 = kill；活 = survived 即打回（`ok=false` + survived 明细）。
- 变异矩阵（§SPEC-1426-11；/2 扩展）：数值 ±1 + 边界外推（0→-1）、布尔翻转、枚举声明域内邻值切换（域外从不生成；域未声明的 Then 绑定在 parse 期已被拒）、`float_near:<tol>` ±max(1, 2×tol)、`not_contains` **断言语义翻转**（not_contains → contains：载荷不变，mutant 经 `case.mutated_not_contains` 通道翻转发该 cell 断言极性，harness 按契约消费——基线绿 ⇒ actual 真不含 needle ⇒ 翻转断言必假 = 确定性 kill，与检查惯用法无关；QA F1 裁定）、`in_set` 退化坍缩集（模板 `split(",", false)` 下 `"" → []` → 必挂）、`signal_count` ±1；**`str` 零变异 = 默认豁免**（Owner 裁决 3：启用 str ±1 须 per-suite 显式声明，未启用前「str 恒豁免」为正常结局）；unchanged 槽纳入矩阵（基线污染探针）。
- **豁免轮（/2，exempt ≠ skip）**：`# mutation: exempt` 场景的 typed 矩阵不产 mutants，但其 case 在 mutate 侧执行一次未变异基线轮（先于 mutant 轮）：绿 = 逐 case `exempt_verified`、挂 = 逐 case run_error 计入门禁；未变异基线任何 case 挂 = fail-fast 跳过 mutant 轮（`summary.exempt_baseline_failed: true`）。纯豁免特性（零 mutants + 有豁免 case）走基线轮收口，不触发 `no mutants generated`。
- 三层防伪（§SPEC-1426-12）：① 变异器对抗性金测（70/71 fixture 恰好 ±1/边界/邻值，单测面）；② 哨兵核对——GUT 输出零测试流 = 执行链失真 = run_error 面失败；③ unchanged 槽变异必挂（挂不掉 = survived 打回）。零 Then 绑定且零 unchanged 且零豁免的 IR = run_error（`no mutants generated`）。
- `--worker`（§SPEC-1426-14）：opt-in **真常驻通道**——compile 产出的 worker 生成物（`acceptance/generated/accept_worker.gd`，受先清空再生成管辖 + `worker_hash` 哈希绑定）以单个 godot 进程服务全矩阵：stdin NDJSON 请求行（`{mutant_id, ir_file}` 小载荷，恒 <1024B——`OS.read_string_from_stdin()` 行缓冲上限 1024B，QA-1 实证）+ 变异 IR 经 `.qa-cache/gqt-accept-worker/` 轮转临时文件（FileAccess 读，零行缓冲限制，用后即删、通道结束清目录）；每 mutant 结束硬纪律 = 释放全部 testing_api 实例 + 重建（reload）+ 自检探针断言复位，不符即 run_error verdict。读侧带超时（复用 `--timeout` 为每轮 verdict 等待上限，超时 = _WorkerDead → 剩余 mutant 记 run_error，MEDIUM-1 修复）。验收 = batch 与 worker 两条真实代码路径的差分对照 verdict 一致性（实机口径：IR ≥1678B 量级、非挂死、ok/killed/survived 三项相等）。
- 输出：`summary.{feature, feature_key, ops_version, mutants, killed, survived, run_errors, full_matrix, worker, exempt_cases, exempt_results}`；`failures` = survived（含 target/kind/original/mutated/rationale）+ run_errors。`exempt_cases` == 源 .feature 内 `# mutation: exempt` 指令行数（V4 机器判据），`exempt_results` 逐 case 具名披露（`exempt_verified` / `run_error: ...`）。

- **W 通道（§SPEC-020，worker-reset/1，A9 opt-in；D-W1/D-W2 修复轮修订）**：`--worker-channel`（或 env `GQT_MUTATION_WORKER=1`）显式启用常驻 GUT 直驱 worker——旗标/env 在**全部执行路径**（CLI 串行、executor 自动退串行、并行分片、零执行收尾）一致生效，CLI 旗标缺席映射 None（env 决定），默认仍为冷通道。静态路由判 worker 的目标（非 autoload/接线链/GUT 域；引用图含 **class_name 裸名使用边**）在每 run 单进程内逐 mutant 执行 R-C 硬序（IGNORE 原位刷码 → LIFO teardown → 逐闭包 reload（.gd 两步；场景/资源成员经**旁路加载 + `take_over_path`** 逐腿刷新——PackedScene 缓存持有加载时脚本对象）→ 顺序门 → fresh Gut 直驱（**error_tracker 注册对齐 GutRunner**，headless pause 跳过）→ beacon）。等价性门（D-W2）：① **parity 前置门**——会话首腿以原始源执行，直驱失败集必须逐位等于冷 baseline 失败集，失配 → stderr 披露 + 整文件冷降级；② **状态漂移门**——连接指纹 / `user://` 树（每腿清场）相对首腿基线漂移 → 会话致命；③ **canary 失配 = 通道资格否证**——窗口改冷权威 + 剩余全部冷降级（不再作废窗口后继续 worker）；会话致命（闭包任一 reload rc≠0 / loaded-source 哈希门失配 / autoload 交叠 / 腿挂死）仍走 respawn + 自上次 canary 窗口整批作废 + 冷重跑 + stderr 披露。summary 增 `exec_scheme`（`cold-shadow/1` vs `worker-reset/1`，只加不改；并行合并面 passthrough 同步在场），killed 证书按 scheme 隔离（身份包入 file_key）。canary 粒度 env `GQT_WORKER_CANARY_EVERY`（缺省 25：首 mutant 强制双通道预检）。
### 4.3 `gqt mutation <file.gd> --project-root <root> [--budget 50] [--timeout 60] [--tests <res://glob>] [--dry-run] [--resume] [--mutation-warning N] [--timeout-factor F] [--mutate-all] [--mutation <id>] [--lines N] [--reuse-coverage] [--no-reuse-coverage] [--godot-bin <path>]`

- **磁盘影子注入（SEE-1550 M0，OPS `see1550/1` 一次性失效）**：注入机制由内存注入改为磁盘影子注入——每 worker 持有隔离 hardlink COW 影子槽（无硬链接 FS 退真拷贝；`.godot` 每槽整树实拷贝；建槽前置先写 `.qa-cache/.gitignore` 为 `*`，失败 fail-loud），mutant 经「临时文件 + `os.replace`」于引擎启动前写入影子内目标 `.gd`（每 mutant 一次 replace、无恢复步；槽归还还原目标并断言 ==原码）；真实仓目标文件全程零写入。二进制分域影子根：Linux ELF → ext4 临时农场（每 run 新建）；win64 wrapper → 项目 `.qa-cache/mutation-pool/` 同盘根（持久 warm 池，分层新鲜度权威 + flock + LRU/预算 prune）。四级注入可达性 beacon（磁盘读回哈希 → load 编译 → autoload 同源 → GUT 运行证据）：哈希/实例不符 → `run_error`；load-null/parse → `invalid_mutant`；无 GUT 运行流不得 killed/survived。现行内存注入生产路径已物理删除（`--injection` 开关不存在）。旧 resume/coverage store（OPS `see1420/1`）不读不写不删，stderr 一次性披露，首跑全量重跑（Owner 裁决菜单 B）。
- **缓存身份包（SEE-1550）**：resume file-key 增身份包成分——`inject_scheme`（`disk-farm/1`）、精确 Godot 版本串（`--version`，按二进制 path+mtime 缓存；不可解析按路径界定）、boot/wiring hash（规范化 `project.godot` + autoload/ 清单 + tests 引用 `.tscn` 清单）、GUT addon 内容哈希。任一漂移 = 旧 key 永不再命中（store 不迁移不脏合并）。coverage store 同步入档（见 `--reuse-coverage`）。
- **killed 证书（SEE-1550）**：killed 条目携带 `killed_by = [{test_name, script_path, script_sha1, deps_sha1}]` **完整失败集**（无截断；归属由 tests 树 func 定义扫描解析）。差分复用门：全部 killer 测试仍在且自身/依赖闭包哈希一致 → 复用；killer 改/删/依赖变 → **仅该 entry 重跑**（trigger `killer_changed`）；证书缺失/解析不确定 → 保守重跑（trigger `cert_missing`）；**新增测试不失效旧 killed**（校验只碰 killer 闭包，非整树指纹）。summary 增 `killed_by_certificates`（id → 完整失败集证书）披露。
- **疑似孤儿披露（SEE-1550，unreachable 不进 verdict）**：目标脚本零静态引用（autoload 接线 / `.tscn` ext_resource / tests 字面量 / 其他脚本引用全无）∧ 存在 survived → 幸存记录逐条 `suspected_orphan: true` + `summary.suspected_orphans` 名单。静态不可判定面（动态 load / uid 间接引用）只披露、不判定、**不 skip**（位点照常执行）。
- **summary 机制披露（SEE-1550，只加不改）**：`inject_scheme`（恒在场，含 dry-run）、`slot_mode`（`fresh`|`warm`；有槽执行语义时在场）、`freshness`（分层权威逐层结论，同上条件在场）。`killed_by_certificates`（有 killed 证书时）与 `suspected_orphans`（孤儿诊断 ∧ 存在 survived 时）为按语义在场的增量键，见「正常输出」。
- **coverage prefilter 自动启用（SEE-1550 L1）**：`--reuse-coverage` 语义升级为**默认自动启用**——store fresh ∧ 身份包全匹配才生效，任一漂移禁用并 stderr 披露；uncovered 不进 ok 门禁、不跑 GUT（既有语义不变）。新增 `--no-reuse-coverage` 显式关断；旧 `--reuse-coverage` 旗标继续接受（与默认同义）。
- **win64 warm 池 / linux fresh 农场（SEE-1550 L1）**：win64 通道槽持久复用（冷首建一次，每 run `git status --porcelain` delta 同步 + 定向重链，慢 delta ≥10s stderr 披露）；linux 通道每 run fresh 农场新建（release 即删）。并发 gqt 经池账本 flock + 每槽 flock 串行化；进程死亡（SIGKILL）残留 mutant 由下轮定向三向（repo stat vs 槽 stat vs 记录）捕获并重链修正。`.qa-cache` 池受槽数上限（8）与磁盘预算（`GQT_MUTATION_POOL_BUDGET_MB`，缺省 512）LRU/prune。全量 census 仅 `GQT_MUTATION_FULL_CENSUS=1` 显式触发；单点/卡尺 run（`--mutation`/`--lines`）走定向三向、跳过全量 delta 同步。
- **§SPEC-010 基准**：`benchmarks/bench_ab_see1550.py`（A=master 旧机制 worktree vs B=新机制，A/A ≥3 轮噪声带 → A/B 交错 ≥3 轮，verdict/kill-set 一致性 + 墙钟门 + JSON 报告）——QA 阶段实机执行留档。
- **默认并行（SEE-1413）**：`--jobs` 缺省时单文件同样走并发 per-mutant 切片路径（默认 jobs = 上限 `max(2, cpu//2)`，复用 executor.default_jobs）。缺省单文件 CLI 顶层契约保持既有 flat 形态（`{"tool","ok","summary","failures"}`），`summary` 新增 `jobs` 键（串行=1，并行=该文件实际分片数）——只增不减，既有键集零变化；多文件及显式 `--jobs > 1` 的 `{"tool","ok","jobs","results"}` 编排信封不变，其 `results[i].summary.jobs` 同样记录该文件实际分片数。dry-run 的每文件 `jobs=1`；无 chunk 的 no-sites/setup-error settled 记录为 `jobs=0`。
- **自动退串行（SEE-1413）**：单文件变异点总数 ≤ 生效 jobs 时自动走既有串行路径（判定在变异点收集后、任何 GUT / prewarm / 共享 baseline 预跑之前——少变异场景并发固定开销倒挂），报告形态与串行一致（`jobs=1`）。显式 `--jobs 1` 仍强制串行。`--jobs` 帮助文本已修正为 per-file AND per-mutant slicing（单文件同样生效）。
- **timing store 语义（SEE-1413）**：退串行路径与 `--jobs 1` 串行快路径接线 TimingStore（预算协商/采样不丢）；并发分片路径不接线（worker 分片跳过 store 读写是 SEE-1356 L1 既有契约，adaptive timeout 只放大不收紧）。

- 前置：`<root>/addons/gut/gut_cmdln.gd` 必须存在；目标 godot 二进制必须可解析（见 §6 Godot 二进制定位）。缺失任一 → 统一 JSON failure（exit 1，`failures[0].reason` 含 `not found`）。
- 执行：baseline GUT（原文件，**永远实测**）→ 差分规划 → 逐 mutant 磁盘影子注入跑 GUT（mutant 写入影子槽内目标 `.gd`，真实仓目标文件零写入——机制见上 M0 条；旧内存注入生产路径已物理删除）。
- **kill 判定 = 失败测试名集合相对 baseline 的差集**，不看 exit code——被测项目基线在无头环境可能自带 pre-existing failures，按 rc 判定会全部误杀。
- **差分默认开启（SEE-1420 定案 D4，行为变更）**：v2 resume manifest 在场即差分——未改 scope 且上次 killed/suspect/invalid_mutant → 复用跳过；survived / timeout / timeout_pinned / run_error → **永远重跑**（survived 永不缓存信任——红线）。`--mutate-all` 显式退路（全位点执行）。**CI 迁移点**：升级后首跑全量重跑一次（见下 resume v2）；此后 CI 红绿面由「每次全量」变为「差分 retry 面」——survived 位点仍每轮重跑，红不会因缓存变绿。
- **scope manifest / 稳定 id（定案 D1/D2/D3）**：scope 集 = 顶层 func + 内部 class 整体黑盒 + module（非 func 顶层声明）；header hash 只收非 func 顶层声明、func 变更绝不触发全文件重跑（单向保守）；无位点 scope 不进 manifest。位点 id = `<file>#<scope_id>#<n>`（scope_id = `func:`/`class:` kind 前缀 + 语义名，同 kind 同名消歧 `func:foo#2`——跨 kind 同名 func/class 不碰撞（Final Review MEDIUM-2）；n 为 scope 内 1-based 扫描序），**id 仅为寻址提示**——复用前必过全签名 (line, column, kind, original, mutated) + scope hash 双校验，任一漂移 = stderr 告警 + 拒绝复用 + 降级重跑，绝不静默按 id 取条目。
- **`# gqt: no-mutate`（定案 L6）**：注释过滤。marker 在顶层 func 定义行（含行尾注释）或其紧邻上方 → 函数级（抑制至下一顶层声明）；其余位置行级（仅 marker 所在行）。被过滤位点不产 mutant、其 scope 不进 manifest。
- **resume v2 store（一次 schema bump，OPS_VERSION `see1420/1`）**：
  - 落盘：`<root>/.qa-cache/gqt-mutation-resume-v2/<file-key>/manifest.json`（per-file 索引层：schema/header hash/含位点 scope hash 表；**收尾方单点写**）+ `<file-key>/<stable-id>.json`（per-mutant 条目：verdict + 全签名五元组 + scope_hash + `timeout_streak`；tmp+os.replace 原子写，各 worker 各写各 id，无跨 worker 竞态）。
  - file-key = sha1(项目内相对路径 | tests_glob | OPS_VERSION | binary_kind | 身份包摘要)（SEE-1550 起增身份包成分，见上「缓存身份包」条）。**无 budget**（D6：预算是每 run 执行上限，不是 verdict 身份成分）。
  - **resume key 负面清单（定案 D6/D5 红线）**：`--mutation-warning`、`--timeout-factor`、`timeout_streak` **绝不进任何 resume key**——调配置不得引发全库重跑。
  - **v1 目录（`gqt-mutation-resume`）不读不写不删**；在场时 stderr 一次性显式披露。**CI 迁移点**：升级后首个 v2 run 全量重跑（一次性失效，不迁移不脏合并；SEE-1550 OPS `see1550/1` bump 再度一次性失效一次，见 M0 条）。**SEE-1463 §SPEC-003**：v1 resume 生产路径（`mutation/resume.py`）已随本批物理删除，其专属用例与 `see1400_resume.feature` 一并退役——resume 落盘仅剩 v2（`resume2.py`）；**v1 目录的在场披露逻辑由 `resume2.disclose_v1_superseded` 保留**（只披露、不读不写不删），老项目 `.qa-cache/` 内遗留 v1 目录仍被一次性披露。
- **timeout_pinned（定案 D5，N=2）**：resume 条目增 `timeout_streak`（连续 timeout 跨 run 计数）。复用位点重判仍 timeout → +1；**streak ≥ 2 → 独立 verdict `timeout_pinned`**：永不并入 killed、**不计入 ok 门禁失败档**、报告持续可见并披露降级来源（failures reason 含 `timeout_pinned after N consecutive timeouts`）；任一后续 run 非超时（survived/killed 均合法）→ 解除清零。**毒 run 守卫**：单 run timeout ≥50% 且 ≥5 条 → 本窗口 timeout 不落 streak，报告披露（stderr `timeout streak suspended: suspected infra run`；守卫窗口=串行整 run / 并发 worker 分片）。
- **budget-on-retry（定案 D6）**：budget 按**差分后 retry 数**计（截断保序，拒轮转）。差分在场 run 与 `--mutate-all` run 同键集披露 `summary.retry_total`（差分 retry 总数）/ `executed`（**实际执行数**——静态预检剔除 invalid_mutant 后真实跑 GUT 的位点数）/ `untouched_ranges`（未触达位点的行号区间列表 `[[a,b],...]`）+ `diff_skipped`（被跳过位点 id 全列；mutate-all 下为空列）。位点数 > budget 时同步披露 `total_sites`（截断前全量）。
- 可选 flag（向后兼容——省略即既有行为，差分除外，见 CI 迁移点）：
  - `--tests <res://glob>`：受影响测试子集（SEE-1312/1400 语义不变）。
  - `--dry-run`：只清点不跑 GUT（零调用契约不变）。**SEE-1420 §18 升级**：manifest 在场时输出差分计数预估 `summary.{diff_available, retry_estimate, executed_estimate}`（无历史时 `diff_available: false` 且 retry_estimate = 全位点数）。
  - `--resume`（SEE-1400 遗留）：语义已被差分默认开启吸收；仅保留 `summary.resumed` 键在场性。store 落盘面已迁移 v2。
  - `--mutation-warning N`（默认 50）：按**截断前**全量位点数判定，超阈值 stderr 告警 + `summary.attention`（建议拆分字符串）。不进任何 resume key。
  - `--timeout-factor F`（默认 2.0）：覆盖内置 `_TIMEOUT_SCALE`，自适应 per-mutant 上限 = `max(timeout, baseline×F+1)`（只放大不收紧；`summary.adaptive_timeout_s` 可查）。不进任何 resume key。
  - `--mutation <file>#<scope_id>#<n>` / `--lines N`（P3 卡尺，单文件）：单点重跑——强制新执行（**绕过差分复用与 coverage 前置过滤**——随机抽查被剔位点防假阴性的公平通道），1 baseline + N selected GUT 调用。无命中 = 显式错误契约（ok=false，`failures[0].reason` 含 id/line），绝不静默空跑。
  - `--reuse-coverage`（P2；SEE-1550 L1 起默认自动启用、`--no-reuse-coverage` 显式关断——见上「coverage prefilter 自动启用」条）：按 `gqt coverage` 最近一次成功 run 的行级事实（`<root>/.qa-cache/gqt-coverage/<key>.json`，provenance 校验：schema/项目内相对路径/源码 sha1/tests_glob 精确匹配/二进制类型/**身份包全匹配**（SEE-1550；旧 schema 无 identity 键 = 漂移）——任一不符 = 告警 + **不过滤**继续跑）前置过滤：跳过锚 = **可插桩 ∧ 未覆盖**——`uncovered` 位点（failures 显式标记 `uncovered` verdict、不执行、不计入 ok 门禁、不消耗 budget——过滤先于 budget）；**不可插桩行（成员初始化器/参数默认值/内部类体等探针面外）上的位点一律留在执行面**（修复 Revy 实机假阴性），可插桩集缺失（旧 schema 缓存）= 告警 + 不过滤（宁多跑不假阴性）。coverage 刷新后立即生效（uncovered 不持久化）。
- **运行进度面（SEE-1525，非判据面）**：每个变异体判定完成时向 **stderr** 输出一行 `[<done>/<total>] <verdict> L<line> <stable_id> <elapsed>s`（并行分片追加 ` worker=wN`，基线降级串行分片追加 ` worker=serial`；`total` 为整 run 的执行面，非单 worker 切片——静态 invalid 位点计入分母也计入 done，故每个 worker 都能走到 `[n/n]`）——串行与并行路径同一计数点，**stdout 恒为恰好一个 JSON**（进度绝不进 stdout）。机器可读面同步原子更新：`<root>/.qa-cache/gqt-mutation-progress/<run-id>/<file-slug>.<worker>.<pid>.json`，字段 `done / total / killed / survived / timeout / run_errors / suspect / invalid_mutants / elapsed_s / started_at / updated_at`。分片按 worker 进程私有（文件名含 worker 槽位与 pid，各写各的路径；写盘 = 同目录 tmp + `os.replace`），聚合只在读侧发生（`godot_qa_toolkit.mutation.progress.read_progress(root[, file])`：按 file 分组，计数求和、`total`/`elapsed_s` 取 max、时间取极值，只取 `updated_at` 最新的 run）——**无共享写者、无文件锁、无读-改-写，故无并行写竞争**。写失败（只读盘 / `.qa-cache` 被占 / Windows 上读者持有目标文件）一次 stderr 告警后放弃进度面；进度面不进 `summary`、不参与 ok 门禁、不影响判定。
- **基线快照失败降级告警（SEE-1525）**：父层共享 baseline 预跑失败（异常 / 非零 rc）→ 该文件整卷退单 worker 串行（既有兜底语义不变），并向 stderr 发 `mutation: baseline snapshot failed for <file>: <reason> — this file falls back to serial single-worker execution (continuing)`；**只披露、不失败、不改变判定**。`tests_glob` 未解析（全量面不预跑 baseline）不在此列。
- 中止契约（baseline 本身不可信时，mutant 数据不产出）：

```json
{"tool":"mutation","ok":false,
 "summary":{"file":"...","run_error":true,"error":"..."},
 "failures":[{"reason":"baseline GUT timed out after Ns ..."}]}
```
  注意：该形态下 `summary` 无 `killed` 等键（消费方不要按默认 0 读取）。
- 正常输出：`summary: {file, mutants, killed, survived, timeout, run_errors, kill_rate, budget}` + SEE-1312 `{suspect, invalid_mutants, adaptive_timeout_s}` + SEE-1420 按模式增量 `{resumed, diff_skipped, retry_total, executed, untouched_ranges, total_sites, attention, timeout_pinned, coverage_prefilter, selected}`（仅在对应语义在场时出现）+ SEE-1550 按模式增量 `{inject_scheme, slot_mode, freshness, killed_by_certificates, suspected_orphans}`（条件在场性见上「summary 机制披露」条）；`failures` = 非 killed 的 mutant 明细，每项增 **`id`** 列（verdict ∈ survived/timeout/run_error/suspect/invalid_mutant/**timeout_pinned**/**uncovered**；survived 记录在疑似孤儿诊断命中时另带 `suspected_orphan: true`）。
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

### 4.4 `gqt coverage <file.gd> --project-root <root> [--min-percent 80] [--timeout 120] [--tests <res://dir-or-.gd>] [--godot-bin <path>]`

- 前置与 mutation 相同（GUT + 同源 Godot 二进制定位，见 §6；SEE-1525 起 `GQT_GODOT_BIN` / `--godot-bin` 对 coverage 同样生效，未设变量行为与 SEE-1525 前逐字节一致）。
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
- **行级事实落盘（SEE-1420 P2）**：成功 run（探针点火 >0）额外原子写 `<root>/.qa-cache/gqt-coverage/<key>.json`——stdout 契约的超集（多带全量 `covered_lines` + `instrumentable_lines`（本次 run 的探针可插桩行集）+ provenance：schema `see1420-coverage/1`/项目内相对路径/源码 sha1/tests_glob/binary_kind），供 mutation `--reuse-coverage` 消费；provenance 不符 = 缓存作废重测。**SEE-1550 L1**：provenance 增**缓存身份包**（`identity`，成分见 §4.3）——mutation prefilter 读回要求 store fresh ∧ 身份包全匹配，任一漂移（含旧 schema 无 identity 键）= prefilter 禁用并 stderr 披露。stdout 契约零变化。
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
| 2 | 用法/装载错误，**stdout 无 JSON** | argparse 参数错误；`doctor` 安装错配（自检 JSON 打 stderr、stdout 恒空） |

约定：exit 2 是「调用方自己错了」，exit 1 是「被测对象判为失败」。消费方自动化只应区分 0 与非 0，并解析 stdout JSON 细分 1 的形态。

## 6. `--project-root` 边界约定

- 必填于 `mutation` / `coverage`；必须指向含 `addons/gut/gut_cmdln.gd` 的 Godot 项目根。
- **Godot 二进制定位（SEE-1525）**：三级优先 = `--godot-bin <path>`（命令级覆盖，`mutation` / `coverage` / `accept run|mutate` 同语义）> 环境变量 `GQT_GODOT_BIN` > `PATH` 首匹配（均无 → 字面量 `godot`）。显式路径（env 或 flag）不存在/不可读/不可执行 → `BinaryConfigError` → 统一 JSON 错误契约（`ok:false`，消息含来源标签与路径）。**`--path` 形态随二进制类型同源**（`detect_binary_kind()` 判定）：win64 通道 `/mnt/` 绝对路径经 `wslpath -w` 转 Windows 形式（不可转换原样），linux 通道一律原生形态不转换——转换原语单一出处（`mutation/inject.py`），mutation 与 coverage 共用。**未设变量且未传 flag 时全部子命令行为与 SEE-1525 前逐字节一致（opt-in 冻结）**。
- 工具在该 root 下运行 `<godot> --headless --path <root> -s res://addons/gut/gut_cmdln.gd [-gdir=<tests_glob>] -gexit`（`<godot>` 为上述解析结果；`-s` 必须是 res:// 形式，绝对路径会被 Godot 拒绝加载——见 mutation/runner.py 注释）。
- 工具可能写入的路径仅限：coverage 插桩的临时改写目标 .gd（运行后必然恢复；**mutation 侧 SEE-1550 起零写入真实仓**——mutant 只写影子槽内目标，见 §4.3 M0 条）、coverage 探针 sink（`user://qa-coverage-hits-<pid>.txt`，SEE-1312 起为 user:// 优先 + run 唯一；兼容读项目根同名文件）、项目外 Linux 影子农场（`gqt-mutation-farm`，XDG/tmp 下 ext4，每 run 新建、归还即删），以及 `<root>/.qa-cache/` 下的工具自有缓存（gqt-timing 预算锚、gqt-mutation-resume-v2 断点续跑（manifest 索引层 + per-id 条目；SEE-1550 起 file-key 含身份包成分）、**mutation-pool win64 持久 warm 池**（SEE-1550，池 schema `see1550-pool/1`：槽位 hardlink COW 影子树 + `.godot` 实拷贝 + 池账本/每槽 flock；槽数上限 8、磁盘预算 `GQT_MUTATION_POOL_BUDGET_MB`（缺省 512）LRU/prune）、gqt-mutation-resume（v1，代码已随 SEE-1463 删除；目录不读不写不删，在场由 resume2 一次性披露）、gqt-coverage 行级覆盖事实（SEE-1420 P2；SEE-1550 起含身份包）、gqt-godot-slots 并发闸锁文件、gqt-mutation-progress 运行进度分片（SEE-1525，per-worker 分片 + 读侧聚合，见 §4.3）——均自含 .gitignore；建槽前置先写 `.qa-cache/.gitignore` 为 `*`，失败 fail-loud）。
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

## 9. parity / registry 契约（SEE-1546 WP1b-PR1，diagnostic-only）

三个新面，全部 **diagnostic-only**：`ok` 只表示命令完整执行，车道异常是数据不是 exit code——机器判定交给 jq 门；不接 CI、不设阈值、无均分（plan §4.1 口径）。

### 9.1 `gqt parity <paths...> --project-root <root> [--report parity.json] [--registry-out reg.json]`

全量逐函数 CC parity（`parity/1`）。oracle = registry func_def 条目（原始 AST 决策点计数，radon 记录绝不当 oracle）；radon 记录来自与 collector 完全相同的 convert+sanitize 管线。配对 = 文件内按裸名分组、**出现序 1:1**（radon 记录按 lineno 排序恢复源码序——fixture 实证 cc_visit 输出序在有 class 时非源码序）；同名计数不符时整组 `identity_mismatch`，绝不折叠。

七车道：`match`（忠实）/ `lambda_gap`（宿主低估=其 lambda_gap，已披露）/ `lambda_disclosed`（lambda 条目无 radon 对应，预期盲区）/ `missing_in_radon`（1:0，如名为 `def` 的函数被消毒改名）/ `identity_mismatch`（同名计数不符或 radon 多出，fail-loud）/ `mismatch`（其余差值=第二层转换缺陷）/ `cc_unmeasured`（parse 失败按文件隔离）。

闭合不变量（机器可查）：七车道行数之和 == `func_entries + lambda_entries`；`summary.closure_ok` 即其判定。`inner_class_files` / `inner_class_method_entries` 为 census 形状值（真树定版见 §SPEC-017），`lambda_gap_total` 为宿主 gap 幅度之和（区别于 `lambda_gap` 车道行数——行数按宿主计）。

### 9.2 `--registry-out reg.json`（`func-registry-snapshot/1`）

多文件 registry 快照：`{snapshot_version, project_root, summary{func_entries, lambda_entries, unparseable, inner_class_files}, files{relpath: func-registry/1}}`。`func-registry/1` 条目字段：`fid`（`relpath::Scope.name`，同类出现序 >1 加 `#k`）、`kind`（func_def|lambda）、`lex_scope`（class 链）、`occurrence`、`static`（穿透单计）、`instrumentable`（inner class 方法 false）、`start_line/end_line`（叶 token 算法）、`body_hash`（span sha1 前 12）、`cc_oracle`（1+自身决策点；lambda 即自身 CC，不据此出分）、`lambda_gap`（仅宿主）；lambda 条目带 `host_fid` 回指，fid 形如 `...::host.<lambda#k>`。parse 失败文件 `entries=[]` 且 `parse:"error"`。文件级 `inner_classes` 清单为诊断字段。

### 9.3 `gqt registry diff <old.json> <new.json> [--out diff.json]`（`func-registry-diff/1`）

statuses：`unchanged` / `edited`（同 fid body_hash 变）/ `added` / `removed` / `identity_changed_candidate`（removed∩added 且 body_hash 一致——机器候选，`human_final:true`，不得自动计收益）/ `removed_identity_candidate`（removed 行被配对进候选后的状态；summary 的 `removed` 计数同时含 `removed` 与 `removed_identity_candidate` 两类行）。scope escape 规则（L1）：`escape_dir`（路径落入 `acceptance/generated/`、`.godot/`、`addons/`）、`lambda_kind`、`moved_into_inner_class`；escape 行单独计数、**永不计入治理净收益**——机器门 `governance_net.countable_added == added - scope_escape`。

### 9.4 契约增量分类

上列三面均为 **capability**（新能力面，加法向后兼容）；`coverage/runner.py` 与 mutation 零改动（§SPEC-021 零漂移承诺）。

## 10. coverage evidence 契约（SEE-1546 WP2 PR-B，diagnostic-only）

### 10.1 `gqt coverage <file> --project-root <root> --evidence-out <dir>`

开启 `coverage-evidence/2` 生命周期（可选旁路；不传则 stdout 契约与 011b87d 逐字节一致——零漂移承诺，§SPEC-026 冻结门）：

- sink 升级为 per-run UUID：`qa-coverage-hits-{run_uuid}.txt`（同进程顺序 run/并发进程/CLI 与 risk 内嵌全部互斥，修 D-候选-1）；
- GUT 前原子写 run 记录 `{run_uuid}.record.json`（status=started，含 probe manifest inline 与 sink_path）；status 生命周期 started→gut_done→bundle_written（或 run_error/timeout/no_probe 终态）；
- bundle `<run_uuid>.json`：evidence_version=coverage-evidence/2，逐条目 T/I/C（T/I 来自 probe-manifest/2，C=hits 按 fid 归属）、census_gaps、sentinels（probes_fired/vacuous/run_error/timeout/zero_tests/gut_ok/transport_anomaly）、entry_states（measured/partial_instrumentation/attribution_gap）、file_state；**无 score 字段**（纯证据，unknown 不出分纪律）；
- bundle 落盘后删除本 run sink；失败路径保留 sink 供恢复；
- 四异常态（run_error/timeout/zero_tests/vacuous）bundle 仍落盘且不出分。

### 10.2 `gqt coverage --recover <evidence-dir>`

重读 started/bundle_written 之外的 record + 对应 UUID sink，按 record 内 probe manifest 重新归属产出 bundle（recovered=true），**不重跑 GUT**；status 迁移至 bundle_written。sink 缺失的 run 记入 errors（fail-loud）。

### 10.3 `gqt coverage --prune-sinks [--retain-days N]`

显式清理（W1 裁决：user:// 在用户 HOME 下，不做隐式清扫）：只删 `qa-coverage-hits-*` 前缀且 mtime 超保留期（默认 7 天）的文件；user:// 目录与项目根两处都扫。

### 10.4 契约增量分类

sink 名 PID→UUID 属私有传输面变更（对外 stdout 契约零影响——§10 冻结门兜底）= **comparability**（PR-B 前的 run 间数据不可比，PR-B 起可比较）；evidence bundle/生命周期/恢复/清理 = **capability**（加法）。`coverage/runner.py` 的插桩判定面（EXECUTABLE_NODE_NAMES/括号深度/class_def 剪枝）语义不变；行收集符号反向公开化至 `registry/lines.py`（§SPEC-029 债务收敛，runner 保留向后兼容别名）。

## 11. canonical recipe 契约（SEE-1546 WP3 PR-A，只读构建器）

### 11.1 `gqt recipe --project-root <root> [--out recipe.json] [--exclude scope:entry:reason ...]`

只读构建 canonical full recipe（`canonical-recipe/1`），**不跑 GUT、不插桩、不宣称 certified**（单轮 exploratory；WP5 拥有双轮认证）：

- **gut_face**：`.gutconfig.json` 现役非 quarantine GUT 面解析（dirs/include_subdirs/prefix 等扫描键原文进 hash）+ config sha256；
- **acceptance_face**：`acceptance/features/**/*.feature` 全量扫描，逐 feature `{key, source, sha256}`（KOL 定版 36）；harness 接线 sha256；
- **toolchain**：gqt/OPS/Godot/GUT 版本——版本变化即 recipe 变化。§SPEC-041 L1 注记：`gqt recipe` CLI 缺省 toolchain 字段为 `unknown`——真实版本由 canonical 编排器（§SPEC-037，PR-C）在测量时注入，注入路径下版本变化即 recipe 变化成立；
- **exclusions**：必须带理由——**空理由 = 配方非法（fail-loud ok=false）**；排除名单与理由全部进 recipe hash；
- **recipe_hash**：对除自身与 `project_root`（provenance 字段）外的规范化 JSON（`sort_keys=True, separators=(',',':')`）取 sha256——路径非测量面：同内容跨路径 hash 一致；同输入同 hash、任一字节变化即变；`verify` 路径（`recipe_hash` 重算比对）校验加载/恢复后的 recipe。

缺省行为不变：不加任何 flag 的既有命令零影响；`recipe` 是新子命令（加法）。

### 11.2 契约增量分类

`gqt recipe` 子命令 + canonical-recipe/1 快照 = **capability**（新能力面，加法向后兼容）；`coverage/runner.py` 插桩判定面、`mutation/`、`risk/` 域本 PR 零触碰。

## 12. accept 私有目录管线契约（SEE-1546 WP3 PR-B，acceptance-suite/1）

### 12.1 `gqt accept compile <feature> --project-root R [--out-dir D]` / `[--keys k1,k2] [--out-dir D]`

选择在编译期完成：`--keys` 把选中 features 逐个编译进一次性私有目录
`acceptance/generated/.gqt-run-<uuid>/`（不传 `--out-dir` 则自动创建；
传入则写入该目录）。**目录即选择集**——私有目录只含选中 keys 的产物，
run 侧整目录执行，绕开 GUT `-gselect` 子串碰撞（选 `{save}` 不会误吸
`test_save_roundtrip.gd`）。私有目录非空即拒（`suite_error`，绝不复用
旧目录）；主 `acceptance/ir|generated` 全程只读不碰；单 feature 不带
`--out-dir` 时缺省行为与 PR-A 前逐字节一致。裸 `accept compile`（无
`<feature>` 且无 `--keys`）错误道为结构化 `ok:false` rc=1（`accept
compile needs <feature> or --keys`），不经 argparse usage error rc=2，
与 §5 exit code 语义一致（exit 2 = 调用方用法错误，exit 1 = 被测对象
判为失败）。

### 12.2 `gqt accept run --project-root R --suite-dir D`（与 `--acceptance-probe-keys` 互斥）

重放(R5) → 整目录 GUT 执行（`-gdir` 指向私有目录 generated）→
acceptance-suite/1 对账五项 → 全过清理（零残留：父目录为本管线创建且
已空才一并移除）；重放失配 = `run_error`（先于 GUT，不执行不可信产物）；
对账任一不过 = `ok:false` 且私有目录保留现场。对账五项：

| 规则 | 字段 | 语义 |
|------|------|------|
| R1 | `generated_eq_selected` | 生成 `test_*.gd` 集合 == 选中 keys |
| R2 | `executed_scripts_eq_generated` | 运行流脚本段（真机 GUT 9.6 锚定：整行 `res://….gd` 头）文件名集合 == 生成集合 |
| R3 | `executed_count_eq_expected` | 脚本段内测试条目数（钩子 `before_all` 等排除、跨脚本同名不塌缩）== manifest attribution 总数 |
| R4 | `unselected_absent` | 私有目录无未选/多余产物 + 主 ir/generated 字节级快照前后一致 |
| R5 | `replay_checked` | 私有目录内每份 manifest 三件哈希绑定重放通过 |

### 12.3 `gqt accept run --project-root R --acceptance-probe-keys [k1,k2]`（W4 成本探针）

缺省 `aidomain_integration`。编译+执行私有目录管线实测墙钟，产出
`performance.acceptance_probe` 记录
`{keys, compile_s, run_s, extrapolation_assumption: "linear-in-feature-count", suite_ok, reconciliation}`；
`risk --performance-file` 外部喂入通道保持不变（向后兼容）。

### 12.4 契约增量分类

`compile --keys/--out-dir`、`run --suite-dir`、`--acceptance-probe-keys`、
acceptance-suite/1 记录 = **capability**（新能力面，全加法，缺省行为不变）；
`compile.py` 仅新增 `out_dir` 分支（主路径代码逐字节未动）、`run.py`
仅 `replay_check` 新增 `suite_dir` keyword（缺省路径不变）；`mutation/`、
`risk/`、`coverage/` 域本 PR 零触碰。
