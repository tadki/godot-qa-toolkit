"""GQT accept 流水线（SEE-1426 P2b）：.feature → IR → 生成 GUT 测试 → headless 执行。

架构同构 gqt mutation（决策 rev2 §3.1a）：消费被测项目自带 GUT
（addons/gut/gut_cmdln.gd 强制前置校验）、一次性 godot --headless 子进程、
库零 Godot 资产（生成测试为 GUT 原生脚本）、写入仅限白名单两目录
（acceptance/ir + acceptance/generated，gitignored、先清空再生成）。
"""

OPS_VERSION = "see1426-accept/1"
