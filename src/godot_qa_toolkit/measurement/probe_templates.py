"""SEE-1594 BTIDY（零行为拆分）：measurement 探针/钩子 GDScript 模板
与 project.godot [autoload] 注入补丁——自 orchestrator.py 原行段物理迁移，
内容逐字节不变。"""

# §SPEC-046：flag 形态的统一落盘由注入的 finalizer autoload 承担——GUT
# 退出（SceneTree 拆卸 _exit_tree）时逐 sink 排空标记数组（每 sink 一次
# open+写循环），落盘次数 = 文件数、与命中事件数无关。仅存在于插桩窗口，
# finally 恢复零残留。
_AUTOLOAD_NAME = "GqtCovFlusher"
_FLUSHER_RELPATH = "addons/gqt_cov_flusher.gd"
# §SPEC-062A：finalizer 经 GUT pre_run_script 通道注册（root index 0，
# 退出序最后 = R5 drain 顺序纪律），测量期 project.godot 零改动
_PRE_HOOK_RELPATH = "addons/gqt_cov_pre_hook.gd"
_PRE_HOOK_GD = """extends "res://addons/gut/hook_script.gd"
# qa-toolkit measurement coverage finalizer registration hook. Registers the
# drain node as root's FIRST child so its _exit_tree runs LAST (reverse
# exit order = R5 drain discipline) — without touching project.godot.
func run():
	var root: Node = (gut.get_tree().root if gut != null
			else Engine.get_main_loop().root)
	var node: Node = load("res://addons/gqt_cov_flusher.gd").new()
	node.name = "GqtCovFlusherNode"
	root.add_child(node)
	root.move_child(node, 0)
"""
_FLUSHER_GD = """extends Node
# qa-toolkit measurement flag-mode probe finalizer (auto-injected, never in
# production). Drains every registered per-file flag array to its sink:
# one open + one write loop per file — sink I/O is bounded by file count
# and never grows with hit event count (§SPEC-046).
#
# §SPEC-062A：drain 挂在 NOTIFICATION_PREDELETE 而非 _exit_tree——GUT 收尾
# 会把本节点移出树（_exit_tree 早于引擎关停），但对象作为孤儿节点在进程
# 末段（autoload _exit_tree 相位之后）才被析构；PREDELETE 时 marks 已含
# 关停期命中（隔离实验实证：sink 双标记全捕获），不重演 R5 drain 顺序
# 丢 hits 缺陷。
func _notification(what: int) -> void:
\tif what != NOTIFICATION_PREDELETE:
\t\treturn
\tif not Engine.has_meta("gqt_cov_marks"):
\t\treturn
\tvar d: Dictionary = Engine.get_meta("gqt_cov_marks")
\tfor sink_name in d.keys():
\t\tvar arr: Array = d[sink_name]
\t\tvar f = FileAccess.open("user://" + sink_name, FileAccess.READ_WRITE)
\t\tif f == null:
\t\t\tf = FileAccess.open("user://" + sink_name, FileAccess.WRITE)
\t\telse:
\t\t\tf.seek_end()
\t\tif f == null:
\t\t\tpush_error("gqt measure coverage probe sink open failed: user://" + sink_name)
\t\t\tcontinue
\t\tfor i in arr.size():
\t\t\tif arr[i] == 1:
\t\t\t\tf.store_line(str(i))
\t\tf.close()
"""

# §SPEC-083 试验（默认关闭）：归属形态的 pre-hook——在既有 finalizer 注册
# 之上，用 GUT 的 start_script/start_test/end_test/end_script 信号维护
# 「当前上下文」：Engine meta `gqt_cov_slot` 单整数槽位（0 无主 / 正数 =
# 组合注册表下标+1，测试与脚本身份共用一个注册表，方向②）。R1 起测试
# 窗口外的命中落「脚本级」车道而非一律无主。
# run_uuid 在注入时以占位符替换（每臂独立）。
_ATTR_RUN_UUID_PLACEHOLDER = "__GQT_RUN_UUID__"
_ATTR_PRE_HOOK_GD = """extends "res://addons/gut/hook_script.gd"
# qa-toolkit measurement attribution hook (experimental, SEE-1581, 方向②).
# Registers the drain node (same R5 discipline as the default hook) and
# maintains the current context as a single positive slot index over one
# combined registry (test/script identities share the index space), so the
# probe hot path is one meta read + two array writes.
var _gqt_ctx: Array = []
var _gqt_idx_by_key: Dictionary = {}
var _gqt_cur_script_slot: int = 0

func run():
\tvar root: Node = (gut.get_tree().root if gut != null
\t\t\telse Engine.get_main_loop().root)
\tvar node: Node = load("res://addons/gqt_cov_flusher.gd").new()
\tnode.name = "GqtCovFlusherNode"
\troot.add_child(node)
\troot.move_child(node, 0)
\tEngine.set_meta("gqt_cov_ctx_registry", _gqt_ctx)
\tEngine.set_meta("gqt_cov_attr_registry_sink",
\t\t\t"qa-cov-attr-tests-__GQT_RUN_UUID__-" + str(OS.get_process_id()) + ".txt")
\tEngine.set_meta("gqt_cov_slot", 0)
\tEngine.set_meta("gqt_cov_slot_max", 0)
\t# 方向③：无主命中的阶段桶（探针仅在 slot==0 时读）
\t# 0 bootstrap（首个 start_script 之前）/ 1 script_gap（脚本之间）/
\t# 2 shutdown（GUT end_run 之后）/ 3 other（防御性默认）
\tEngine.set_meta("gqt_cov_phase", 0)
\tif gut != null:
\t\tgut.start_script.connect(_on_start_script)
\t\tgut.start_test.connect(_on_start_test)
\t\tgut.end_test.connect(_on_end_test)
\t\tgut.end_script.connect(_on_end_script)
\t\tgut.end_run.connect(_on_end_run)

func _on_end_run() -> void:
\tEngine.set_meta("gqt_cov_phase", 2)

func _gqt_register(kind: String, identity: String) -> int:
\tvar key: String = kind + "\\t" + identity
\tvar idx: int = _gqt_idx_by_key.get(key, -1)
\tif idx < 0:
\t\tidx = _gqt_ctx.size()
\t\t_gqt_ctx.append(key)
\t\t_gqt_idx_by_key[key] = idx
\t\tEngine.set_meta("gqt_cov_slot_max", idx + 1)
\t\tif Engine.has_meta("gqt_cov_attr"):
\t\t\tvar d: Dictionary = Engine.get_meta("gqt_cov_attr")
\t\t\tfor sink in d.keys():
\t\t\t\tvar h: Array = d[sink]
\t\t\t\th[0].resize(idx + 1)
\t\t\t\th[1].resize(idx + 1)
\t\t\t\th[0][idx] = 0
\t\t\t\th[1][idx] = 0
\treturn idx

func _on_start_script(coll_script) -> void:
\t# §SPEC-030/D2：脚本身份用完整路径（GUT CollectedScript.path 是
\t# res:// 全路径；get_filename_and_inner() 只给 basename——同 basename
\t# 不同目录的同名测试会在注册表层折叠成单键，verdict 消歧与 A12
\t# 投影归属的全路径精确全等分支都依赖完整路径）。
\tvar sPath: String = str(coll_script.path)
\tif sPath.is_empty():
\t\tsPath = str(coll_script.get_filename_and_inner())
\tvar idx: int = _gqt_register("script", sPath)
\t_gqt_cur_script_slot = idx + 1
\tEngine.set_meta("gqt_cov_phase", 1)
\tEngine.set_meta("gqt_cov_slot", idx + 1)

func _on_start_test(test_name) -> void:
\tvar base: String = ""
\tif _gqt_cur_script_slot > 0:
\t\tvar sk: String = str(_gqt_ctx[_gqt_cur_script_slot - 1])
\t\tif sk.begins_with("script\\t"):
\t\t\tbase = sk.substr(7) + "::"
\tvar idx: int = _gqt_register("test", base + str(test_name))
\tEngine.set_meta("gqt_cov_slot", idx + 1)

func _on_end_test() -> void:
\tEngine.set_meta("gqt_cov_slot", _gqt_cur_script_slot)

func _on_end_script() -> void:
\t_gqt_cur_script_slot = 0
\tEngine.set_meta("gqt_cov_slot", 0)
\tEngine.set_meta("gqt_cov_phase", 1)
"""

# §SPEC-083 R2：创建点包装的运行时上下文（注入 res://addons/gqt_cov_ctx.gd）。
# 归属注册表用 weakref 持目标对象；回调触发时按创建时捕获的 tag 因果归属。
# tag 编码与钩子一致：>0 测试下标+1，<0 脚本下标+1 的负数。
_ATTR_CTX_RELPATH = "addons/gqt_cov_ctx.gd"
_ATTR_CTX_GD = """extends RefCounted
# NOTE: GDScript 4 has no try/finally — restore is best-effort straight-line.
# qa-toolkit measurement causal-attribution context (experimental, SEE-1581 R2).
# 创建点包装：捕获创建时的 tag（当前上下文槽位），回调触发时把 slot
# 置回该 tag，回调结束（或下一个 idle 相位）恢复原值。tag 编码与钩子一致：
# 0 无主 / 正数 = 上下文槽位（组合注册表下标+1）。

static func current_tag() -> int:
\treturn Engine.get_meta("gqt_cov_slot", 0)

static func _enter(tag: int) -> int:
\tvar prev: int = Engine.get_meta("gqt_cov_slot", 0)
\tEngine.set_meta("gqt_cov_slot", tag)
\treturn prev

static func _restore(prev: int) -> void:
\tEngine.set_meta("gqt_cov_slot", prev)

static func _run_tagged(tag: int, obj_ref, method: String, args: Array) -> void:
\tvar obj = obj_ref.get_ref() if obj_ref is WeakRef else obj_ref
\tif obj == null:
\t\treturn
\tvar prev: int = _enter(tag)
\tobj.callv(method, args)
\t_restore(prev)

static func call_deferred_tagged(obj, call_args: Array) -> void:
\tif call_args.is_empty():
\t\treturn
\tvar tag: int = current_tag()
\tvar wr := weakref(obj)
\tvar method: String = str(call_args[0])
\tvar args: Array = call_args.slice(1)
\t(func(): _run_tagged(tag, wr, method, args)).call_deferred()

static func _on_timeout(tag: int) -> void:
\tvar prev: int = _enter(tag)
\t# timeout 的其余订阅者在本帧同步执行；恢复延到 idle，避免污染后续归属
\t(func(): _restore(prev)).call_deferred()

static func create_timer_tagged(tree, time_sec: float,
\t\tprocess_always: bool = true, process_in_physics: bool = false,
\t\tignore_time_scale: bool = false) -> SceneTreeTimer:
\tvar tag: int = current_tag()
\tvar t: SceneTreeTimer = tree.create_timer(time_sec, process_always,
\t\t\tprocess_in_physics, ignore_time_scale)
\t# 先于业务侧 connect，确保回调执行时 ctx 已是创建点上下文
\tt.timeout.connect(func(): _on_timeout(tag))
\treturn t
"""

# 归属形态 finalizer：在既有 hits drain 之后，按 attr sink 落盘
# (test_idx 命中行 + 已归属/无主命中事件计数)，并把测试注册表写入
# 每进程一个的注册表 sink。所有写入仍是「每文件一次 open + 一次循环」，
# I/O 与命中事件数无关（§SPEC-046 口径不破）。
_ATTR_FLUSHER_GD = """extends Node
# qa-toolkit measurement per-test attribution finalizer (experimental, SEE-1581).
func _notification(what: int) -> void:
\tif what != NOTIFICATION_PREDELETE:
\t\treturn
\tif Engine.has_meta("gqt_cov_marks"):
\t\tvar d: Dictionary = Engine.get_meta("gqt_cov_marks")
\t\tfor sink_name in d.keys():
\t\t\tvar arr: Array = d[sink_name]
\t\t\tvar f = FileAccess.open("user://" + sink_name, FileAccess.READ_WRITE)
\t\t\tif f == null:
\t\t\t\tf = FileAccess.open("user://" + sink_name, FileAccess.WRITE)
\t\t\telse:
\t\t\t\tf.seek_end()
\t\t\tif f == null:
\t\t\t\tpush_error("gqt measure coverage probe sink open failed: user://" + sink_name)
\t\t\t\tcontinue
\t\t\tfor i in arr.size():
\t\t\t\tif arr[i] == 1:
\t\t\t\t\tf.store_line(str(i))
\t\t\tf.close()
\tif Engine.has_meta("gqt_cov_attr"):
\t\tvar a: Dictionary = Engine.get_meta("gqt_cov_attr")
\t\tfor attr_sink in a.keys():
\t\t\tvar h: Array = a[attr_sink]
\t\t\tvar af = FileAccess.open("user://" + attr_sink, FileAccess.READ_WRITE)
\t\t\tif af == null:
\t\t\t\taf = FileAccess.open("user://" + attr_sink, FileAccess.WRITE)
\t\t\telse:
\t\t\t\taf.seek_end()
\t\t\tif af == null:
\t\t\t\tpush_error("gqt attribution sink open failed: user://" + attr_sink)
\t\t\t\tcontinue
\t\t\tvar seen: Array = h[0]
\t\t\tfor i in seen.size():
\t\t\t\tif seen[i] == 1:
\t\t\t\t\taf.store_line("test " + str(i))
\t\t\tvar cnt: Array = h[1]
\t\t\tfor i in cnt.size():
\t\t\t\tif cnt[i] > 0:
\t\t\t\t\taf.store_line("cnt " + str(i) + " " + str(cnt[i]))
\t\t\tvar fctx: Dictionary = h[2]
\t\t\tfor probe_id in fctx.keys():
\t\t\t\tvar slots: Dictionary = fctx[probe_id]
\t\t\t\tfor slot in slots.keys():
\t\t\t\t\taf.store_line("fn " + str(probe_id) + " " + str(slot))
\t\t\tvar ucounts: Dictionary = h[3]
\t\t\tfor pid in ucounts.keys():
\t\t\t\taf.store_line("unattr " + str(pid) + " " + str(ucounts[pid]))
\t\t\tvar ph: Array = h[4]
\t\t\taf.store_line("ph " + str(ph[0]) + " " + str(ph[1])
\t\t\t\t\t+ " " + str(ph[2]) + " " + str(ph[3]))
\t\t\taf.close()
\tif Engine.has_meta("gqt_cov_attr_registry_sink"):
\t\tvar rsink: String = Engine.get_meta("gqt_cov_attr_registry_sink")
\t\tvar rf = FileAccess.open("user://" + rsink, FileAccess.READ_WRITE)
\t\tif rf == null:
\t\t\trf = FileAccess.open("user://" + rsink, FileAccess.WRITE)
\t\telse:
\t\t\trf.seek_end()
\t\tif rf != null:
\t\t\tif Engine.has_meta("gqt_cov_ctx_registry"):
\t\t\t\tvar keys: Array = Engine.get_meta("gqt_cov_ctx_registry")
\t\t\t\tfor i in keys.size():
\t\t\t\t\trf.store_line("slot " + str(i) + "\\t" + str(keys[i]))
\t\t\trf.close()
"""


def _patch_autoload(text: str, entry: str) -> str:
    """project.godot 的 [autoload] 段首插入一行 autoload 注册；段缺失则
    追加新段。只动文本结构，不改既有条目。

    置首而非置尾：autoload 退出顺序与加载顺序相反——finalizer 必须最后
    退出，才能排空其他 autoload 在各自 _exit_tree 里产生的关停期命中
    （R5 实证：置尾时 _exit_tree 覆盖整段丢失）。"""
    lines = text.splitlines(keepends=True)
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[autoload]":
            start = i
            break
    if start is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + f"\n[autoload]\n{entry}\n"
    lines.insert(start + 1, entry + "\n")
    return "".join(lines)
