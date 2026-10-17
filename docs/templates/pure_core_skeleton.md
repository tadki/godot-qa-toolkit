# Pure Core 目录骨架模板（SEE-1426 P4，决策 rev1 §3.4）

> 复制本骨架到被测项目 `res://src/core/`。Pure Core = 与场景树解耦的
> 纯逻辑/纯数据层，`gqt arch` 按此边界执行规则 v1。

## 目录布局

```
res://src/core/
├── battle/            # 战斗域：状态机、结算、回合推进（纯逻辑）
│   ├── battle_state.gd      # extends RefCounted（纯数据状态容器）
│   └── battle_engine.gd     # extends RefCounted（确定性推进逻辑）
├── rules/             # 规则域：数值公式、枚举域定义（纯函数）
└── data/              # 数据域：Resource 子类的静态配置
```

## 纪律（gqt arch 规则 v1 的机器判据）

1. **extends 白名单**：core/ 下只允许 `RefCounted` / `Resource` / 其他
   纯数据内建——继承 Node/Node2D/Node3D/SceneTree 即违规。
2. **禁引擎 API**：不得引用 Node/SceneTree/Engine/Input/get_tree/
   get_node/get_viewport/add_child/queue_free 等（含类型注解形态）。
   场景树访问属表现层职责，经参数注入（回调/信号/快照对象）进入。
3. **禁裸全局随机**：`randi()/randf()/randfn()/randi_range()/
   randf_range()/randomize()/seed()` 一律不得直接调用——随机性必须经
   显式注入的 `RandomNumberGenerator`（成员形式调用放行）或由调用方
   传入种子。

## 合规示例

```gdscript
# res://src/core/battle/battle_state.gd
extends RefCounted
var hp: int = 100
var mode: String = "normal"

# res://src/core/battle/battle_engine.gd
extends RefCounted
var rng := RandomNumberGenerator.new()   # 显式 RNG 注入面

func apply_damage(state: RefCounted, amount: int) -> void:
    state.hp = max(0, state.hp - amount)
```

## 验收命令

```
PYTHONPATH=src python -m godot_qa_toolkit.cli arch <project-root>/src/core
```

exit 0 = Pure Core 边界干净；exit 1 时 `failures[].rule` 指明违规规则
（`core/extends-whitelist` / `no-engine-api` / `no-bare-global-random`）。
