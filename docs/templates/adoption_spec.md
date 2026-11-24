# adoption_spec 采纳规范（SEE-1426 P4，决策 rev1 §3.4/§3.5）

> 被测项目接入 `gqt measure arch` / `gqt gherkin` 门禁的人接线说明。核心纪律：
> **固定单根 + 人工接口**——gqt 只读取 `addons/gqt_gherkin/` 的 schema、
> features、harness 与 validator；不探测其他根、不读 tag 分轨，testing_api
> 的业务映射由人按本规范显式实现。

## 1. 轨道映射（人接线）

| 被测项目形态 | 接入轨道 | 必做接线 |
|---|---|---|
| 已有 Pure Core（`src/core/`） | arch + gherkin 全量 | `gqt measure arch src/core` 进 CI；harness 按模板接 testing_api |
| 场景树耦合项目 | gherkin 分步 | 先建 harness（薄适配层），core 边界按 pure_core_skeleton.md 逐步迁移 |
| 仅想跑验收 | gherkin only | compile + run + mutate；arch 跳过（gauntlet 模板已条件化） |

接线动作（固定单根，接口人工实现）：
1. 按 `docs/contract.md` §4.2a 准备 `addons/gqt_gherkin/schema.json` 与
   `validator.gd`：schema 最小形如
   `{"schema":"gqt-gherkin-addon/1.0","requires_gqt":"v0.4.2","testing_api":"see1468-gherkin/2"}`；
   validator 必须 exit 0，且最后一份含 `supported_majors` 的合法 verdict 为
   `ok=true`、`supported_majors=[1]`。无需 enable addon 或配置 autoload。
2. 复制 `testing_api.gd.md` 模板到 `res://addons/gqt_gherkin/harness.gd`，
   把必需方法翻译到被测项目真实 API（映射表人工维护）。
3. `.feature` 必须直接放在 `res://addons/gqt_gherkin/features/`；其他根、
   旧 acceptance 布局及 symlink 逃逸均不接受。gauntlet 显式传入该根内文件。
4. CI 把 `gherkin_gauntlet.sh` 作为门禁步（exit 非 0 即打回）。

## 2. 固定单根与人工接线边界

- gqt 不读 `project.godot` 推断结构、不扫描其他 feature 根、不按 tag 分轨；
  仅在 `addons/gqt_gherkin/features/` 枚举 `.feature`，并校验
  schema/validator/harness 的物理位置与版本握手。
- harness 不在场 = 生成测试 `fail_test("gherkin harness unavailable")`
  → run 红（显式失败，非静默跳过）。
- 业务轨道映射只存在于人的 harness 接线与 CI 配置；feature 的固定目录
  枚举不等同于推断其他项目布局。

## 3. 残余风险声明（v1 已知边界）

1. **游离调用不受保护**：被测项目绕过 harness 直接操作场景树/单例的
   代码路径，不在 unchanged 快照与 Then 断言的观测面内——gqt 只保证
   "经 query_state 观测的状态"的确定性。
2. **字段级篡改残余**：manifest 重锚防线（§SPEC-1426-24）覆盖"源
   .feature 缺失/漂移"；攻击者连源文件带全部生成物整体替换的场景在 v1
   无 stamp 语义下不可分辨（stamp/attest 为 v2 预登记项）。
3. **gauntlet 互斥锁罩硬序**：单命令游离调用（不经 gauntlet 直接跑
   `gqt gherkin run`）不在锁罩内——与 gauntlet 并行时可能出现生成物
   竞争，调用方自律（详见决策 rev1 §3.5 Archi 攻-5 TOCTOU 修补边界）。
4. **--worker 硬纪律依赖 harness**：`reset()` 未真实释放实例时，探针
   断言面只能覆盖 harness 自己可见的状态复位；跨实例泄漏归被测项目侧。

## 4. 验收清单（接入完成判据）

- [ ] `schema.json` 与 `validator.gd` 齐备；schema 纯解析握手通过，validator
  exit 0 且 `ok=true`、`supported_majors=[1]`
- [ ] `gqt measure arch <core-dir>` exit 0（仅 Pure Core / arch 轨道；gherkin-only 跳过）
- [ ] `gqt gherkin compile addons/gqt_gherkin/features/<name>.feature --project-root <root> --write`
  四件产物已生成：IR、generated test、`gherkin_worker.gd`、manifest（可重建，不要求入库）
- [ ] `gqt gherkin run --project-root <root> --key <key>` 全绿且 `summary.tests_executed > 0`
- [ ] `gqt gherkin mutate --project-root <root> --key <key>` 全 kill（survived = 断言面不全，先补测试）
- [ ] `gherkin_gauntlet.sh` 在 CI 绿；并行双会话第二条等待锁
