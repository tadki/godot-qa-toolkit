# testing_api.gd 接口模板（SEE-1426 P4，决策 rev1 §3.4）

> 复制到被测项目 `res://addons/gqt_accept/harness.gd`。这是 `gqt accept
> compile` 生成测试消费的**唯一约定接口**——gqt 不嗅探、不发现、不推断
> 接口实现；轨道映射由人按本模板接线。

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

func query_state(path: String):
    ## 状态快照查询："entity.field" 点路径。Then 断言与 unchanged
    ## 基线快照都经此读取；未知路径返回 null（比对必挂，fail-safe）。

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
| `run_step` | execute_tick / apply_player_action | 每 Given/When 步 |
| `query_state` | query_entity_state(path) | Then 断言 + unchanged 基线 |
| `reset` | 实例释放 + 状态复位 | 每 case 结束（worker 纪律） |

## 纪律

- 本模板**不实现业务**：它把 Gherkin 步骤文本映射到被测项目真实 API
  的接线层。映射表由人维护（见 adoption_spec.md）。
- `query_state` 对未知路径必须返回 null 而非抛错——unchanged 基线
  比对语义依赖"读不到 = 不相等 = 挂"。
- 游离调用（不经本 harness 的场景树操作）不受 gqt 门禁保护，残余风险
  由 adoption_spec 声明。
