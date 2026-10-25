# testing_api.gd 接口模板（SEE-1426 P4，决策 rev1 §3.4；SEE-1468 /2 扩展）

> 复制到被测项目 `res://addons/gqt_accept/harness.gd`。这是 `gqt accept
> compile` 生成测试消费的**唯一约定接口**——gqt 不嗅探、不发现、不推断
> 接口实现；轨道映射由人按本模板接线。
>
> **/2（ops_version `see1468-accept/2`）**：核心五/六方法语义不动；
> 新增**可选方法族**（场景生命周期 / 输入注入 / 信号观测 / 探针通道）
> 与**查询路径扩展**——未实现方法被步骤触达时 run_step 必须返回
> `"no harness binding for ..."` 显式失败（禁止静默忽略）。
> 参考：`probe_library.gd.md`（探针库 GD 参考模板，含可选族参考实现）。

## 接口契约（生成测试按此调用，缺一即 run_error 面）

```gdscript
extends RefCounted
# 薄而稳的 Core Smoke API 适配层：把 testing_api 的四连招翻译到被测项目
# 的 testing_api 单例/场景。零业务逻辑——业务逻辑永远在被测项目侧。

func smoke_check() -> bool:
    ## 冒烟自检（§SPEC-1426-08）：init/tick/query 连招各点一次，
    ## 失败返回 false → 生成测试 fail_test，run 判定红（不产 verdict）。
    ## 实现要点：真实调用 init_battle_state → execute_tick →
    ## query_entity_state，断言返回形态合法。

func init_case(case: Dictionary) -> String:
    ## 每 IR case 开始：把被测项目状态复位到本 case 起点。
    ## case 含 {scenario, values, steps, types, then_unchanged}。
    ## 返回 ""= 成功；非空字符串 = 失败原因（直接进 GUT fail_test）。

func reset() -> void:
    ## 每 case 结束释放全部 testing_api 实例并复位状态（--worker 硬
    ## 纪律的落实点：常驻通道每 mutant 结束必须走到这里）。

func run_step(step: Dictionary, case: Dictionary) -> String:
    ## 执行单步：step 含 {keyword, text, line}。Given/When 驱动
    ## execute_tick/apply_player_action；行为无绑定的步骤返回非空
    ## 错误字符串（"no harness binding for step: ..."）。
    ## **/2 调用约定**：生成测试与 mutate worker 对本方法一律 `await`——
    ## 同步实现返回值被 await 透传（零破坏）；本方法**可以**实现为协程
    ## （场景生命周期步骤：advance_frames / settle_until 内部 await 帧信号）。

func query_state(path: String):
    ## 状态快照查询："entity.field" 点路径。Then 断言与 unchanged
    ## 基线快照都经此读取；未知路径返回 null（比对必挂，fail-safe）。
    ## **/2 查询路径扩展**（见下节）。

> **typed cell 双重语义（Revy QA LOW 澄清）**：`case.values` 里的
> typed cell 同时是**执行输入与期望值的唯一数据面**——变异轮改的就是
> 这些 cell。harness 执行若从步骤文本/其他来源驱动输入、仅以 values 作
> Then 期望，则执行输入侧不在变异面内，相应 mutant 不可 kill → 恒
> survived → 门禁误打回。取舍由人接线时明确：要 kill 输入面 mutant，
> 就必须让执行从 `case.values` 取数（此时输入变异同时改输入与期望，
> 不可 kill 的形态由 unchanged 槽与对抗面守卫覆盖）；要 kill 期望面
> mutant，执行输入必须与 values 分离（从步骤文本等未变异来源驱动）。
> 两种形态都在本契约内，adoption_spec 接线时二选一并写明。

func check_expectations(case: Dictionary) -> String:
    ## Then 期望值比对（数据驱动断言核心，§SPEC-1426-13）：case.values
    ## 里的每个类型声明 cell 是本 case 的期望值——harness 按自己的
    ## query 映射逐项核对，任何不等返回非空原因字符串（进 fail_test）。
    ## 契约变异轮改的正是这些 cell：本方法缺实现/恒过 = mutant survived
    ## = 变异门禁打回。
```

## 四连招语义（决策 rev1 §3.4 定名）

| 模板方法 | testing_api 对应 | 生成测试调用时机 |
|---|---|---|
| `smoke_check` | init/tick/query 连招 | 首个 case 前，一次 |
| `init_case` | init_battle_state(case) | 每 case 开始 |
| `run_step` | execute_tick / apply_player_action | 每 Given/When 步（/2 起被 `await` 调用） |
| `query_state` | query_entity_state(path) | Then 断言 + unchanged 基线 |
| `reset` | 实例释放 + 状态复位 | 每 case 结束（worker 纪律） |

## /2 可选方法族（按需接线；未实现而步骤触达 = "no harness binding" 显式失败）

### 场景生命周期族

| 方法 | 语义 | 消费方 |
|---|---|---|
| `mount_scene(path: String) -> String` | 实例化并挂载场景到树（装配面 Given）；返回 ""=成功 | 步骤路由 |
| `advance_frames(n: int) -> String` | 确定性帧步进（如 see1403 冻结 30 帧）。**仅用于确定性场景输入**——帧参数入台账、豁免变异（变异帧数改变测量窗口而非期望） | 步骤路由 |
| `settle_until(cond: Callable, max_frames: int) -> String` | liveness 到达态等待（时序面）；cond 取静息定义（如连续 2 帧几何不变） | 步骤路由（协程形态） |
| `current_scene_name() -> String` | 当前场景名读取（挂载验证） | 步骤路由 / query |

### 输入注入族（边界：只收逻辑触发等价物；像素/命中测试/hover 归 godot-mcp，不进契约）

| 方法 | 语义 |
|---|---|
| `inject_gui_input(node_path: String, event: InputEvent) -> String` | 向指定 Control 注入 GUI 事件 |
| `press_button(node_path: String) -> String` | 逻辑等价按下按钮（emit pressed） |
| `emit_signal_path(node_path: String, sig: String, args: Array) -> String` | 沿节点路径发射信号（typed emit helper 优先，同桥 emit_signal 形态） |

### 信号观测族

| 方法 / 查询 | 语义 |
|---|---|
| `watch_signal(node_path: String, sig: String) -> String` | 武装计数器（SigCounter 形态），返回 watcher key |
| `query_state("signal:<sig>.count")` | 读该信号发射次数（未武装 → null = 比对必挂） |

### 探针通道

| 方法 | 语义 |
|---|---|
| `run_probe(op: Dictionary) -> Variant` | **op 词表驱动**（非自由函数调用）：收编四桥实证 op——世界面 18 op 平移 + 交互 12-op 超集（call / call_with_resource / call_with_prior_arg / call_ref / get_ref / get / watch_signal / watch_signal_capture / signal_last_args / signal_count / make_signal_stub / emit_signal）。未知 op = "no harness binding for op: <op>" |

## /2 查询路径扩展（query_state path 语法 /2）

| 形态 | 语义 |
|---|---|
| `a.b.c`（多段点路径） | 深层属性链读 |
| `node:<NodePath>:<property>` | 节点属性读 |
| `ref:<action_id>:<prop链>` | RefCounted 链读（call_with_prior_arg/ref 家族的读侧） |
| `exists:<NodePath>` | 节点在场性（bool） |
| `count:<NodePath>` | 同名实例计数（泄漏/实例数） |
| `signal:<name>.count` | 信号发射计数（需先 watch_signal） |

## /2 typed cell 类型族与变异豁免

| `# types:` 声明 | 断言语义（harness 按 check_expectations 实现） | 变异 |
|---|---|---|
| `int` / `numeric` | 数值精确相等 | ±1 / 边界外推 |
| `float_near:<tol>` | `abs(actual - expected) <= tol` | ±max(1, 2×tol) |
| `bool` | 布尔字面相等 | 翻转 |
| `enum{a,b,c}` | 精确相等且 ∈ 域 | 域内邻值 |
| `in_set` | cell 为逗号分隔成员表（`a,b,c`），actual ∈ 成员集 | 退化坍缩集 "" |
| `not_contains` | actual 不含 cell 子串——**实现惯用法钉死：`actual.find(needle) != -1` 判包含**（`find("") == 0` → 退化 mutant "" 恒被判包含 = 恒失败 = 确定性 kill）。**禁用 `String.contains` 惯用法**：GDScript `"x".contains("") == false`（与 Python `in` 相反），`not actual.contains(needle)` 会让退化 mutant 恒存活、算子失效 | 退化空串 ""（对钉死惯用法恒失败） |
| `signal_count` | `signal:<sig>.count` == cell（整数） | ±1 |
| `str` | 字符串相等 | **零变异（默认豁免，Owner 裁决 3）**——「str 恒豁免」为可接受正常结局；启用 str ±1 须 per-suite 显式声明 |

### not_contains / in_set 的 check_expectations 参考实现（惯用法钉死）

```gdscript
# not_contains：包含判定只允许 find 惯用法（退化 mutant "" 必挂）。
var actual := String(query_state(String(EXPECTATION_MAP[cell])))
if actual.find(needle) != -1:
    return "%s: not_contains violated — found %r in %r" % [cell, needle, actual]

# in_set：cell 逗号拆成员，actual 必须命中其一（退化 mutant "" 拆出 {""}，
# 非空 actual 恒不命中 → 必挂）。
var members := expected.split(",", false)
if not members.has(actual):
    return "%s: %r not in declared set %s" % [cell, actual, members]
```

> 变异闭环依据：退化 mutant 把期望 cell 改成 ""。find 惯用法下
> `find("") == 0 != -1` → not_contains 断言必挂（kill 成立）；若改用
> `not actual.contains("")`，GDScript 求值为 `not false == true` →
> 断言通过 → mutant 恒 survived。接线时照抄上方参考实现，勿自造惯用法。

`# mutation: exempt`（scenario 级，每 scenario 恰好一行）：豁免本场景 typed
变异矩阵；**exempt ≠ skip**——mutate 轮仍执行未变异基线，绿记
`exempt_verified`、挂记 run_error；unchanged 槽基线污染探针照跑。
`summary.exempt_cases == grep -c '# mutation: exempt' <feature>`（V4）。

typed cell 双重语义纪律不变：按钮文本等断言必须走 values→query_state
数据面，**禁止 harness 内硬编码期望**（否则该 cell 变异恒 survived）。

## 纪律

- 本模板**不实现业务**：它把 Gherkin 步骤文本映射到被测项目真实 API
  的接线层。映射表由人维护（见 adoption_spec.md）。
- `query_state` 对未知路径必须返回 null 而非抛错——unchanged 基线
  比对语义依赖"读不到 = 不相等 = 挂"。
- 游离调用（不经本 harness 的场景树操作）不受 gqt 门禁保护，残余风险
  由 adoption_spec 声明。
