# gqt 接口契约（AC-DECPL-005）

> 权威来源：`src/godot_qa_toolkit/cli.py`（SEE-1268 M1/M2 落地）。本文档化机器可判的输入/输出约定，消费方（agent、CI、其他脚本）以此为准。实现与本文冲突时以代码为准并回改本文档。

## 1. 插件定位声明

godot-qa-toolkit 是一个**Godot 项目专用的确定性 QA 测试工具插件**（工具链：Gherkin acceptance pipeline、mutation runner、measurement 面〔suite/run/diagnose/map-update/coverage/complexity/arch/parity/registry-diff/risk〕、doctor 自检）。

> **gherkin 命令沿革（audit）**：独立 Python steps runner / expectations / cli 于 SEE-1468 M5-b 物理退役（parser 晋升为当时 `accept.parser`，判定面迁入 `gqt accept` 与 pytest-bdd）；SEE-1583 起 `accept` 一级入口**更名回 `gherkin`**（`gqt gherkin compile/run/mutate`），旧 Python steps runner 不复活、`--steps` 旗标不存在。

与 godot-mcp 运行时库的分层边界（Owner 2026-09-09 两库拆分裁定）：

| | godot-mcp（① 运行时库） | godot-qa-toolkit（② 工具库，本库） |
|---|---|---|
| 职责 | 运行时驱动：addon 内核 + node proxy/server + launch/ 控制面（启动/驱动真实 editor） | 测试手段：对任何 Godot 项目做测试度量与判定 |
| 与被测项目关系 | 以运行时身份接入项目（MCP 端口、lease、协议） | 以**离线测试工具**身份消费项目（显式 `--project-root` 或受控向上发现 `project.godot`） |
| Godot/GUT 依赖 | 通过被驱动 editor 运行 | 通过被测项目自带的 `addons/gut/` 跑测试；工具自身零 Godot 依赖 |

解耦红线（本库自我约束）：

- **零项目耦合**：`src/` 不引用任何具体游戏项目。被测项目根可由显式 `--project-root` 指定，或从目标路径/CWD 向上发现最近的 `project.godot`；显式非法或发现失败均 fail-loud。
- **消费方显式可控**：需要消除环境歧义时传 `--project-root`；发现只回答项目根，不推断无关目录或仓库布局。
- 历史注释中的具体项目实证（SEE-1268 Revy QA）已改中性表述（AC-DECPL-005：`grep -rn KOL src/` = 0）。

## 2. 调用形态

```
gqt <command> [args]           # console script（pyproject [project.scripts]）
python -m godot_qa_toolkit.cli <command> [args]      # 等价入口
```

**四个一级入口（SEE-1583，v0.3.0 起）**：

```text
gqt
├─ gherkin compile / run / mutate
├─ mutation
├─ measure suite / run / diagnose / map-update / coverage / complexity / arch / parity / registry-diff / risk
└─ doctor
```

改名映射（无兼容）：`accept` → `gherkin`；`canonical` → `measure run`；`canonical-diagnose` → `measure diagnose`；`recipe` → `measure suite`；`attr-update` → `measure map-update`；`registry diff`（子命令组）→ `measure registry-diff`（扁平化，清理后除 `gherkin` 三叶子外无三级命令）；`mutation` / `doctor` 保留原名；`complexity` / `coverage` / `arch` / `parity` / `risk` 归入 `measure`。**旧一级入口全部删除，调用即 exit 2（argparse invalid choice，stdout 无 JSON）；不保留别名、argv 重写、隐藏 parser 或弃用警告。** stdout `tool` 值同步为 `gherkin` / `mutation` / `measure.<leaf>`；版本化标识同步为 `measurement-suite/2` 与 `measurement-report/2`。SEE-1587 起 attribution 当前 schema 为 `test-attribution/6`（行级主真值）；旧 schema 产物一次性失效，无迁移无脏合并。

## 3. 统一输出 JSON 契约

所有子命令在 **stdout** 输出恰好一个 JSON 对象（`ensure_ascii=False`，缩进 2），带换行结尾：

> **doctor 例外**（SEE-1463 登记）：`gqt doctor` 自检 JSON 打 **stderr**（不产出判据 JSON），stdout 恒空——它与其它子命令不同，不判被测对象而是判定「editable 安装 vs 当前 worktree gitlink」的安装一致性，结果属于诊断而非判据；错配时 exit 2（见 §5）。

```json
{
  "tool": "<gherkin|mutation|measure.<leaf>>",
  "ok": true,
  "summary": { },
  "failures": [ ]
}
```

- `tool`：命令路径（`gherkin` / `mutation` / `measure.<leaf>`，如 `measure.coverage`；`doctor` 例外见上），恒定。
- `ok`：机器判定总结论（P0' 原则——结论确定可判，工具内不做启发式打分）。
- `summary`：各工具专属度量（见 §4），结构随工具不同。
- `failures`：失败明细数组，`ok=true` 时为 `[]`。每项至少含 `reason` 字符串。**killed verdict 永不进 failures**（含差分复用重建的记录——与常规路径同一过滤，见 mutation 差分条）。

各工具的额外顶层字段：

| 工具 | 额外字段 | 说明 |
|---|---|---|
| complexity | `config`、`warnings` | 生效阈值 `{warn_complexity, max_complexity}`；仅告警项（不进 failures） |
| mutation | `jobs`、`results`（仅多文件信封）、`stage_timings`、`stage_reconciliation`、`rss_peak_mb`、`disk_delta_bytes`、`knobs` | 见 §4.3「测量仪表」条；测量字段只加不改。单文件恒为 flat 契约（含显式 `--jobs 1` 与 `--jobs > 1`），信封键投影进 `summary`；`results[]` 只属于多文件编排层。dry-run 零测量字段 |

错误兜底：stdout 无 JSON（JSON 解析失败）= 工具实现 bug 或进程级崩溃，消费方按「不可信」处理，而非按 fail。

## 4. 各子命令契约

### 4.2 `gqt measure complexity <paths...> [--warn 10] [--max 15]`

- 输入：一个或多个 `.gd` 文件/目录。
- 阈值语义：`> max` → violation（fail）；`warn < cc <= max` → warning（不 fail）。`--max` 降到 `--warn` 之下时 warn 与其合并（`warn = min(warn, max)`）。
- 不可转换文件（gd2py 失败）计为 failure（unmeasured = fail signal，绝不静默跳过）。
- `ok = 无 violation 且无 unparseable`。
- 输出：`summary: {functions, max_seen, violations, warnings, unparseable}`；`failures` = violations（含 file/name/line/complexity/threshold）+ unparseable；`warnings` 同构独立数组。

### 4.2 `gqt measure arch <paths...>`（SEE-1426 P1）

- 输入：一个或多个 `.gd` 文件/目录（gdtoolkit AST 静态检查）。
- 规则 id 开放枚举（contract §7 加法演进），v1：`core/extends-whitelist`（extends 白名单：RefCounted/Resource 等纯数据基类）、`no-engine-api`（禁引用 Node/SceneTree/Engine/Input/get_tree/get_node 等引擎全局/场景树入口，含类型注解形态）、`no-bare-global-random`（禁裸全局随机 randi/randf/randfn/range 版本/randomize；文件内显式 `RandomNumberGenerator` 在场时成员调用形式放行）。
- 输出：`summary: {files, violations, unparseable, by_rule}`；`failures` = violations（file/line/rule/message）+ unparseable（不可解析 = fail signal）。`ok` = 无违规且无 unparseable。
- exit 0/1，统一 JSON 契约。

### 4.2a `gqt gherkin compile <feature> --project-root <root> [--write] [--no-clean]`（SEE-1426 P2；SEE-1468 /2；SEE-1583 更名）

- `.feature` → 版本化 IR（`ops_version = "see1468-accept/2"`）→ 数据驱动 GUT 测试（运行时读 IR，逐 case 一个 GUT 测试方法）→ 归因 manifest（scenario→test_name，非 ASCII 场景名标识符化 + 序号保障唯一）。
- **ops_version major bump 纪律（/2）**：schema 键集或调用约定变更 = major bump；旧版本 manifest 在 run 侧 replay_check 严格相等校验下 run_error（fail-fast，零新增机制代码）。
- IR schema（/2）：`{ops_version, feature, source_path, cases:[{scenario, scenario_line, example_row_line, values, steps, types, then_unchanged, mutation_exempt, test_name}]}`。schema 冻结五件套（Examples 表一等公民、占位符 `<x>` 归一化、`# types:` 类型声明含 `enum{...}`/`float_near:<tol>` 域与 `str`/`bool`/`not_contains`/`in_set`/`signal_count` 族（声明类型与 cell 形态失配 = parse error；`in_set` 成员以逗号分隔、**周边空白由 compile 期归一化剥离**（QA F2：IR 存归一化 cell，parse 面与模板 naive split 同源）、**成员内含逗号不可表达**）、`# unchanged:` 反向断言槽（/2 放宽多段点路径 `a.b.c`）、manifest 哈希绑定）+ `# mutation: exempt` scenario 级指令（每 scenario 恰好一行，重复 = parse error）全部在 parse 期 fail-fast（违规 = GherkinSyntaxError → JSON 契约 ok:false）。
- 产物三件：`acceptance/ir/<key>.json` + `acceptance/generated/test_<key>.gd`（GUT 目录扫描按 `test_` 前缀过滤——非 test_ 前缀脚本被忽略；**/2 起 run_step 全量 await 化**——GDScript await 对非协程返回值透传，同步 harness 零破坏，场景生命周期步骤可为协程）+ `acceptance/generated/<key>.manifest.json`（含 ir_hash/generated_hash/worker_hash sha256 绑定 + attribution 表）。worker 生成物（`accept_worker.gd`）NDJSON 循环协程化（`_drive()` fire-and-forget，协议不变）。
- 写入仅限 §6 白名单两目录；先清空再生成（`--no-clean` 退路）；写入 opt-in（`--write`），`summary.wrote` 逐文件披露；GUT runner 缺失 = JSON 契约 ok:false。
- **生成测试的对称生命周期（SEE-1619 §SPEC-002）**：生成 GUT 测试具备 `before_each`/`after_each` 对称还原——harness 已创建后，init 失败、断言红、测试体提前 return 与后续场景均经 `after_each` 可靠还原，不再只依赖普通测试体末尾语句；崩溃/超时仍由外层 arm 清理，不伪称 reset 成功。既有 IR/manifest/replay 语义不变。

### 4.2b `gqt gherkin run <dir> --project-root <root> --key <feature-key> [--timeout 120] [--godot-bin <path>]`（SEE-1426 P2）

- 执行前三件哈希绑定重放校验：manifest 的 ir_hash/generated_hash 与磁盘实际 sha256 必须一致，任一失配 = run_error（`summary.run_error: true`，无告警放行档）；GUT runner 缺失同理。**重锚防线（§SPEC-1426-24）**：run 侧把 manifest.source_path 指向的源 `.feature` 现场重编译核对 IR 哈希一致——源文件缺失或漂移 = run_error。**tests_executed 守卫（§SPEC-1426-24）**：GUT 输出零测试流而 exit 0 = 判定失败（`summary.tests_executed: 0` + 显式 failure，不是绿）。
- 执行：一次性 `godot --headless` 子进程跑 GUT（`-gdir=res://acceptance/generated`），跨进程槽位闸包裹（复用 mutation 的 GlobalGodotGate）。
- 输出：统一契约回译——失败测试名经 manifest attribution 对回 scenario/line，失败 reason 附 GUT `[Failed]` 断言明细（§SPEC-1426-25，含 unchanged slot 违规文本）；无法归因的失败显式披露（`scenario: null` + `not attributable` reason）。`summary.{replay_checked, ir_hash, exit_code, tests_executed, wrote}`。

### 4.2c `gqt gherkin mutate <dir> --project-root <root> --key <feature-key> [--timeout 120] [--worker] [--godot-bin <path>]`（SEE-1426 P3）

- 全量契约变异闭环（v1 无 stamp，每轮全量、无增量跳过）：对 IR 全量产 mutants → 逐 mutant 写临时变异 IR（磁盘 IR 每轮后恢复原样）→ headless 跑同一套生成测试 → 挂 = kill；活 = survived 即打回（`ok=false` + survived 明细）。
- 变异矩阵（§SPEC-1426-11；/2 扩展）：数值 ±1 + 边界外推（0→-1）、布尔翻转、枚举声明域内邻值切换（域外从不生成；域未声明的 Then 绑定在 parse 期已被拒）、`float_near:<tol>` ±max(1, 2×tol)、`not_contains` **断言语义翻转**（not_contains → contains：载荷不变，mutant 经 `case.mutated_not_contains` 通道翻转发该 cell 断言极性，harness 按契约消费——基线绿 ⇒ actual 真不含 needle ⇒ 翻转断言必假 = 确定性 kill，与检查惯用法无关；QA F1 裁定）、`in_set` 退化坍缩集（模板 `split(",", false)` 下 `"" → []` → 必挂）、`signal_count` ±1；**`str` 零变异 = 默认豁免**（Owner 裁决 3：启用 str ±1 须 per-suite 显式声明，未启用前「str 恒豁免」为正常结局）；unchanged 槽纳入矩阵（基线污染探针）。
- **豁免轮（/2，exempt ≠ skip）**：`# mutation: exempt` 场景的 typed 矩阵不产 mutants，但其 case 在 mutate 侧执行一次未变异基线轮（先于 mutant 轮）：绿 = 逐 case `exempt_verified`、挂 = 逐 case run_error 计入门禁；未变异基线任何 case 挂 = fail-fast 跳过 mutant 轮（`summary.exempt_baseline_failed: true`）。纯豁免特性（零 mutants + 有豁免 case）走基线轮收口，不触发 `no mutants generated`。
- 三层防伪（§SPEC-1426-12）：① 变异器对抗性金测（70/71 fixture 恰好 ±1/边界/邻值，单测面）；② 哨兵核对——GUT 输出零测试流 = 执行链失真 = run_error 面失败；③ unchanged 槽变异必挂（挂不掉 = survived 打回）。零 Then 绑定且零 unchanged 且零豁免的 IR = run_error（`no mutants generated`）。
- `--worker`（§SPEC-1426-14）：opt-in **真常驻通道**——compile 产出的 worker 生成物（`acceptance/generated/accept_worker.gd`，受先清空再生成管辖 + `worker_hash` 哈希绑定）以单个 godot 进程服务全矩阵：stdin NDJSON 请求行（`{mutant_id, ir_file}` 小载荷，恒 <1024B——`OS.read_string_from_stdin()` 行缓冲上限 1024B，QA-1 实证）+ 变异 IR 经 `.qa-cache/gqt-accept-worker/` 轮转临时文件（FileAccess 读，零行缓冲限制，用后即删、通道结束清目录）；每 mutant 结束硬纪律 = 释放全部 testing_api 实例 + 重建（reload）+ 自检探针断言复位，不符即 run_error verdict。读侧带超时（复用 `--timeout` 为每轮 verdict 等待上限，超时 = _WorkerDead → 剩余 mutant 记 run_error，MEDIUM-1 修复）。验收 = batch 与 worker 两条真实代码路径的差分对照 verdict 一致性（实机口径：IR ≥1678B 量级、非挂死、ok/killed/survived 三项相等）。
- 输出：`summary.{feature, feature_key, ops_version, mutants, killed, survived, run_errors, full_matrix, worker, exempt_cases, exempt_results}`；`failures` = survived（含 target/kind/original/mutated/rationale）+ run_errors。`exempt_cases` == 源 .feature 内 `# mutation: exempt` 指令行数（V4 机器判据），`exempt_results` 逐 case 具名披露（`exempt_verified` / `run_error: ...`）。

- **W 通道（§SPEC-020，worker-reset/1，默认启用；D-W1/D-W2 修复轮 + R-DW4 Owner 裁决 + 默认化实测轮修订）**：常驻 GUT 直驱 worker 对 statically eligible 目标**默认启用**（Owner 裁决实测轮：无旗标即 worker，全部自动降级护栏保留——parity 前置 / 状态漂移 / canary 失配=通道资格否证 / 会话致命整批作废 / 两连失败冷降级）；`--worker-channel` 保留为诊断性强制；env `GQT_MUTATION_WORKER=0|false|no` 为**关断逃生门**（整体回退冷通道，全部执行路径一致生效）；baseline 永远实测走冷通道（红线不变）。静态路由判 worker 的目标（非 autoload/接线链/GUT 域/场景持有者；引用图含 **class_name 裸名使用边** 与 **`closure_scene_cached` 场景持有者谓词**——闭包任一成员被 `.tscn/.tres` ext_resource 引用即冷，Owner 裁决 R-DW4：PackedScene 缓存持有加载时脚本对象、腿间重置不可达，指针旁路彻底退役，§SPEC-001「take_over_path 零命中」判据原样成立）在每 run 单进程内逐 mutant 执行 R-C 硬序（IGNORE 原位刷码 → LIFO teardown → 逐闭包 reload（reload 集收敛 .gd，compile-time 绑定唯一载体）→ 顺序门 → fresh Gut 直驱（**error_tracker 注册对齐 GutRunner**，headless pause 跳过）→ beacon）。等价性门（D-W2）：① **parity 前置门**——会话首腿以原始源执行，直驱失败集必须逐位等于冷 baseline 失败集，失配 → stderr 披露 + 整文件冷降级；② **状态漂移门**——连接指纹 / `user://` 树（每腿清场）相对首腿基线漂移 → 会话致命；③ **canary 失配 = 通道资格否证**——窗口改冷权威 + 剩余全部冷降级（不再作废窗口后继续 worker）；会话致命（闭包任一 reload rc≠0 / loaded-source 哈希门失配 / autoload 交叠 / 腿挂死）仍走 respawn + 自上次 canary 窗口整批作废 + 冷重跑 + stderr 披露。summary 增 `exec_scheme`（`cold-shadow/1` vs `worker-reset/1`，只加不改；并行合并面 passthrough 同步在场），killed 证书按 scheme 隔离（身份包入 file_key）。canary 粒度 env `GQT_WORKER_CANARY_EVERY`（缺省 25：首 mutant 强制双通道预检）。**每轮通道披露（SEE-1589 B §SPEC-002）**：`summary.worker_channel` 恒给出 `requested` / `route` / `exec_scheme` / `decision` / `reason` / `route_reasons` / `reason_counts` / `closure_size` / `override_refused`。`decision` 枚举 `worker` / `cold_route_excluded` / `cold_worker_disabled` / `cold_gut_direct_unavailable` / `cold_unknown` / `worker_degraded_cold`；`reason` 对应 `worker_reset_session` / `route_reason_present` / `worker_flag_or_env_off` / `gut_addon_missing` / `unresolved`，会话致命降级另给 `user_state_drift` / `connection_drift` / `parity_mismatch` / `session_fatal`，并附 `fatal_events` 与 `equivalence=cold_rerun_all_remaining_legs`。路由 reason 码为 `target_in_autoload_table` / `closure_member_in_autoload_table:<member>` / `target_in_gut_domain` / `closure_intersects_gut_domain:<member>` / `closure_scene_cached:<member>` / `eligible_worker`；`reason_counts` 按冒号前码折叠计数。显式 `--worker-channel` 但路由判冷 → `requested=true` 且 `override_refused=true`；禁止无解释的静默冷通道。
### 4.3 `gqt mutation [<files...>] [--project-root <root>] [--budget 50] [--timeout 600] [--tests <res://glob>] [--select {line,fn,file,full}] [--attribution-map <path>] [--no-attribution-auto] [--dry-run] [--mutation-warning N] [--timeout-factor F] [--mutate-all] [--break-on-baseline-failure] [--no-kill-bail] [--mutation <id>] [--lines SPEC] [--no-reuse-coverage] [--godot-bin <path>]`（`--resume`/`--reuse-coverage` 已退役：差分与 prefilter 均默认开启，SEE-1594 A13）

- **目标入口（SEE-1594 §SPEC-019/020）**：显式文件可为普通 `.gd` 路径或单个 `<file>.gd#<lines>` 紧凑卡尺（与不同值的 `--lines` 冲突即 fail-loud，不与其他目标混用）；位置参数全省略时按 `git diff HEAD` 的已跟踪改动并上未跟踪 `.gd` 文件推导目标，排除子模块、隐藏/工具产物与 vendored `addons/`。非 Git、git 失败或无匹配目标均 fail-loud。`--project-root` 显式优先，缺省从目标路径/CWD 向上发现最近的 `project.godot`，发现失败同样 fail-loud。

- **磁盘影子注入（SEE-1550 M0，OPS `see1550/1` 一次性失效）**：注入机制由内存注入改为磁盘影子注入——每 worker 持有隔离 hardlink COW 影子槽（无硬链接 FS 退真拷贝；`.godot` 每槽整树实拷贝；建槽前置先写 `.qa-cache/.gitignore` 为 `*`，失败 fail-loud），mutant 经「临时文件 + `os.replace`」于引擎启动前写入影子内目标 `.gd`（每 mutant 一次 replace、无恢复步；槽归还还原目标并断言 ==原码）；真实仓目标文件全程零写入。影子槽统一走 `<root>/.qa-cache/mutation-pool/slots/` 的持久 warm 池（Linux 与 win64 同机制，SEE-1589 B 起；池锁不可用才回退一次 fresh ext4 农场 `<tmp>/gqt-mutation-farm/<slug>`，归还即删）。四级注入可达性 beacon（磁盘读回哈希 → load 编译 → autoload 同源 → GUT 运行证据）：哈希/实例不符 → `run_error`；load-null/parse → `invalid_mutant`；无 GUT 运行流不得 killed/survived。现行内存注入生产路径已物理删除（`--injection` 开关不存在）。旧 resume/coverage store（OPS `see1420/1`）不读不写不删、静默忽略（A14 已退役一次性失效披露面），首跑全量重跑（Owner 裁决菜单 B）。
- **缓存身份包（SEE-1550）**：resume file-key 增身份包成分——`inject_scheme`（`disk-farm/1`）、精确 Godot 版本串（`--version`，按二进制 path+mtime 缓存；不可解析按路径界定）、boot/wiring hash（规范化 `project.godot` + autoload/ 清单 + tests 引用 `.tscn` 清单）、GUT addon 内容哈希。任一漂移 = 旧 key 永不再命中（store 不迁移不脏合并）。coverage store 同步入档（A13 后 prefilter 旗标已退役，见 §4.3 coverage prefilter 条）。
- **killed 证书（SEE-1550）**：killed 条目携带 `killed_by = [{test_name, script_path, script_sha1, deps_sha1}]` **完整失败集**（无截断；归属由 tests 树 func 定义扫描解析）。差分复用门：全部 killer 测试仍在且自身/依赖闭包哈希一致 → 复用；killer 改/删/依赖变 → **仅该 entry 重跑**（trigger `killer_changed`）；证书缺失/解析不确定 → 保守重跑（trigger `cert_missing`）；**新增测试不失效旧 killed**（校验只碰 killer 闭包，非整树指纹）。summary 增 `killed_by_certificates`（id → 完整失败集证书）披露。
- **疑似孤儿披露（SEE-1550，unreachable 不进 verdict）**：目标脚本零静态引用（autoload 接线 / `.tscn` ext_resource / tests 字面量 / 其他脚本引用全无）∧ 存在 survived → 幸存记录逐条 `suspected_orphan: true` + `summary.suspected_orphans` 名单。静态不可判定面（动态 load / uid 间接引用）只披露、不判定、**不 skip**（位点照常执行）。
- **summary 机制披露（SEE-1550；SEE-1589 B 扩键，只加不改）**：`inject_scheme`（恒在场，含 dry-run）、`slot_mode`（`fresh`|`warm`；有槽执行语义时在场）、`freshness`（分层权威逐层结论，同上条件在场）、`worker_channel`（每轮通道决策与 reason，见 W 通道条）、`farm`（槽位 mode/decision/reason/proof_binding，见 farm/prewarm 条）、`prewarm`（decision/reason/import_wall_s）。`killed_by_certificates`（有 killed 证书时）与 `suspected_orphans`（孤儿诊断 ∧ 存在 survived 时）为按语义在场的增量键，见「正常输出」。
- **coverage prefilter 自动启用（SEE-1550 L1；SEE-1587 同源投影；SEE-1594 A13/A14 退役旧面）**：`--reuse-coverage` 旗标已退役（默认自动启用，无需 flag）；`--no-reuse-coverage` 显式关断全部过滤层。解析顺序 = 同轮 `/2` 投影 store（绑定映射内容与执行证据）→ 新鲜 `/6` 行级主真值现场投影；手动 `/1` store 已退役，盘上残留条目按 schema 分流永不命中。每层校验自己的 provenance/绑定，漂移即失效并披露；全部不可用才回退不过滤。uncovered 不进 ok 门禁、不跑 GUT、不占 budget（既有语义不变）。
- **统一 warm 池与新鲜度权威（SEE-1550 L1；SEE-1589 B 起 Linux 同样可达复用）**：Linux 与 win64 通道槽均持久复用（冷首建一次，每 run `git status --porcelain` delta 同步 + 定向重链 + 行级/卡尺选择目录同步/清理，慢 delta ≥10s stderr 披露）；只有池锁不可用（无 fcntl / 加锁失败）才回退一次性 fresh ext4 农场并具名披露 `reason_detail=pool_lock_unavailable`。复用前过 `.godot` 哨兵、目标三向与（显式开启时）全量 census；任何歧义整槽重建并披露 reason（`slot_tree_missing` / `freshness_ambiguity` / `generated_dirs_missing` / `proof_drift_rebuild`）。并发 gqt 经池账本 flock + 每槽 flock 串行化；进程死亡（SIGKILL）残留 mutant 由下轮定向三向（repo stat vs 槽 stat vs 记录）捕获并重链修正。`.qa-cache` 池受槽数上限（8）与磁盘预算（`GQT_MUTATION_POOL_BUDGET_MB`，缺省 512）LRU/prune。全量 census 仅 `GQT_MUTATION_FULL_CENSUS=1` 显式触发；单点/卡尺 run（`--mutation`/`--lines`）走定向三向、跳过全量 delta 同步。
- **§SPEC-010 基准**：`benchmarks/bench_ab_see1550.py`（A=master 旧机制 worktree vs B=新机制，A/A ≥3 轮噪声带 → A/B 交错 ≥3 轮，verdict/kill-set 一致性 + 墙钟门 + JSON 报告）——QA 阶段实机执行留档。
- **基准脚本修复语义（SEE-1564 §SPEC-201，两处 bug 修复，输出 JSON 形态不变）**：`bench_ab_see1550.py` 的 `warm` 场景改为**真零执行复用**——不传 `--mutate-all`，killed 全部复用后仅剩 baseline 锚（此前误传 `--mutate-all` 使 warm 场景仍全量跑，测不出复用收益）；`bench_worker_channel.py` 的 `kol` 模式经 `prepared_proj` 直接使用已就绪的项目根（如 KOL `/tmp` 真副本），跳过 fixture 制备分支（此前该分支引用未定义变量 `fixture` 直接崩溃）；fixture 模式语义不变。
- **测量仪表（SEE-1564 §SPEC-202；SEE-1589 B 扩键，stdout 单 JSON 只加不改）**：非 dry-run 输出增 `stage_timings`（11 键 `test_scan`/`prewarm`/`baseline`/`per_mutant`/`worker_boot`/`slot_sync`/`prefilter`/`farm_provision`/`planning`/`route_scan`/`total`，单位秒，round 3，同一阶段多次发生按求和累计）、`stage_reconciliation`、`rss_peak_mb`（本进程 `VmHWM` 高水位，Linux `/proc` 读取，不可得 = `0.0` 诚实缺省；godot 子进程内存不计入）、`disk_delta_bytes`（`.qa-cache` + ext4 影子农场树的前后字节差）。**披露层级**：单位级（`summary.stage_timings`）= 该单元亲自执行的阶段——`prewarm` 只在并行父层发生、单元恒 `0.0`；`baseline` = 本 run 消费的 baseline GUT 耗时（含复用父层快照的 elapsed，占比归因口径）；运行级（多文件信封 `stage_timings`）= 父层编排阶段（`test_scan`/`prewarm`/`baseline`/`prefilter`/`planning`/`total`）+ 各分片执行阶段（`per_mutant`/`worker_boot`/`slot_sync`/`farm_provision`/`route_scan`）求和。同名阶段跨层语义不同（单元 `baseline`=快照耗时 vs 父层=预跑墙钟），**不得盲目合并**。`stage_reconciliation` 把阶段分解对账到墙钟：`{wall_s, attributed_s, unexplained_s, over_attributed_s, prewarm_import_s, prewarm_decision}`——`slot_sync` 是含 `farm_provision` + `baseline` + `per_mutant` 的窗口、`total` 是墙钟基准，二者不参与求和；并行分片阶段求和可超过墙钟（并发重叠），溢出以 `over_attributed_s` 显式披露，不得让 `unexplained_s=0` 伪装成完全归因。`disk_delta_bytes` 仅运行级信封与单文件 summary（并行分片各自走树会重复且语义归 run）。dry-run 零测量字段。
- **`slot_sync` 口径（SEE-1564 §SPEC-210 LOW-1）**：窗口 = 槽获取（含 fresh 制备 / warm delta 同步）→ 归还断言**全程**，故**包含整段 `per_mutant` 执行**；槽位固定成本 = `slot_sync` − `per_mutant`（QA 实测 ≈0.09 s/chunk）。
- **farm / prewarm 机制披露（SEE-1589 B §SPEC-002）**：每轮 `summary.farm` 给出 `{mode, decision, reason, proof_binding, proof_reason, validation, linked?, copied?, generated_dirs?}`。`mode` ∈ `fresh` / `warm`；`decision` ∈ `created` / `reused` / `rebuilt`，`reason` 如 `fresh_farm_created` / `warm_created` / `warm_reused` / `proof_drift_rebuild` / `proof_drift_persists` / `slot_tree_missing` / `freshness_ambiguity` / `generated_dirs_missing`；复用/重建路径另披露 `generated_dirs={synced, pruned}`。`proof_binding` ∈ `pending` / `bound` / `drift_persisted` / `inactive`；`validation` 保留逐层新鲜度结论。prewarm 结果单文件投影为 `summary.prewarm`、多文件在信封顶层 `prewarm`，形如 `{decision, reason, import_calls, probe_s, proof_id?, import_wall_s?}`：`decision` ∈ `skipped_non_project` / `disabled` / `imported_no_lock` / `fresh_skip` / `imported_certified` / `imported_uncertified`；`fresh_skip` = 证书双摘要逐位一致且 `import_calls=0`（`reason=proof_fresh`）；`import_wall_s` 为实际导入墙钟，证书签发需两次成功导入，故为两段之和。`freshness` 字符串列表保留为人类可读兼容面，机器消费以 `farm` / `prewarm` 为准。
- **测量专用 gated 旋钮（SEE-1564 §SPEC-203；全 unset = 现行为，逐位不变）**：`GQT_MUTATION_JOBS_CAP`（覆写 jobs 上限，缺省 `max(2, cpu//2)`，钳下限 2，无效值 stderr 告警后回退默认）、`GQT_MUTATION_POOL_MAX_SLOTS`（覆写 warm 池槽上限，缺省 8，钳下限 1，无效值告警后回退；Linux/win64 同池）、`GQT_MUTATION_BASELINE_IN_FARM`（baseline 改在一次性 ext4 影子槽副本内实测——fresh 槽用完即弃、不进 warm 池；opt-in 默认 **OFF**，D5「真实仓 baseline 权威」路径不动；制备失败降级回真实仓并 stderr 披露，披露值取**实际生效值**，降级时为 `false`）。旋钮**仅在 set 时披露**：信封 `knobs`，单文件投影进 `summary.knobs`。
- **D-1 通道身份归位（SEE-1564 §SPEC-207）**：resume manifest 与逐 mutant 记录必须落**同一通道键**。父层规划现经 `identity_for_target` 按目标解析 `exec_scheme`（与 `run_mutation` 同源），worker 路由文件的 manifest 落 worker 键、与其记录一致；cold 侧为 no-op（既有 cold 键零迁移），A/B 跨侧隔离保留。修复前默认 worker 通道下父层以缺省 cold 键写 manifest → 下轮 `no_prior_manifest` 全量重跑（差分复用静默失效）。
- **D-2 coverage 身份归一（SEE-1564 §SPEC-208）**：覆盖行是**通道无关事实**（行覆盖不依赖执行通道），prefilter 的查找/校验身份归一为 canonical cold（写侧 coverage CLI 恒 cold，为 no-op）；manifest/store2 键**不经**此归一（维持通道身份，D-1 语义不动）。其余身份成分（ops / Godot 版本 / wiring / GUT 哈希）仍全匹配校验——任一漂移仍禁用并披露（宁 miss 不假绿）。
- **行级运行时真值选面（SEE-1587，默认生效；SEE-1581 静态 auto-scan 退役后的主路径升级）**：省略 `--tests` 时选测经 `test-attribution/6` 行级主真值映射（路径经 `--attribution-map` / `GQT_ATTRIBUTION_MAP`，缺省自动管理 `.qa-cache/attribution-map.json`）。主数据为 per-test/per-script `test_to_lines` / `script_to_lines` 与 `line_to_tests` 反查；file/function 视图由行事实 + 源码 registry 派生，不再是独立主真值。
- **默认 `--select line`**：按每个实际位点所在行的测试脚本集合 `frozenset(line→tests)` 签名分组；同签名位点共享一个物化选择目录与 baseline，组数 = 不同测试集合数，禁止一行一组。无映射测试的位点在默认路径标 `uncovered`、不执行、不进 ok 门禁并留痕。
- **test 级筛选（SEE-1610 §SPEC-IMPL，默认开启）**：仅对 `line` 选面且分片携带组内映射（`tests_allow`）的可控宿主生效；宿主在基线红物理跳过（`skip_red`）之后按 allowlist 过滤 GUT collector，kill 判定仍是失败名差集，skip_red 与 kill 即退语义不变。闭包阶梯：命中行 `line_to_tests` 身份 ∪ `script_to_lines` 脚本车道整脚本保留（`*`，before_all 等脚本级触达不可 test 级收窄）；稀疏行先扩到包围函数的 `test_to_functions` 边，仍空则整文件面；目标文件列入映射 `unattributed_files` 或该键缺失（legacy/增量产物无法证伪完整）→ 整文件面。闭包形态披露于 `summary.test_filter.scope`：`line` / `file`；`file` 表示行/fn 精度不可信而保守放宽，不是新选面模式。空 allow 不下发（行为零变化）。开关默认开，`GQT_MUTATION_TEST_FILTER=0|false|no` 关断（当前无专用 CLI flag）；无组内映射、显式 `--tests`、`fn`/`file`/`full` 面不启用。
- **显式兼容/审计面**：`fn` 按包围函数分组，函数不在映射时有界回退文件集并披露 `file_fallback`；`file` 使用文件级派生集合；二者仅作显式校准/回退视图。`full` 是唯一显式全脸审计开关（bypass attribution，`tests_glob=None`）。无兼容别名，文件级或映射本身不可用时 fail-loud，不自动退全脸。
- **卡尺同源选面**：`--lines SPEC` 接受单行 `42`、连续区间 `10-20`、多段 `10-20,30,35-40`；与实际位点求交并披露 `lines_selection.{requested,requested_count,hit,hit_count,skipped_no_site}`，全部无位点显式失败。`--mutation <stable-id>` 与 `--lines` 均使用选中位点/行的测试集合并集，不再回落文件或函数整面；无映射测试的选中位点照常执行并披露 `no_mapped_tests`，保留公平重跑通道。显式 `--tests` 表示用户接管，披露 `test_selection.source=explicit_tests` 与未使用行级选面原因。
- **新鲜与执行完整性**：产物须同时满足 recipe pristine、目标文件 sha256、测试面内容与当前树一致，且 `arms_state.gut_full` 有真实执行证据。无 bundle、零测试/零探针且非 vacuous = 不可用；`pairs>0` 可用，合法空面仅在 `probes_fired>0` 或 `vacuous=true` 时可用。缺字段的旧 `/6` 无边产物同样不可证明测过。
- **自动重建与显式只读**：缺失、过期、旧 schema 或执行证据不可用的默认映射在 auto 模式经 `measure run --test-attribution` 全量或 `measure map-update` 增量重建一次（锁 + 唯一 tmp + `os.replace` + `validate_refresh` 守恒校验）后重判，仍不可用即 fail-loud；显式 `--attribution-map` 只读透传，schema/绑定不符 fail-loud 且不自动改写；`--no-attribution-auto` 关闭自动生成/增量。**virgin 导入引导（SEE-1605）**：auto 生成/重建在首个插桩 GUT 前先做一次项目导入引导——`.godot` 缓存不全时执行 `godot --headless --import`（`GQT_AUTO_IMPORT=0` 关断；缓存已 warm 时校验廉价通过），引导失败即具名 fail-loud，不进入测量。**生成失败披露（SEE-1605）**：auto 生成/重建失败统一走 JSON `ok:false` + `failures[].reason` 具名，同一具名原因（`attribution map auto-generation failed: …`，如 `attribution_generate_failed`）同步写 stderr，exit 1；不落半成品、不假 `ok:true`。`measure risk` 在车道循环前以全部请求文件预温 auto 映射（跨目录一次覆盖），预温失败即中止；warmed 复用、显式 `--attribution-map`、mode=off 与 `--skip-measure` 不触发生成，语义不变。
- **选面物化与披露**：选择目录 `.qa-cache/gqt-attr-select/<id>/` 以选中脚本内容寻址，GUT 经同名 `extends` 包装收集；`tests_glob` 仍为单一 res:// 目录，下游 resume/coverage/worker 使用同一身份链。披露键：`selection_mode`、`selection_groups`、`jobs_limit_reason`、`test_selection`、`lines_selection`、`summary.attribution`；kill 证书归属使用映射身份（`RuntimeOwnership`），无新鲜映射不产出证书（fail-safe 偏向重跑）。`selection_mode` 取**实际生效面**全枚举：`line` / `fn` / `file` / `full` / `lines` / `mutation` / `explicit_tests`（优先级：卡尺 `--mutation` > `--lines` > 显式 `--tests` > `--select`；未知值 fail-loud，不静默回落）。行级/函数级分片被签名组数钳制时（groups < 请求 jobs）披露 `jobs_limit_reason=selection_groups` 且 `jobs=selection_groups`；未钳制或非分组模式不披露该键。
- **零引用短路（SEE-1571，默认生效；SEE-1581 起判据改真值映射）**：省略 `--tests` 时若**真值映射**判定目标零测试触及且目标有可执行位点，则以「有意不执行」替代全量 GUT 退路——零测试引用在数学上不可能杀死 mutant。判据唯一出处是映射选面（串行 dispatch 门与并行规划门同源；禁止按 CLI 参数推断）。定终态记录 `ok=true`、各 verdict 计数为 0、`jobs=0`、`scoped=false`、`note=zero-ref short-circuit…`、`summary.zero_ref_short_circuit=true`，并在 stderr 输出同义披露行（双披露）；多文件 run 中该文件记录保留在 `results`，不得静默消失。显式 `--tests` 使门不可达；`--dry-run`、零位点与收集异常不短路。该语义表示「潜在 kill 有意不执行」，不是全量质量证据。**键位（SEE-1589 A-L2）**：零引用终态恒有 `summary.zero_ref_short_circuit=true`；零测试且零变异位点的 no-sites 路径同样置该键，并附 `summary.short_circuit_reason=no_mutation_sites`，两条路径键名一致、可机器区分。
- **成本感知串/并行仲裁（SEE-1571，默认生效）**：单文件 run 收尾方将 `stage_timings` 归一为四键先验（`prewarm_s` / `baseline_s` / `per_mutant_s` / `slot_fixed_s`），原子写 `<root>/.qa-cache/gqt-mutation-cost-priors.json`（单写者；0 值保留旧先验，串行轮测不到的阶段不得覆盖历史，短路/零执行轮不写）。仲裁模型：`serial_est = baseline_est + total×per_mutant_s`，`parallel_est = prewarm_s + baseline_est + ceil(total/jobs)×per_mutant_s + jobs×slot_fixed_s`；仅当目标位点总数 ≤ **8**（`SMALL_SCALE_SITES`）且有有效先验、且 `serial_est < parallel_est` 时判串行。>8 位点永不仲裁、永不降级（即使先验被慢槽污染也回并行缺省）；无先验 / `per_mutant_s<=0` / 先验损坏 = 并行。仲裁时 stderr 披露决策线（total、jobs、两个估算值、四键先验；出域并行也披露 out-of-domain）。
- **静态 auto-scan 退役注记（audit）**：SEE-1571「gdself 索引进程内复用」与 SEE-1576 §SPEC-613「直连引用强度选测」描述的静态派生路径（`mutation/testscan.py`）已随 SEE-1581 真值选面**退出主路径**（不再作为省略 `--tests` 时的选测依据）；模块与单测保留，零引用短路判据改由真值映射给出（见上条）。历史契约细节以两 SPEC 原文为准。
- **prewarm 新鲜度证明（SEE-1571，默认生效）**：并行路径的 `godot --headless --import` 预热由内容证明守护。证书落 `<root>/.qa-cache/gqt-prewarm/proof.json`（项目级 flock；原子发布；导入前先失效旧证）。只有当前 `input_digest` 与 `.godot` 整树 `output_seal` 均与证书逐位一致才 `fresh_skip`；输入摘要按当前内容哈希项目内全部常规文件（仅排除 `.git/`、`.qa-cache/`、`.godot/` 与当前 `.gutconfig.json` 精确声明的 JUnit 报告；`.uid`、`.import`、未知扩展名与 `.gdignore` 全纳入）。签证要求两次成功导入达到 `(S2,O2)==(S1,O1)` 内容定点、两次 rc=0 且无 `ERROR:` 输出、必要导入产物与 remap 输出在位；引擎身份绑定启动器及 wrapper 链至终端二进制的当前内容 SHA-256。`importer="keep"` sidecar 是非导入配置输入：完整字节计入输入摘要、不进 provider 闭包、零 remap 输出合法；非 keep 的零产物声明仍判歧义。provider 登记表版本 `see1571-e1/1`，内建离线 importer 精确集合为 `{texture, font_data_dynamic, font_data_bmfont, csv_translation}`。未知 importer、自定义 import plugin、symlink/非常规文件、不可读路径、非自包含依赖、启动器无法解析为具体文件（含 bare `godot` 字面量形态）、wrapper 链循环/超深/无法到达终端二进制、锁不可用、证书字段失配——一律不签发/不跳过，宁可导入（fail-closed）。`GQT_PREWARM_SENTINEL=0|false|no` 为逃生门：始终导入。fresh-skip 与重导入原因均 stderr 披露（skip 行含 proof_id 与双摘要前缀）。真实仓 baseline、baseline-in-farm 与普通 Linux farm 的执行根在 baseline 启动前做同一证书的双摘要绑定；失配时先重签证/重建，baseline 不得消费旧证。
- **默认并行（SEE-1413）**：`--jobs` 缺省时单文件同样走并发 per-mutant 切片路径（默认 jobs = 上限 `max(2, cpu//2)`，复用 executor.default_jobs）。单文件 CLI 顶层契约**恒为 flat 形态**（`{"tool","ok","summary","failures"}`，显式 `--jobs 1`、`--jobs > 1` 与缺省一律如此），`summary` 新增 `jobs` 键（串行=1，并行=该文件实际分片数）——只增不减，既有键集零变化；`{"tool","ok","jobs","results"}` 编排信封**只属于多文件**，其 `results[i].summary.jobs` 同样记录该文件实际分片数。dry-run 的每文件 `jobs=1`；无 chunk 的 no-sites/setup-error settled 记录为 `jobs=0`。
- **自动退串行（SEE-1413）**：单文件变异点总数 ≤ 生效 jobs 时自动走既有串行路径（判定在变异点收集后、任何 GUT / prewarm / 共享 baseline 预跑之前——少变异场景并发固定开销倒挂），报告形态与串行一致（`jobs=1`）。显式 `--jobs 1` 仍强制串行。`--jobs` 帮助文本已修正为 per-file AND per-mutant slicing（单文件同样生效）。
- **显式串行的选面边界（SEE-1587）**：`--jobs 1` 串行路径仍使用文件级并集 glob；它是覆盖行级所选测试的正确超集，但收敛弱于默认行级签名分组。默认/并行路径不受此边界影响。
- **timing store 语义（SEE-1413）**：退串行路径与 `--jobs 1` 串行快路径接线 TimingStore（预算协商/采样不丢）；并发分片路径不接线（worker 分片跳过 store 读写是 SEE-1356 L1 既有契约，adaptive timeout 只放大不收紧）。

- 前置：`<root>/addons/gut/gut_cmdln.gd` 必须存在；目标 godot 二进制必须可解析（见 §6 Godot 二进制定位）。缺失任一 → 统一 JSON failure（exit 1，`failures[0].reason` 含 `not found`）。
- **目标路径解析（SEE-1576 §SPEC-610）**：`<file.gd>` 相对路径统一锚定 `--project-root`（绝对路径直过），路径解析不依赖调用方 CWD——与文档示例形态（`gqt mutation src/... --project-root .`）一致。mutation 多文件检查点 / `run_mutation` / coverage `run_coverage` 三处同形态检查点同口径；找不到时错误消息附锚定后路径。
- 执行：baseline GUT（原文件，**永远实测**）→ 差分规划 → 逐 mutant 磁盘影子注入跑 GUT（mutant 写入影子槽内目标 `.gd`，真实仓目标文件零写入——机制见上 M0 条；旧内存注入生产路径已物理删除）。
- **kill 判定 = 失败测试名集合相对 baseline 的差集**，不看 exit code——被测项目基线在无头环境可能自带 pre-existing failures，按 rc 判定会全部误杀。
- **差分默认开启（SEE-1420 定案 D4，行为变更）**：v2 resume manifest 在场即差分——未改 scope 且上次 killed/suspect/invalid_mutant → 复用跳过；survived / timeout / timeout_pinned / run_error → **永远重跑**（survived 永不缓存信任——红线）。`--mutate-all` 显式退路（全位点执行）。**CI 迁移点**：升级后首跑全量重跑一次（见下 resume v2）；此后 CI 红绿面由「每次全量」变为「差分 retry 面」——survived 位点仍每轮重跑，红不会因缓存变绿。**复用记录口径（SEE-1576 §SPEC-609）**：复用重建的记录与常规路径同口径——killed 复用条目**不进 failures[]**（经 `summary` 计数 + `diff_skipped` 披露呈现），suspect 等非 killed 复用条目保留在 failures[]；各 summary 计数语义不变。
- **scope manifest / 稳定 id（定案 D1/D2/D3）**：scope 集 = 顶层 func + 内部 class 整体黑盒 + module（非 func 顶层声明）；header hash 只收非 func 顶层声明、func 变更绝不触发全文件重跑（单向保守）；无位点 scope 不进 manifest。位点 id = `<file>#<scope_id>#<n>`（scope_id = `func:`/`class:` kind 前缀 + 语义名，同 kind 同名消歧 `func:foo#2`——跨 kind 同名 func/class 不碰撞（Final Review MEDIUM-2）；n 为 scope 内 1-based 扫描序），**id 仅为寻址提示**——复用前必过全签名 (line, column, kind, original, mutated) + scope hash 双校验，任一漂移 = stderr 告警 + 拒绝复用 + 降级重跑，绝不静默按 id 取条目。
- **`# gqt: no-mutate`（定案 L6）**：注释过滤。marker 在顶层 func 定义行（含行尾注释）或其紧邻上方 → 函数级（抑制至下一顶层声明）；其余位置行级（仅 marker 所在行）。被过滤位点不产 mutant、其 scope 不进 manifest。
- **resume v2 store（一次 schema bump，OPS_VERSION `see1420/1`）**：
  - 落盘：`<root>/.qa-cache/gqt-mutation-resume-v2/<file-key>/manifest.json`（per-file 索引层：schema/header hash/含位点 scope hash 表；**收尾方单点写**）+ `<file-key>/<stable-id>.json`（per-mutant 条目：verdict + 全签名五元组 + scope_hash + `timeout_streak`；tmp+os.replace 原子写，各 worker 各写各 id，无跨 worker 竞态）。
  - file-key = sha1(项目内相对路径 | tests_glob | OPS_VERSION | binary_kind | 身份包摘要)（SEE-1550 起增身份包成分，见上「缓存身份包」条）。**无 budget**（D6：预算是每 run 执行上限，不是 verdict 身份成分）。**SEE-1587**：行级分组仍使用内容寻址选择目录作为 `tests_glob`，键结构与 OPS 无需 bump；旧 fn 组与新 line 组因 glob 不同自然不命中，升级/组面变化后首跑重算，属于 fail-safe 失效而非兼容复用。
  - **resume key 负面清单（定案 D6/D5 红线）**：`--mutation-warning`、`--timeout-factor`、`timeout_streak` **绝不进任何 resume key**——调配置不得引发全库重跑。
  - **v1 目录（`gqt-mutation-resume`）不读不写不删、静默忽略**（A14 已退役其一次性失效披露）。**CI 迁移点**：升级后首个 v2 run 全量重跑（一次性失效，不迁移不脏合并；SEE-1550 OPS `see1550/1` bump 再度一次性失效一次，见 M0 条）。**SEE-1463 §SPEC-003**：v1 resume 生产路径（`mutation/resume.py`）已随本批物理删除，其专属用例与 `see1400_resume.feature` 一并退役——resume 落盘仅剩 v2（`resume2.py`）；**A14（SEE-1594 S5）已退役 v1 在场披露逻辑（`resume2.disclose_v1_superseded` 已删除）**——老项目 `.qa-cache/` 内遗留 v1 目录静默忽略（不读不写不删、无 stderr 输出）。
- **timeout_pinned（定案 D5，N=2）**：resume 条目增 `timeout_streak`（连续 timeout 跨 run 计数）。复用位点重判仍 timeout → +1；**streak ≥ 2 → 独立 verdict `timeout_pinned`**：永不并入 killed、**不计入 ok 门禁失败档**、报告持续可见并披露降级来源（failures reason 含 `timeout_pinned after N consecutive timeouts`）；任一后续 run 非超时（survived/killed 均合法）→ 解除清零。**毒 run 守卫**：单 run timeout ≥50% 且 ≥5 条 → 本窗口 timeout 不落 streak，报告披露（stderr `timeout streak suspended: suspected infra run`；守卫窗口=串行整 run / 并发 worker 分片）。
- **budget-on-retry（定案 D6）**：budget 按**差分后 retry 数**计（截断保序，拒轮转）。差分在场 run 与 `--mutate-all` run 同键集披露 `summary.retry_total`（差分 retry 总数）/ `executed`（**实际执行数**——静态预检剔除 invalid_mutant 后真实跑 GUT 的位点数）/ `untouched_ranges`（未触达位点的行号区间列表 `[[a,b],...]`）+ `diff_skipped`（被跳过位点 id 全列；mutate-all 下为空列）。位点数 > budget 时同步披露 `total_sites`（截断前全量）。
- 可选 flag（向后兼容——省略即既有行为，差分除外，见 CI 迁移点）：
  - `--tests <res://glob>`：受影响测试子集（SEE-1312/1400 语义不变）。
  - `--dry-run`：只清点不跑 GUT（零调用契约不变）。**SEE-1420 §18 升级**：manifest 在场时输出差分计数预估 `summary.{diff_available, retry_estimate, executed_estimate}`（无历史时 `diff_available: false` 且 retry_estimate = 全位点数）。
  - `--resume`（已退役——差分默认开启，无需 flag；SEE-1594 A13，SEE-1400 遗留）：语义已被差分默认开启吸收。`summary.resumed` 仅在真实复用（resumed > 0）时在场；store 落盘面已迁移 v2。
  - `--mutation-warning N`（默认 50）：按**截断前**全量位点数判定，超阈值 stderr 告警 + `summary.attention`（建议拆分字符串）。不进任何 resume key。
  - `--timeout-factor F`（默认 2.0）：覆盖内置 `_TIMEOUT_SCALE`，自适应 per-mutant 上限 = `max(timeout, baseline×F+1)`（只放大不收紧；`summary.adaptive_timeout_s` 可查）。不进任何 resume key。
  - `--no-kill-bail`（SEE-1606 §SPEC-P3）：kill 即退默认**开启**（对齐 Stryker `disableBail=false`）。开启时：cold 通道对**物化选择目录 / 单脚本面**改用一次性直驱宿主（进程数不变，beacon L1-L3 契约逐字节保留；rc 全过=0、有失败=1），worker 通道 resident 会话内按脚本顺序执行；同一 Gut 实例按**脚本顺序分 chunk（`KILL_BAIL_CHUNK=32`）执行**，每个 chunk 结束后检查该 chunk 失败名——出现相对 baseline 的**新增失败**即中断后续 chunk 并打 `GQT_KILL_BAIL scripts=k/n new_fail=<test>`（恒附 `GQT_DIRECT_SCRIPTS=k/n`；`scripts_run` 含 bailout 所在 chunk）。chunk 粒度为性能门实测取舍：per-script 逐脚本调用（C=1）在 286 脚本宽面幸存腿实测 210.9s vs 现状 78.5s（2.69×）违反「幸存不更慢」，C=32 幸存腿 63.5s（0.81×）；被杀腿至多多跑 ≤31 个脚本。verdict 判定不变——仍由失败名差集完成，早停只省面内剩余脚本成本。禁用/回退全部具名：env `GQT_MUTATION_KILL_BAIL=0|false|no` 或本 flag 关断（`kill_bail_disabled_by=flag|env`）；面为显式 `--tests` 目录或 `--select full` 全脸（`kill_bail_face=unsupported_explicit_or_full`）；影子槽内无真实 `gut.gd`（`kill_bail_face=gut_addon_missing`）——三者均回既有 beacon+gut_cmdln 形态，绝不静默改道。`summary.{kill_bail, kill_bail_face, kill_bail_disabled_by, kill_bail_scripts}` 恒在场；被杀明细附 `kill_bail_info`；worker 腿同语义（resp `bail` → 明细 `kill_bail_info`）。**不新增 `exec_scheme` token**（早停是现有 scheme 内部行为，缓存身份零失效）。
  - `--break-on-baseline-failure`（默认关闭，SEE-1606 P2；§SPEC-P2-R1 升级语义）：对齐 Stryker `break-on-initial-test-failure`。关闭时基线红测试从后续 mutant 执行面**物理跳过**（身份 = 脚本归属 + 测试名——可控宿主在 GUT collector 中按「脚本 + 名」精确移除红测试；混合红绿脚本只摘红脚本中的同名红测，**其它脚本的同名绿测照常执行并可正常 kill**，QA-D2 反例判据），以 `GQT_SKIP_RED marked=k requested=n names=<script_basename>::<test>` marker + `summary.baseline_skip {applied, face, requested}` + 明细 `skip_red` 披露（红脚本的红名不再出现在运行流 = 未被调用的机器证据；跳过按脚本+名定位后，跨脚本同名不再产生假 survivor）；跳过生效后空差集 = 真实存活（不再落 suspect 档）。物理跳过仅在可控宿主面可用（物化选择目录/单脚本面的 cold 直驱宿主、worker 通道任意面——宿主自行解析目录收集）；冷通道 legacy 面（显式 `--tests` 目录 / `--select full` / 缺真实 `gut.gd`）+ 基线脏 → 具名拒绝中止（`summary.baseline_skip_unsupported: true` + `suggested_action`），不退回「跑了但不算」。原 `summary.{baseline_dirty, baseline_failures, baseline_failure_names}` 登记披露保留。打开开关时基线有失败立即中止（`ok:false`、`summary.run_error:true`、零 mutant GUT；`failures[].reason` 具名）。
  - `--mutation <file>#<scope_id>#<n>` / `--lines SPEC`（P3 卡尺，单文件；SEE-1587 行级化）：强制新执行，绕过差分复用与 coverage 前置过滤；默认使用选中位点所在行的测试集合并集，显式 `--tests` 才由用户接管。`SPEC` 支持单行/区间/多段，命中与无位点行分别披露；无映射测试的选中位点披露后执行。全部无位点或稳定 id 无命中 = 显式错误（ok=false），绝不静默空跑。
  - `--reuse-coverage`（已退役；SEE-1550 L1 起默认自动启用、`--no-reuse-coverage` 显式关断——见上「coverage prefilter 自动启用」条）：统一经 `resolve_coverage_for_prefilter` 按 **`/2` 投影 store → 现场投影** 取值（SEE-1594 S2 A04：手动 `/1` store 已退役，盘上残留 `/1` 条目按 schema 分流永不命中）。`/2` 由 `/6` map 中实际 `tests_glob` 所承载测试的行事实并集投影（派生缓存，非第二真源），现场投影使用 selection 已解析的同一映射（A06）与同一数据源且零第二次插桩/GUT；两层共享同一次映射加载（A05 合并读取）。`/2` 绑定包含映射内容 sha 与执行证据 sha（A05）——map 任一内容/证据变化即显式失效；其余路径、源码、tests_glob、binary/identity 及 map/recipe/tests-face 绑定匹配同前，漂移原因逐层聚合披露。全部失效 = 告警 + 不过滤。跳过锚 = **可插桩 ∧ 未覆盖**：`uncovered` 不执行、不进 ok 门禁、不消耗 budget；不可插桩行上的位点一律留在执行面，可插桩集不可得也不过滤。成功 `coverage_prefilter` 披露 `source=projection_store|projection_live` 与 uncovered/covered_executed 计数。
- **运行进度面（SEE-1525，非判据面）**：每个变异体判定完成时向 **stderr** 输出一行 `[<done>/<total>] <verdict> L<line> <stable_id> <elapsed>s`（并行分片追加 ` worker=wN`，基线降级串行分片追加 ` worker=serial`；`total` 为整 run 的执行面，非单 worker 切片——静态 invalid 位点计入分母也计入 done，故每个 worker 都能走到 `[n/n]`）——串行与并行路径同一计数点，**stdout 恒为恰好一个 JSON**（进度绝不进 stdout）。机器可读面同步原子更新：`<root>/.qa-cache/gqt-mutation-progress/<run-id>/<file-slug>.<worker>.<pid>.json`，字段 `done / total / killed / survived / timeout / run_errors / suspect / invalid_mutants / elapsed_s / started_at / updated_at`。分片按 worker 进程私有（文件名含 worker 槽位与 pid，各写各的路径；写盘 = 同目录 tmp + `os.replace`），聚合只在读侧发生（`godot_qa_toolkit.mutation.progress.read_progress(root[, file])`：按 file 分组，计数求和、`total`/`elapsed_s` 取 max、时间取极值，只取 `updated_at` 最新的 run）——**无共享写者、无文件锁、无读-改-写，故无并行写竞争**。写失败（只读盘 / `.qa-cache` 被占 / Windows 上读者持有目标文件）一次 stderr 告警后放弃进度面；进度面不进 `summary`、不参与 ok 门禁、不影响判定。
- **基线快照失败降级告警（SEE-1525）**：父层共享 baseline 预跑失败（异常 / 非零 rc）→ 该文件整卷退单 worker 串行（既有兜底语义不变），并向 stderr 发 `mutation: baseline snapshot failed for <file>: <reason> — this file falls back to serial single-worker execution (continuing)`；**只披露、不失败、不改变判定**。`tests_glob` 未解析（全量面不预跑 baseline）不在此列。
- 中止契约（baseline 本身不可信时，mutant 数据不产出）：

```json
{"tool":"mutation","ok":false,
 "summary":{"file":"...","run_error":true,"error":"..."},
 "failures":[{"reason":"baseline GUT timed out after Ns ..."}]}
```
  注意：该形态下 `summary` 无 `killed` 等键（消费方不要按默认 0 读取）。baseline 超时中止另附 `summary.suggested_action` 与 `summary.suggested_action_kind`（SEE-1589 A-H1）：`raise_timeout` = `--timeout` 未达 600s 预算上限，建议抬到上限；`narrow_face` = 已到/超上限，`--select full` 跑整套 GUT 审计、真实规模项目装不进该预算，建议改 `--select line|fn`（缺省 line）或卡尺面 `--lines`/`--mutation`，或缩减 `.gutconfig.json` 的 dirs。已在上限时不得再回显同值 `--timeout`。**SEE-1606 P1 起默认 `--timeout` 即 600（=上限）**：未显式降低时超时一律落 `narrow_face`，`raise_timeout` 仅出现于显式低于上限的调用；阶梯键义与上限值不变（非契约变更）。
- 正常输出：`summary: {file, mutants, killed, survived, timeout, run_errors, kill_rate, budget}` + SEE-1312 `{suspect, invalid_mutants, adaptive_timeout_s}` + SEE-1420 按模式增量 `{resumed, diff_skipped, retry_total, executed, untouched_ranges, total_sites, attention, timeout_pinned, coverage_prefilter, selected}`（仅在对应语义在场时出现）+ SEE-1550 按模式增量 `{inject_scheme, slot_mode, freshness, killed_by_certificates, suspected_orphans}`（条件在场性见上「summary 机制披露」条）+ SEE-1571/1589 按语义增量 `{zero_ref_short_circuit, short_circuit_reason}`（仅短路定终态记录在场；`short_circuit_reason=no_mutation_sites` 为 no-sites 路径）+ SEE-1581/1587/1589 按语义增量 `{selection_mode, selection_groups, jobs_limit_reason, test_selection, attribution, lines_selection, worker_channel, farm, prewarm, stage_reconciliation}`（行级真值选面/卡尺/机制披露在场时；`coverage_prefilter` 另含 source 分层来源）；`failures` = 非 killed 的 mutant 明细，每项增 **`id`** 列（verdict ∈ survived/timeout/run_error/suspect/invalid_mutant/**timeout_pinned**/**uncovered**；survived 记录在疑似孤儿诊断命中时另带 `suspected_orphan: true`）。
- **test 级筛选披露（SEE-1610 §SPEC-JSON，只加不改）**：`summary.test_filter = {enabled, disabled_by, applied, allow_identities, script_lane_fallback, scope}`。`enabled`/`disabled_by` 描述开关及关断来源；`applied` 表示是否实际下发非空 allow；`allow_identities` 是 `[script,test]` 对数，`script_lane_fallback` 是 `[script,"*"]` 整脚本保留数；`scope` 为 `line`/`file`，未下发时为 `null`。逐 mutant `verdict.test_face` 含 `requested`（allow 对数）、`kept`（保留测试数）、`removed`（物理移除测试数）、`whole_script_fallback`（整脚本保留的脚本数）；cold marker v2 另含 `matched_pairs`（命中 collector 的 allow 对数）与 `overlap_red`（其中已被 skip_red 先删的重叠对数），worker 腿披露 `overlap_red`、不保证 `matched_pairs`。过滤已启用但宿主未及打点（如 timeout/run_error）时以 `marker_missing=true` 兜底，各计数为 `null`。五档 `killed`/`survived`/`timeout`/`run_error`/`suspect` 均可携带该键；killed 记录另落 `summary.killed_by_certificates[].test_face` 与 resume store 条目（store 为**最后写者**披露，后续关断轮或 worker 覆写可替换，消费方不得当作不可变历史证书）。`summary.test_face = {applied_legs, kept, removed, whole_script_fallback, marker_missing_legs, overlap_red, by_verdict}` 聚合实际有披露的腿；`kept`/`removed` 只累加 marker 在场腿，`marker_missing_legs` 单列，`by_verdict` 给各 verdict 档腿数。
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

### 4.4 `gqt measure coverage [file.gd] [--project-root <root>] [--min-percent 80] [--timeout 120] [--tests <res://dir-or-.gd>] [--evidence-out <dir>] [--prune-sinks] [--retain-days N] [--godot-bin <path>]`

- **行级映射唯一真源（SEE-1594 §SPEC-025）**：`run_coverage` 经 `coverage/mapping.py::project_file_coverage` 消费 `test-attribution/6`；不再有逐文件 direct GUT 插桩执行路径。已有新鲜映射时覆盖投影零 GUT、零第二次插桩；映射缺失或漂移时可经 `truth_select.ensure_map` 构建/增量刷新，失败具名 `CoverageMappingError` 并转统一 JSON，绝不回退旧 direct 路径。
- `--project-root` 显式优先，缺省从目标文件/CWD 向上发现最近的 `project.godot`；显式非法或发现失败均 fail-loud。真实 Godot 项目导入缓存不全时默认自动导入一次（`GQT_AUTO_IMPORT=0` 可关断）；已有新鲜投影无需执行引擎。
- `--timeout` 作用于映射构建/增量刷新阶段，不是新鲜投影的 GUT 预算。`--tests` 以目录边界或单文件精确匹配过滤映射中的测试脚本，再按这些脚本的行事实投影；省略即使用完整映射测试面。
- **执行证据**：`probes_fired` 取自映射 `arms_state.gut_full`，`probes_source=map_arms_state`，不是本次伪造的探针计数。映射无有效 arms、缺 `probes_fired`、非 vacuous 面却零探针均 run_error；零可执行行才走 `vacuous=true` 终态。
- `--evidence-out` 由投影结果写 `coverage-evidence/2` bundle；hits_lines 来自映射，probes_fired 来自 arms_state，不新增第二次 GUT。写失败具名 fail-loud。旧 sink-recovery 入口与旧 run-record 消费已退役。
- `--prune-sinks` 只清理遗留 `qa-coverage-hits-*` 文件，不产生覆盖答案；`file` 仅在该模式下可省略。
- 正常输出：`summary: {file, total_lines, covered_lines, coverage_percent, min_percent, probes_fired, probes_source, branch_total, branch_covered, branch_coverage_percent, hits_lines, coverage_source, attribution_map, selection_scripts, vacuous}`；`coverage_source=line_mapping_projection`。分支覆盖仍为源码结构 + 命中行的 observational 推导，不进 ok 判定。
- `ok = coverage_percent >= min_percent`；映射/证据错误强制 `ok=false`。未达标 failure 附 `covered/total/uncovered_lines`（最多 20 行）。

### 4.5 `gqt doctor`（SEE-1463 契约登记）

- 用途：自检「editable 安装的 godot-qa-toolkit 是否仍指向当前 worktree gitlink」的安装一致性，供消费方在插件升级/重排后核对。
- 判定对象是**安装一致性**（诊断），非被测项目的判据——因此自检 JSON 打 **stderr**、stdout 恒空，与 §3「各子命令 stdout 恰好一个 JSON」的判据通道分叉（例外理由见 §3）。
- 输出：stderr 一个 JSON，`status` ∈ `ok` / `ok_multi_checkout` / `mismatch` / `not_installed` / `skipped`。`ok_multi_checkout` = editable 指向另一 gqt 检出但两者 HEAD 相同（合法多检出形态，附 `note` 说明无需动作）；HEAD 不同仍判 `mismatch`。`mismatch` 附 `editable_location`/`toolkit_root`/`gitlink_head`/`fix_hint`；`not_installed` 附 `fix_hint`；`skipped`（cwd 不在任何工具树内）附修复提示：在 gqt 检出根运行，或设 `GQT_TOOLKIT_ROOT=<gqt 检出根>` 后重跑。工具库根识别支持直接在 gqt 源码检出内运行（`pyproject.toml` + `src/godot_qa_toolkit`），不因缺少历史 `.dev/qa-toolkit` 布局而误报 skipped（SEE-1589 A-L3）。
- exit：一致 → 0；安装错配（self-check 判定 install mismatch）→ **exit 2**，stdout 无 JSON（§5 的「调用方/环境错」档，不判被测对象）。

## 5. exit code 语义

| exit | 语义 | 触发 |
|---|---|---|
| 0 | 判定通过（`ok=true`） | 任意子命令 |
| 1 | 判定失败（`ok=false`，stdout 有 JSON） | verdict fail / run_error 中止 / 目标文件或 GUT runner 不存在 |
| 2 | 用法/装载错误，**stdout 无 JSON** | argparse 参数错误；`doctor` 安装错配（自检 JSON 打 stderr、stdout 恒空） |

约定：exit 2 是「调用方自己错了」，exit 1 是「被测对象判为失败」。消费方自动化只应区分 0 与非 0，并解析 stdout JSON 细分 1 的形态。

## 6. `--project-root` 边界约定

- `mutation` / `measure` 各测量面：显式 `--project-root` 优先；缺省从目标路径（无目标则 CWD）向上发现最近的 `project.godot`。显式值不存在/非目录、或发现失败均 fail-loud。`gherkin` 仍按各叶子契约显式传根。根目录必须含 `addons/gut/gut_cmdln.gd`。
- **Godot 二进制定位（SEE-1525）**：三级优先 = `--godot-bin <path>`（命令级覆盖，`mutation` / `measure coverage` / `gherkin run|mutate` 同语义）> 环境变量 `GQT_GODOT_BIN` > `PATH` 首匹配（均无 → 字面量 `godot`）。显式路径（env 或 flag）不存在/不可读/不可执行 → `BinaryConfigError` → 统一 JSON 错误契约（`ok:false`，消息含来源标签与路径）。**`--path` 形态随二进制类型同源**（`detect_binary_kind()` 判定）：win64 通道 `/mnt/` 绝对路径经 `wslpath -w` 转 Windows 形式（不可转换原样），linux 通道一律原生形态不转换——转换原语单一出处（`mutation/inject.py`），mutation 与 coverage 共用。**未设变量且未传 flag 时全部子命令行为与 SEE-1525 前逐字节一致（opt-in 冻结）**。
- 工具在该 root 下运行 `<godot> --headless --path <root> -s res://addons/gut/gut_cmdln.gd [-gdir=<tests_glob>] -gexit`（`<godot>` 为上述解析结果；`-s` 必须是 res:// 形式，绝对路径会被 Godot 拒绝加载——见 mutation/runner.py 注释）。
- 工具可能写入的路径仅限：`measure run` 映射构建的临时插桩目标（finally 恢复；direct coverage 已改行级映射投影，不再单独改写目标文件；**mutation 侧 SEE-1550 起零写入真实仓**——mutant 只写影子槽内目标，见 §4.3 M0 条）、遗留 coverage sink 的显式清理（`--prune-sinks`；不再作为 `measure coverage` 数据源）、影子槽池 `<root>/.qa-cache/mutation-pool/slots/`（Linux/win64 同机制；池锁不可用时回退项目外 fresh ext4 农场 `gqt-mutation-farm`，XDG/tmp 下、归还即删），以及 `<root>/.qa-cache/` 下的工具自有缓存（gqt-timing 预算锚、gqt-mutation-resume-v2 断点续跑（manifest 索引层 + per-id 条目；SEE-1550 起 file-key 含身份包成分）、**mutation-pool 持久 warm 池**（SEE-1550；SEE-1589 B 起 Linux/win64 同机制；池 schema `see1550-pool/1`：槽位 hardlink COW 影子树 + `.godot` 实拷贝 + 池账本/每槽 flock；槽数上限 8、磁盘预算 `GQT_MUTATION_POOL_BUDGET_MB`（缺省 512）LRU/prune）、gqt-mutation-resume（v1，代码已随 SEE-1463 删除；目录不读不写不删、静默忽略）、gqt-coverage 行级覆盖事实（SEE-1420 P2；SEE-1550 起含身份包）、gqt-godot-slots 并发闸锁文件、gqt-mutation-progress 运行进度分片（SEE-1525，per-worker 分片 + 读侧聚合，见 §4.3）、gqt-mutation-cost-priors.json 成本感知四键先验（SEE-1571，收尾方单点原子写）、gqt-prewarm 新鲜度证明与项目级锁（SEE-1571，proof.json / prewarm.lock / 失效侧写 .stale）——均自含 .gitignore；建槽前置先写 `.qa-cache/.gitignore` 为 `*`，失败 fail-loud）。
- **生成类持久写入档（SEE-1426，Owner 批准的 §6 首次扩张）**：`gqt gherkin compile --write` 可写 `<root>/acceptance/ir/` 与 `<root>/acceptance/generated/` 两目录（gitignored 一次性产物；先清空再生成；写入 opt-in，`summary.wrote` 逐文件披露；写入前置校验 `<root>/addons/gut/gut_cmdln.gd` 在场）。除此之外零写入。
- timeout 语义：单次 GUT 全套测试的 wall-clock 上限（`--timeout` 默认 600s，SEE-1606 P1；`BUDGET_CAP_S` 同值）；被测项目基线超过 timeout 时 mutation 返回 run_error 中止（数据不可信优于慢数据）。SEE-1312 起 mutation per-mutant 上限自适应 = max(显式 timeout, baseline 实测耗时 × F)（SEE-1420 起 F 可由 `--timeout-factor` 覆盖，默认 2.0 不变；只放大不收紧，`summary.adaptive_timeout_s` 可查）。SEE-1400 起全部 godot 子进程（mutation baseline/per-mutant 与 coverage）经跨进程 flock 槽位闸（容量 env `GQT_GLOBAL_GODOT_SLOTS`，缺省 max(2,cpu//2)；等槽时间不计入 timeout；fcntl 缺失平台告警一次后放行不阻塞）。

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

### 9.1 `gqt measure parity <paths...> --project-root <root> [--report parity.json] [--registry-out reg.json]`

全量逐函数 CC parity（`parity/1`）。oracle = registry func_def 条目（原始 AST 决策点计数，radon 记录绝不当 oracle）；radon 记录来自与 collector 完全相同的 convert+sanitize 管线。配对 = 文件内按裸名分组、**出现序 1:1**（radon 记录按 lineno 排序恢复源码序——fixture 实证 cc_visit 输出序在有 class 时非源码序）；同名计数不符时整组 `identity_mismatch`，绝不折叠。

七车道：`match`（忠实）/ `lambda_gap`（宿主低估=其 lambda_gap，已披露）/ `lambda_disclosed`（lambda 条目无 radon 对应，预期盲区）/ `missing_in_radon`（1:0，如名为 `def` 的函数被消毒改名）/ `identity_mismatch`（同名计数不符或 radon 多出，fail-loud）/ `mismatch`（其余差值=第二层转换缺陷）/ `cc_unmeasured`（parse 失败按文件隔离）。

闭合不变量（机器可查）：七车道行数之和 == `func_entries + lambda_entries`；`summary.closure_ok` 即其判定。`inner_class_files` / `inner_class_method_entries` 为 census 形状值（真树定版见 §SPEC-017），`lambda_gap_total` 为宿主 gap 幅度之和（区别于 `lambda_gap` 车道行数——行数按宿主计）。

### 9.2 `--registry-out reg.json`（`func-registry-snapshot/1`）

多文件 registry 快照：`{snapshot_version, project_root, summary{func_entries, lambda_entries, unparseable, inner_class_files}, files{relpath: func-registry/1}}`。`func-registry/1` 条目字段：`fid`（`relpath::Scope.name`，同类出现序 >1 加 `#k`）、`kind`（func_def|lambda）、`lex_scope`（class 链）、`occurrence`、`static`（穿透单计）、`instrumentable`（inner class 方法 false）、`start_line/end_line`（叶 token 算法）、`body_hash`（span sha1 前 12）、`cc_oracle`（1+自身决策点；lambda 即自身 CC，不据此出分）、`lambda_gap`（仅宿主）；lambda 条目带 `host_fid` 回指，fid 形如 `...::host.<lambda#k>`。parse 失败文件 `entries=[]` 且 `parse:"error"`。文件级 `inner_classes` 清单为诊断字段。

### 9.3 `gqt measure registry-diff <old.json> <new.json> [--out diff.json]`（`func-registry-diff/1`；SEE-1583 扁平化，原 `registry diff` 子命令组）

statuses：`unchanged` / `edited`（同 fid body_hash 变）/ `added` / `removed` / `identity_changed_candidate`（removed∩added 且 body_hash 一致——机器候选，`human_final:true`，不得自动计收益）/ `removed_identity_candidate`（removed 行被配对进候选后的状态；summary 的 `removed` 计数同时含 `removed` 与 `removed_identity_candidate` 两类行）。scope escape 规则（L1）：`escape_dir`（路径落入 `acceptance/generated/`、`.godot/`、`addons/`）、`lambda_kind`、`moved_into_inner_class`；escape 行单独计数、**永不计入治理净收益**——机器门 `governance_net.countable_added == added - scope_escape`。

### 9.4 契约增量分类

上列三面均为 **capability**（新能力面，加法向后兼容）；`coverage/runner.py` 与 mutation 零改动（§SPEC-021 零漂移承诺）。

## 10. coverage evidence 契约（诊断面；SEE-1594 行级映射收口）

### 10.1 `gqt measure coverage [file] [--project-root <root>] --evidence-out <dir>`

`--evidence-out` 由当前行级映射投影生成 `coverage-evidence/2` bundle，不重跑 GUT、不新增插桩：

- `hits_lines` 来自 `test-attribution/6` 行事实；`probes_fired` 来自映射 `arms_state.gut_full`，不得伪造本次执行证据。
- bundle 保留 `evidence_version`、target identity、T/I/C、census_gaps、sentinels、entry_states 与 file_state；无 score 字段，unknown/vacuous 不出分。
- 映射不可用或证据缺失时仍可写具名失败 bundle（run_error、空 hits），供 `measure risk` 降级披露；evidence 写失败本身也须具名失败。
- 不传 `--evidence-out` 时只输出既有 coverage JSON 契约。

### 10.2 退役面

旧 direct 插桩的 PID/UUID sink 生产、`--recover <evidence-dir>` 和 started→bundle_written 的 run-record 恢复链已随行级映射单一真源退役；不再作为 `measure coverage` 参数或覆盖来源。`measure run` 自身的双臂插桩/证据链仍在 §13 管辖，二者不得混写。

### 10.3 `gqt measure coverage --prune-sinks [--retain-days N]`

显式清理旧 `qa-coverage-hits-*` sink：只处理 mtime 超保留期（默认 7 天）的文件；不产生覆盖答案，也不读取旧 sink 作为当前行事实。

### 10.4 契约增量分类

映射投影 evidence 输出 = **capability**；旧 sink/recovery 物理退役 = 既有契约收口。行级插桩与符号收集的实现归 `coverage/instrument.py` / `registry/lines.py`，不再是 direct coverage 的第二执行路径。

## 11. measurement suite 契约（SEE-1546 WP3 PR-A，只读构建器；原 canonical recipe）

### 11.1 `gqt measure suite --project-root <root> [--out recipe.json] [--exclude scope:entry:reason ...]`（原 `gqt recipe`）

只读构建 measurement suite 定义（`measurement-suite/2`；SEE-1583 前为 `canonical-recipe/1`，旧产物一次性失效），**不跑 GUT、不插桩、不宣称 certified**（单轮 exploratory；WP5 拥有双轮认证）：

- **gut_face**：`.gutconfig.json` 现役非 quarantine GUT 面解析（dirs/include_subdirs/prefix 等扫描键原文进 hash）+ config sha256；
- **acceptance_face**：`acceptance/features/**/*.feature` 全量扫描，逐 feature `{key, source, sha256}`（KOL 定版 36）；harness 接线 sha256；
- **toolchain**：gqt/OPS/Godot/GUT 版本——版本变化即 recipe 变化。§SPEC-041 L1 注记：`gqt measure suite` CLI 缺省 toolchain 字段为 `unknown`——真实版本由 measure 编排器（§SPEC-037，PR-C）在测量时注入，注入路径下版本变化即 recipe 变化成立；
- **exclusions**：必须带理由——**空理由 = 配方非法（fail-loud ok=false）**；排除名单与理由全部进 recipe hash；
- **recipe_hash**：对除自身与 `project_root`（provenance 字段）外的规范化 JSON（`sort_keys=True, separators=(',',':')`）取 sha256——路径非测量面：同内容跨路径 hash 一致；同输入同 hash、任一字节变化即变；`verify` 路径（`recipe_hash` 重算比对）校验加载/恢复后的 recipe。

### 11.2 契约增量分类

`gqt measure suite` 命令（原 `gqt recipe`）+ measurement-suite/2 快照 = **capability**（能力面；SEE-1583 命令更名无兼容）；`coverage/runner.py` 插桩判定面、`mutation/`、`risk/` 域本 PR 零触碰。

## 12. gherkin 私有目录管线契约（SEE-1546 WP3 PR-B，acceptance-suite/1）

### 12.1 `gqt gherkin compile <feature> --project-root R [--out-dir D]` / `[--keys k1,k2] [--out-dir D]`

选择在编译期完成：`--keys` 把选中 features 逐个编译进一次性私有目录
`acceptance/generated/.gqt-run-<uuid>/`（不传 `--out-dir` 则自动创建；
传入则写入该目录）。**目录即选择集**——私有目录只含选中 keys 的产物，
run 侧整目录执行，绕开 GUT `-gselect` 子串碰撞（选 `{save}` 不会误吸
`test_save_roundtrip.gd`）。私有目录非空即拒（`suite_error`，绝不复用
旧目录）；主 `acceptance/ir|generated` 全程只读不碰；单 feature 不带
`--out-dir` 时缺省行为与 PR-A 前逐字节一致。裸 `gherkin compile`（无
`<feature>` 且无 `--keys`）错误道为结构化 `ok:false` rc=1（`gherkin
compile needs <feature> or --keys`），不经 argparse usage error rc=2，
与 §5 exit code 语义一致（exit 2 = 调用方用法错误，exit 1 = 被测对象
判为失败）。

### 12.2 `gqt gherkin run --project-root R --suite-dir D`（与 `--acceptance-probe-keys` 互斥）

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

### 12.3 `gqt gherkin run --project-root R --acceptance-probe-keys [k1,k2]`（W4 成本探针）

缺省 `aidomain_integration`。编译+执行私有目录管线实测墙钟，产出
`performance.acceptance_probe` 记录
`{keys, compile_s, run_s, extrapolation_assumption: "linear-in-feature-count", suite_ok, reconciliation}`；
`measure risk --performance-file` 外部喂入通道保持不变（向后兼容）。

### 12.4 契约增量分类

`compile --keys/--out-dir`、`run --suite-dir`、`--acceptance-probe-keys`、
acceptance-suite/1 记录 = **capability**（新能力面，全加法，缺省行为不变）；
`compile.py` 仅新增 `out_dir` 分支（主路径代码逐字节未动）、`run.py`
仅 `replay_check` 新增 `suite_dir` keyword（缺省路径不变）；`mutation/`、
`risk/`、`coverage/` 域本 PR 零触碰。

## 13. measure 双源编排契约（SEE-1546 WP3 PR-C，measurement-report/2 + coverage-union/1；原 canonical 编排）

### 13.1 `gqt measure run <roots...> --project-root R --recipe recipe.json --out report.json [--acceptance-probe-keys [k1,k2]] [--timeout N] [--evidence-dir D]`（原 `gqt canonical`）

批式臂双源编排（§SPEC-037；目标全集一次插桩 → 每臂 1 次 GUT → 逐文件 sink 读回 → bundle → union）：

- **臂 gut_full**：.gutconfig 面（无 `-gdir`——GUT 缺省读配置，GUT 9.6 实测锚定）；
- **臂 acceptance（SEE-1619 §SPEC-005 共享分片）**：`acceptance/features` 全量 keys 按共享分片分组编译进私有目录 `.gqt-run-<uuid>/`（§12 管线）+ 逐 key 重放校验 + 重新插桩（新 run_uuid+sink 名）→ 组内多 key 共享 GUT 进程，真实扇出 = `len(groups) × reps`（不再默认 44 feature×3 进程）；`acceptance_shards` 为分片/并发上限。对账五项、逐 key replay、独立 XDG/sink/run UUID、rep 收敛语义保留；私有目录与全部异常路径清理零残留。
- **per-process 临时降级（SEE-1619 §SPEC-006）**：仅当某 feature 当次未通过共享校验时才 singleton 执行，并在 `process_fallbacks:[{key,reason}]` 逐 feature 披露名称与原因——按 key 排序、每项恰含 `key/reason`、成功/suite_error/清理失败等所有返回路径均存在（早失败为空数组）、全原子时 `process_fallbacks=[]`；共享红且隔离也红不得包装为有效降级，GUT map 与 union 完整性按既定口径降级，修复后必须回共享。降级轮并发同样 ≤ `acceptance_shards`；清理失败该臂 invalid 并披露残留路径，禁止带残留报 valid。
- **逐文件 sink** `qa-coverage-hits-{run_uuid}-{file_key8}.txt`（`file_key8 = sha1(relpath)[:8]`），落盘即清、沿 §10 prune 纪律；
- **在树插桩**：备份 per-uid 私有目录 `/tmp/gqt-canonical-backups-<uid>/<run_uuid>/`（started-record + 逐文件原文；0700、ownership 校验、拒 symlink），finally 原子恢复；启动时回收同项目陈旧备份（崩溃恢复：marker schema 校验 + 恢复写入经 pinned-root 逐组件 O_NOFOLLOW dirfd 走廊限定 project root 内）；KOL 生产树零残留；
- 两臂共用同一 registry/probe manifest（同源码、同 manifest），run UUID 每臂独立；
- `recipe.arm` 显式注入（`gut_full|gut_core|acceptance`）；缺省不传 arm 保留 tests_glob 推断（既有 stdout 契约零漂移）；
- **certification.status="not_certified"**、`evidence_level="exploratory"`、`diagnostic_only=true`、`dynamic_cache=false`（单轮 exploratory；WP5 拥有双轮认证）；
- **可用映射的退出语义（SEE-1589 A-M1）**：`summary` 增 `certification`（`certified` / `uncertified` / `unusable`）、`union_certified`、`attribution_usable` 与 `exit_semantics`（`certified` / `artifacts_usable_union_uncertified` / `failed`），有归属产物时另附 `attribution_map`。union 不可认证但归属映射**本轮新鲜**且臂执行证据有效（`arms_valid`）= `ok=true` / exit 0、`certification=uncertified`、`exit_semantics=artifacts_usable_union_uncertified`——读退出码的消费方不得丢弃可用产物；无可用映射（含仅有陈旧 map 文件）仍 `ok=false` / exit 1，`failures[0].reason=union invalid or arms invalid`。产物可用 ≠ union 已认证，两个语义分列披露；
- W4 探针（缺省 `aidomain_integration`）实测墙钟写入 `performance.acceptance_probe`；`measure risk --performance-file` 兼容保留。

### 13.2 `coverage-union/1` 有效性矩阵（§SPEC-036）

同源码 sha256、同 probe manifest 哈希、两臂 run_uuid 不同、两臂 bundle 均 valid（run_error/timeout/zero_tests/vacuous/no_probe 任一 → 臂 invalid）、两臂臂名不同（整臂替换禁拼接）、**禁平均**（`coverage=|∪C|/total`，行集并集）；任一不满足 → `valid=false` + `invalid_reason`，union=null 不出分。`comparable` 仅在两臂 recipe_hash 一致且等于 canonical recipe_hash 时 true（子集配方 false）。

**臂级新增失败门控（§SPEC-060a/064b）**：`baseline_failures` 传入既有（未插桩）运行的失败 `(script, test)` 身份对集合——臂失败对逐对披露后仅**新增**失败对判臂 run_error → invalid；未归因对仅匹配未归因、同名跨脚本不互相吸收；Run Summary 段未归因回显在其归因实例被基线吸收时随同覆盖（§SPEC-064a）。**产品路径懒获取**（缺省 None 时，gut_full 臂专属）：仅当插桩臂观测到失败才现场跑一次同面无插桩 GUT 取既有失败基线并复核（绿臂零额外 GUT；acceptance 臂不取基线——其失败保持可见，债归独立 issue）；显式提供优先；获取失败 fail-loud（具名 stderr + 臂保持 invalid）；现场获取不跨 run 缓存。生效基线（显式或懒获取）披露在归属产物 `baseline_failures_meta`（method/face/count/sha256/wall_s，随增量刷新携带；键只加不改、消费者不读）；映射生成/增量交付门要求 gut_full 臂 state==valid（新增失败产物拒绝交付）。

### 13.3 性能门（§SPEC-039）

端到端墙钟（插桩 + 两臂 GUT + union，recipe 构建与 W4 探针不计）对「现有 GUT + acceptance 序贯基线」3+3 交错中位数 ≤1.0×（§SPEC-052 现行最终性能门；旧 §SPEC-039 时代的 1.5× 中间门已作废）；**不达标=停下汇报、不得合并**；loadavg+CPU 双记录（§SPEC-022 协议）随交付。

### 13.3a acceptance 共享分片性能验收（SEE-1619 §SPEC-010，`tests/integration/run_see1619_acceptance_perf.py`）

- **对比对象**：**同一 gqt 分支内**「共享分片（shared）」与「逐 feature 独立进程（per-feature）」两种进程形态，同 KOL 输入、同 reps、同并发、独立 store；**不是跨 gqt 版本的 master 对比**，不得以进程数推算墙钟。
- **A/A 独立基线（§SPEC-029）**：噪声样本只取首个 B 之前的独立 shared A 块，`baseline_samples ≥ 3`；交错 A/B 对数 `interleaved_pairs ≥ 3` 且必须位于基线块之后。交错已开始而基线不足时 batch 具名 exit 1；纯 A/A 中间轮可合法不完整。
- **速度门（§SPEC-026）**：`speed_gate.ok = (median_ratio > threshold)` **严格大于**——等于阈值不算 PASS。报告绝对秒数、倍率、每轮进程/分片账与逐轮原始记录；任何 INCONCLUSIVE 不得写 PASS。
- **batch/全量出口（§SPEC-028）**：`rounds_complete=true` 时以合并 summary 判定出口（MEASURED → exit 0；speed_gate 失败等 → exit 1），`--mode A/B --round-index` 的单轮 batch 不得绕过 summary/speed gate 形成假绿；不完整报告可按同一语义 resume。
- **披露边界（LOW D-A）**：perf 汇总不携带底层 `rc/exit_semantics`/gut 臂状态；单轮 `rc=1/ok=false` 属 uncertified 设计语义，`MEASURED` 的证明域 = acceptance 臂有效 + 轮次完备 + 速度门。

### 13.4 契约增量分类

`gqt measure run` 命令（原 `gqt canonical`）+ coverage-union/1 + measurement-report/2（原 canonical-report/1）= **capability**（能力面；SEE-1583 命令更名无兼容）；`evidence.py` 仅新增 `arm` keyword（缺省推断不变——零漂移）；`shadow/1` 与 `gqt measure risk` stdout 契约零触碰；`mutation/` 域零触碰。

### 13.5 SEE-1587 line-primary attribution 与增量契约

- `--test-attribution` 启用 hit 级归属：attr 模板的全部块入口 probe 携带归属块，`build_attribution_map` 将 `(probe, slot)` 经 `probe_lines + block_map` 展开为 per-test/per-script covered line sets。产物 schema = `test-attribution/6`，含 `test_to_lines` / `script_to_lines` / `line_to_tests`、`line_pairs` / `script_line_pairs`、`instrumentable_lines`、recipe/文件/测试面绑定与 `arms_state` 执行证据。
- file/function 边与 file 反查由行事实 + 当前源码 registry 派生；没有独立的函数入口主真值。旧 schema 在 `load_map` / 增量 baseline / refresh 校验处 fail-loud；auto 管理路径可重建，显式路径不原地改写。
- prepcache 归属 algo 使用 `attr-line/1` 编码行级粒度；缓存命中读回 `block_map`，全量与增量都经 `_instrument_pair` 构建模板。旧归属形态键不命中新模板，plain 模板不受此分形影响。
- `measure map-update` 对生产变更替换相关行事实，对测试变更/删除按测试或脚本身份整体替换/删边，再从合并后的行事实重算派生视图与反查；`instrumentable_lines` 与文件/测试面绑定同步刷新。不能安全合并时按既有 hash mismatch、新生产文件、重跑面过大或校准到期规则回退全量。Coverage Store 不另起插桩轮，由后续 mutation 按实际 `tests_glob` 从同一 `/6` 事实投影。
- **删除生产文件（SEE-1610 §SPEC-N1，`prod_removed`）**：目标面内已不在盘上的 `.gd` 且有基线边时归 `prod_removed`，不当作 `unknown`。失效面 = 行事实反查触达测试（test 车道 ∪ script 车道）∪ 静态直连消费者（gdself depth==1）；候选脚本重跑并整体替换行事实，旧图该文件死边从行事实主真值 purge，派生视图单源重算，`target_files` / `file_sha256` / `instrumentable_lines` 同步剔除。非 `.gd` 路径、无基线边的删除文件等仍归 `unknown`，以包含具体路径的 `IncrementError` 具名拒绝，不静默披露后照跑。
- **重跑面阈值实测校准（SEE-1610 §SPEC-TH1）**：`DEFAULT_RERUN_FULL_FACE_RATIO=0.05`，候选重跑脚本占基线脚本面超过 5% 时回退全脸。KOL warm 副本（824 文件 / 285 脚本）实测增量成本 ≈ `16.16s + 8.878s × 重跑脚本数`，暖全脸 ≈ `223–227s`，交叉点 ≈ `23.6` 脚本（`8.3%`）；旧 `0.5` 会让中位面增量比全脸慢 `4.3–5.2×`，收紧至 `0.05` 后帽内估算 ≈ `140.5s < 225.25s`，保留收益并留噪声带余量。
- **窗口归因完整性（SEE-1610 D1）**：臂级 `unattributed_files` 列出存在未归属命中的生产文件，表示 test 窗口归因对该文件欠近似；增量产物保守携带基线名单 ∪ 本轮变更/新增生产文件。mutation test 级筛选消费该名单：命中文件或键缺失时放弃行/fn 精度、退整文件 allow 面，避免把归因缺口静默缩成假 survivor。

## 14. SEE-1587 集成判据 harness 契约

以下语义属于 `tests/integration/run_see1587_e2e.py` 与 `run_see1587_line_benchmark.py`，不是新增 gqt 一级命令：

- **E2E 新产物证明**：测量前失效既有映射，测量后以内容哈希证明新 `/6` map；空测量或执行完整性不足不得靠旧产物/零引用假绿。默认 mutation 必须披露行级选面与实际 prefilter 来源。
- **行级基准门**：S0=显式 fn、S1=行范围并集、S2=默认 line、S3=oracle。当前孤立副本协议将 S3 披露为 file 级超集 oracle。性能按 Owner 2026-11-16 裁定采用 `S2/S0 ≤ 1 + A/A noise band`；合并面带宽取各面最大值，字面 ≤1.0× 清单仅作信息性保留。
- **对账完整性**：每个面 S0/S1/S2 均须 `compared>0` 且无 selection_error；只有 `killed` / `survived` 是可判定 verdict。`run_error` / `timeout` / `suspect` / `uncovered` 等不计 compared、不掩盖 mismatch，计入 `undecidable_excluded`；全不可判定轮必须 `ok=false` / RC=1。
- **pending 不是通过**：`--max-wall-seconds` 截止或 `--analyze-only` 缺轮次时输出 `status="pending"`、`ok=false`、completed/required 计数并 **RC=1**；续跑落盘全部必需轮次且通过 gate 后才给终判。
