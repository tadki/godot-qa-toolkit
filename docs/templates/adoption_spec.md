# adoption_spec 采纳规范（SEE-1426 P4，决策 rev1 §3.4/§3.5）

> 被测项目接入 `gqt arch` / `gqt accept` 门禁的人接线说明。核心纪律：
> **gqt 零嗅探零发现**——工具不探测 testing_api 在场、不读 tag 判轨道、
> 不发现项目结构；一切轨道映射由人按本规范显式接线。

## 1. 轨道映射（人接线）

| 被测项目形态 | 接入轨道 | 必做接线 |
|---|---|---|
| 已有 Pure Core（`src/core/`） | arch + accept 全量 | `gqt arch src/core` 进 CI；harness 按模板接 testing_api |
| 场景树耦合项目 | accept 分步 | 先建 harness（薄适配层），core 边界按 pure_core_skeleton.md 逐步迁移 |
| 仅想跑验收 | accept only | compile + run + mutate；arch 跳过（gauntlet 模板已条件化） |

接线动作（零自动发现，全部人工）：
1. 复制 `testing_api.gd.md` 模板到 `res://addons/gqt_accept/harness.gd`，
   把四连招翻译到被测项目真实 API（映射表人工维护）。
2. `.feature` 放置路径自定（建议 `res://acceptance/`），gauntlet 命令行
   显式传入。
3. CI 把 `accept_gauntlet.sh` 作为门禁步（exit 非 0 即打回）。

## 2. 零嗅探零发现红线

- gqt 不读 `project.godot` 推断结构、不扫描 testing_api、不按 tag 分轨。
- harness 不在场 = 生成测试 `fail_test("accept harness unavailable")`
  → run 红（显式失败，非静默跳过）。
- 轨道信息只存在于人的接线（gauntlet 命令行参数与 CI 配置）。

## 3. 残余风险声明（v1 已知边界）

1. **游离调用不受保护**：被测项目绕过 harness 直接操作场景树/单例的
   代码路径，不在 unchanged 快照与 Then 断言的观测面内——gqt 只保证
   "经 query_state 观测的状态"的确定性。
2. **字段级篡改残余**：manifest 重锚防线（§SPEC-1426-24）覆盖"源
   .feature 缺失/漂移"；攻击者连源文件带三件产物整体替换的场景在 v1
   无 stamp 语义下不可分辨（stamp/attest 为 v2 预登记项）。
3. **gauntlet 互斥锁罩硬序**：单命令游离调用（不经 gauntlet 直接跑
   `gqt accept run`）不在锁罩内——与 gauntlet 并行时可能出现生成物
   竞争，调用方自律（详见决策 rev1 §3.5 Archi 攻-5 TOCTOU 修补边界）。
4. **--worker 硬纪律依赖 harness**：`reset()` 未真实释放实例时，探针
   断言面只能覆盖 harness 自己可见的状态复位；跨实例泄漏归被测项目侧。

## 4. 验收清单（接入完成判据）

- [ ] `gqt arch <core-dir>` exit 0（Pure Core 边界干净）
- [ ] `gqt accept compile <feature> --write` 三件产物在库
- [ ] `gqt accept run` 全绿且 `summary.tests_executed > 0`
- [ ] `gqt accept mutate` 全 kill（survived = 断言面不全，先补测试）
- [ ] `accept_gauntlet.sh` 在 CI 绿；并行双会话第二条等待锁
