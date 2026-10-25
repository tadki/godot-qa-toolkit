# gqt_accept_probe.gd 探针库参考模板（SEE-1468 M0a /2，定案 §2 目标 3）

> 复制到被测项目 `res://addons/gqt_accept/`（随 harness.gd 同目录），供
> harness 的 run_step / run_probe / query_state 扩展路径调用。**参考实现**
> ——接线由人完成（gqt 不嗅探不推断）；op 词表收编自 KOL
> `tests/devtests/_shared/bridge.gd` 的 **12-op 超集**（§10.2 定案），
> 世界面 18 op 按同构形态平移进 harness 探针库。

## arch 边界（明写，定案 §2 原文）

**harness 与探针库（`addons/gqt_accept/`）不在 `gqt arch` 扫描路径内**：
arch 守卫的扫描集由调用方显式传 paths（`gqt arch <paths...>`，通常是业务
core/ 目录），addons/ 不在其中。因此 harness/探针库可以自由使用
Node/SceneTree/get_tree 等引擎 API——它们是**观测与驱动面**，不是被判定
的业务逻辑。**业务逻辑要进 Pure Core 判定必须拆为独立 RefCounted/
Resource**（`extends RefCounted`/`Resource`、零引擎 API、显式 RNG 注入），
由 harness 加载驱动；把业务逻辑直接写进 harness/探针库 = 逃逸 arch 门禁，
属接线违规，adoption_spec 声明面外。

## 接口速览（与 testing_api.gd.md /2 一一对应）

```gdscript
extends RefCounted
# 探针库：op 词表驱动的观测/驱动原语集。零业务逻辑——所有 op 都是对
# 被测项目公开 API 的通用调用形态（call/get/connect/instantiate），不含
# 任何具体游戏语义。

# ---- run_probe op 词表（12-op 超集，源：tests/devtests/_shared/bridge.gd）----
# op: call                  {node, method, args}            → callv 返回值
# op: call_with_resource    {node, method, args[0]=$resource{script,props}} → 资源注入调用
# op: call_with_prior_arg   {node, method, args, source_id, arg_index}      → 先行结果回灌
# op: call_ref              {ref_action, method, args}      → RefCounted 链上调用
# op: get_ref               {ref_action, property}          → RefCounted 链属性读
# op: get                   {node, property}                → 节点属性读
# op: watch_signal          {node, signal, key?}            → 武装计数器
# op: watch_signal_capture  {node, signal, key?}            → 武装计数器+末次参数捕获
# op: signal_count          {key}                           → 发射次数（未武装 -1）
# op: signal_last_args      {key}                           → 末次发射参数（JSON 化）
# op: make_signal_stub      {name}                          → 原生信号桩节点入场
# op: emit_signal           {target, signal, args}          → typed emit 优先发射
# 失败形态统一 {"__error": "..."}；未知 op 同形态（"no harness binding" 纪律）。

# ---- 场景生命周期族（harness 转发）----
func mount_scene(path: String) -> Node            # 实例化 + 挂 current scene 兄弟位
func advance_frames(n: int) -> void               # await process_frame × n（确定性输入）
func settle_until(cond: Callable, max_frames: int) -> String  # 静息判定，超预算报错
func current_scene_name() -> String

# ---- 输入注入族（逻辑触发等价物；像素/hover 归 godot-mcp）----
func press_button(node_path: String) -> String    # Button.pressed 逻辑等价发射
func inject_gui_input(node_path: String, event: InputEvent) -> String
func emit_signal_path(node_path: String, sig: String, args: Array) -> String

# ---- 信号观测 + 查询路径扩展（query_state 转发）----
func watch_signal(node_path: String, sig: String) -> String
func resolve_query(path: String) -> Variant       # a.b.c / node:…:prop / ref:id:链 /
                                                  # exists:… / count:… / signal:x.count
```

## 参考实现（骨架全量可拷贝；`_root` 由 harness 注入 `root` 节点）

```gdscript
class_name GqtAcceptProbe
extends RefCounted

var _root: Node = null                 # harness 注入：场景树根
var _sig_counters: Dictionary = {}     # key -> SigCounter/SigRecorder
var _results: Array = []               # 先行结果链（call_with_prior_arg / ref 系）

class SigCounter:
	extends RefCounted
	var count: int = 0
	func inc(_a = null, _b = null, _c = null, _d = null) -> void:
		count += 1

class SigRecorder:
	extends RefCounted
	var count: int = 0
	var last_args: Array = []
	func inc(a = null, b = null, c = null, d = null) -> void:
		count += 1
		var args: Array = []
		for v in [a, b, c, d]:
			if v == null:
				break
			args.append(v)
		last_args = args

func setup(root: Node) -> void:
	_root = root

func run_probe(op: Dictionary) -> Variant:
	var name := String(op.get("op", ""))
	match name:
		"call": return _op_call(op)
		"call_with_resource": return _op_call_with_resource(op)
		"call_with_prior_arg": return _op_call_with_prior_arg(op)
		"call_ref": return _op_call_ref(op)
		"get_ref": return _op_get_ref(op)
		"get": return _op_get(op)
		"watch_signal": return _op_watch_signal(op, false)
		"watch_signal_capture": return _op_watch_signal(op, true)
		"signal_count": return _op_signal_count(op)
		"signal_last_args": return _op_signal_last_args(op)
		"make_signal_stub": return _op_make_signal_stub(op)
		"emit_signal": return _op_emit_signal(op)
	# no harness binding 纪律：未知 op 显式失败，禁止静默 null。
	return {"__error": "no harness binding for op: %s" % name}

func _node(path: String) -> Node:
	if _root == null:
		return null
	return _root.get_node_or_null(NodePath(path))

func _op_call(op: Dictionary) -> Variant:
	var node := _node(String(op["node"]))
	if node == null:
		return {"__error": "no node: %s" % op["node"]}
	if not node.has_method(String(op["method"])):
		return {"__error": "no method %s on %s" % [op["method"], op["node"]]}
	var args: Array = []
	for a in op.get("args", []):
		args.append(_resolve(a))
	return node.callv(String(op["method"]), args)

func _op_call_with_resource(op: Dictionary) -> Variant:
	var node := _node(String(op["node"]))
	if node == null:
		return {"__error": "no node: %s" % op["node"]}
	var raw: Array = op.get("args", [])
	var spec: Dictionary = (raw[0].get("$resource", {})
		if raw and raw[0] is Dictionary else {})
	if spec.is_empty():
		return {"__error": "call_with_resource: missing $resource arg"}
	var res := Resource.new()
	res.set_script(load(String(spec["script"])))
	for prop in spec.get("props", {}).keys():
		res.set(String(prop), spec["props"][prop])
	var rest: Array = []
	for i in range(1, raw.size()):
		rest.append(_resolve(raw[i]))
	return node.callv(String(op["method"]), [res] + rest)

func _op_call_with_prior_arg(op: Dictionary) -> Variant:
	var node := _node(String(op["node"]))
	if node == null:
		return {"__error": "no node: %s" % op["node"]}
	var source_id := String(op.get("source_id", ""))
	var arg_index := int(op.get("arg_index", 0))
	var prior: Variant = null
	var found := false
	for entry in _results:
		if String(entry.get("id", "")) == source_id:
			if not entry.get("ok", false):
				return {"__error": "prior action %s failed" % source_id}
			prior = entry.get("value")
			found = true
			break
	if not found:
		return {"__error": "no prior action: %s" % source_id}
	var args: Array = []
	for a in op.get("args", []):
		args.append(_resolve(a))
	while args.size() <= arg_index:
		args.append(null)
	args[arg_index] = prior
	return node.callv(String(op["method"]), args)

func _ref_of(op: Dictionary) -> Variant:
	var from_action := String(op.get("ref_action", ""))
	for entry in _results:
		if entry.get("id", "") == from_action and entry.get("ok", false):
			return entry["__ref"]
	return {"__error": "no stored ref for action: %s" % from_action}

func _op_call_ref(op: Dictionary) -> Variant:
	var ref: Variant = _ref_of(op)
	if ref is Dictionary and ref.has("__error"):
		return ref
	var obj: Object = ref
	if obj == null or not obj.has_method(String(op["method"])):
		return {"__error": "no method %s on ref of %s"
			% [op["method"], op.get("ref_action")]}
	var args: Array = []
	for a in op.get("args", []):
		args.append(_resolve(a))
	return obj.callv(String(op["method"]), args)

func _op_get_ref(op: Dictionary) -> Variant:
	var ref: Variant = _ref_of(op)
	if ref is Dictionary and ref.has("__error"):
		return ref
	if ref == null:
		return {"__error": "null ref for action: %s" % op.get("ref_action")}
	return ref.get(String(op["property"]))

func _op_get(op: Dictionary) -> Variant:
	var node := _node(String(op["node"]))
	if node == null:
		return {"__error": "no node: %s" % op["node"]}
	return node.get(String(op["property"]))

func _op_watch_signal(op: Dictionary, capture: bool) -> Variant:
	var key := String(op.get("key",
		"%s:%s" % [op.get("node", ""), op.get("signal", "")]))
	var node := _node(String(op["node"]))
	if node == null or not node.has_signal(String(op["signal"])):
		return {"__error": "no signal %s on %s" % [op.get("signal"), op.get("node")]}
	var watcher: RefCounted = SigRecorder.new() if capture else SigCounter.new()
	_sig_counters[key] = watcher
	node.connect(String(op["signal"]), Callable(watcher, "inc"))
	return key

func _op_signal_count(op: Dictionary) -> Variant:
	var key := String(op["key"])
	if not _sig_counters.has(key):
		return -1  # 未武装：可区分于真实 0
	return _sig_counters[key].count

func _op_signal_last_args(op: Dictionary) -> Variant:
	var key := String(op["key"])
	if not _sig_counters.has(key):
		return null
	var rec = _sig_counters[key]
	if not (rec is SigRecorder):
		return {"__error": "watcher is a counter, not a recorder: %s" % key}
	if rec.count == 0:
		return null
	return rec.last_args

func _op_make_signal_stub(op: Dictionary) -> Variant:
	# 原生脚本信号桩（heart_stub 形态）：add_user_signal 对
	# `signal.connect` 属性访问不可见——生产契约桩必须走脚本声明信号。
	var stub := Node.new()
	stub.name = String(op["name"])
	if op.has("script"):
		stub.set_script(load(String(op["script"])))
	_root.add_child(stub)
	return String(stub.get_path())

func _op_emit_signal(op: Dictionary) -> Variant:
	var target := _node(String(op["target"]))
	if target == null or not target.has_signal(String(op["signal"])):
		return {"__error": "no signal %s on %s" % [op.get("signal"), op.get("target")]}
	var args: Array = []
	for a in op.get("args", []):
		args.append(_resolve(a))
	var method := "emit_" + String(op["signal"])
	if target.has_method(method):
		return target.callv(method, args)
	var argv: Array = [String(op["signal"])]
	argv.append_array(args)
	return target.callv("emit_signal", argv)

func _resolve(arg: Variant) -> Variant:
	# {"$node": "/root/X"} → 活节点；其余字面透传。
	if arg is Dictionary and arg.has("$node"):
		return _node(String(arg["$node"]))
	return arg

# ---- 场景生命周期族 ----

func mount_scene(path: String) -> Variant:
	var packed: PackedScene = load(path)
	if packed == null:
		return {"__error": "no scene: %s" % path}
	var inst := packed.instantiate()
	_root.add_child(inst)
	return inst

func advance_frames(n: int) -> void:
	for i in n:
		await _root.get_tree().process_frame

func settle_until(cond: Callable, max_frames: int) -> String:
	# 静息判定由调用方给 cond（如连续 2 帧几何不变）；超预算 = 显式失败
	# （fail-loud，不静默超时当绿）。
	for i in max_frames:
		if cond.call():
			return ""
		await _root.get_tree().process_frame
	return "settle_until: condition not met within %d frames" % max_frames

func current_scene_name() -> String:
	var scene := _root.get_tree().current_scene
	return scene.name if scene != null else ""

# ---- 输入注入族（逻辑触发等价物）----

func press_button(node_path: String) -> String:
	var node := _node(node_path)
	if node == null:
		return "no node: %s" % node_path
	if not node.has_signal("pressed"):
		return "no pressed signal on %s" % node_path
	# 逻辑等价：直接发射 pressed（不伪造鼠标几何——像素面归 godot-mcp）。
	node.emit_signal("pressed")
	return ""

func inject_gui_input(node_path: String, event: InputEvent) -> String:
	var node := _node(node_path)
	if node == null:
		return "no node: %s" % node_path
	if node is Control:
		node._gui_input(event)
	else:
		node._input(event)
	return ""

func emit_signal_path(node_path: String, sig: String, args: Array) -> String:
	return str(_op_emit_signal({"target": node_path, "signal": sig,
		"args": args}))

# ---- 信号观测 + 查询路径扩展 ----

func watch_signal(node_path: String, sig: String) -> String:
	var r: Variant = _op_watch_signal({"node": node_path, "signal": sig}, false)
	return r if r is String else str(r)

func resolve_query(path: String) -> Variant:
	# /2 查询路径文法（testing_api.gd.md §/2 查询路径扩展）。
	if path.begins_with("exists:"):
		return _node(path.trim_prefix("exists:")) != null
	if path.begins_with("count:"):
		var base := _node(path.trim_prefix("count:"))
		return base.get_child_count() if base != null else 0
	if path.begins_with("signal:"):
		var body := path.trim_prefix("signal:")
		if body.ends_with(".count"):
			return _op_signal_count({"key": body.trim_suffix(".count")})
		return null
	if path.begins_with("node:"):
		var rest := path.trim_prefix("node:")
		var sep := rest.rfind(":")
		if sep < 0:
			return null
		var node := _node(rest.substr(0, sep))
		return node.get(rest.substr(sep + 1)) if node != null else null
	if path.begins_with("ref:"):
		var chain := path.trim_prefix("ref:").split(":", false, 1)
		if chain.is_empty():
			return null
		var ref: Variant = _ref_of({"ref_action": chain[0]})
		if ref is Dictionary and ref.has("__error"):
			return null
		var cur: Object = ref
		for prop in (chain[1].split(".") if chain.size() > 1 else []):
			cur = cur.get(prop) if cur != null else null
			if cur == null:
				return null
		return cur
	# 多段点路径：锚 root 后逐段下钻（autoload 名 + 属性链混排）。
	var cur: Variant = _root
	for seg in path.split("."):
		if cur is Node:
			cur = cur.get(NodePath(seg))
		elif cur != null:
			cur = cur.get(seg)
		if cur == null:
			return null  # 读不到 = 不相等 = 挂（fail-safe 契约）
	return cur
```

## 纪律

- 探针库**零业务语义**：op 全部是对公开 API 的通用调用形态；具体游戏
  语义（哪个节点、哪个方法）永远来自步骤/IR 数据面。
- `resolve_query` 读不到必须返回 **null**（比对必挂），不许抛错或返回
  空串冒充——与 query_state 契约同款 fail-safe。
- 每个被测项目的接线映射（步骤文本 → op/方法调用）由人维护在
  harness 内；本库只提供原语。
- 变异纪律：本库不消费 case.values、不做任何期望判断——期望面只在
  harness 的 `check_expectations`（数据面单一化，typed cell 双重语义
  纪律见 testing_api.gd.md）。
