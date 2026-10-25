"""GQT accept 流水线（SEE-1426 P2b）：.feature → IR → 生成 GUT 测试 → headless 执行。

架构同构 gqt mutation（决策 rev2 §3.1a）：消费被测项目自带 GUT
（addons/gut/gut_cmdln.gd 强制前置校验）、一次性 godot --headless 子进程、
库零 Godot 资产（生成测试为 GUT 原生脚本）、写入仅限白名单两目录
（acceptance/ir + acceptance/generated，gitignored、先清空再生成）。

ops_version 纪律（SEE-1468 /2，定案 §2）：**schema 键集或调用约定变更 =
major bump**（IR 键集、cell 类型族、harness 调用语义、worker 请求/verdict
协议任一变更）。旧版本 manifest 在 run 侧 replay_check 严格相等校验下
直接 run_error（既有 fail-fast，零新增机制代码）。
"""

# /1 → /2：IR 新增 mutation_exempt 键 + cell 类型族扩展（str/bool/float_near/
# not_contains/in_set/signal_count）+ `# mutation: exempt` 指令 + run_step
# 调用约定 await 化 + worker 循环协程化——schema 键集与调用约定双变，major bump。
OPS_VERSION = "see1468-accept/2"
