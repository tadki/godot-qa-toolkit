# GUT suite for the SEE-1550 8-form fixture (one test per killable form).
extends GutTest

const Late := preload("res://forms/preload_late.gd")


func test_runtime_load_form() -> void:
	var script: GDScript = load("res://forms/runtime_load.gd")
	var inst: RefCounted = script.new()
	assert_eq(inst.factor(), 11)


func test_preload_boot_form_via_autoload() -> void:
	# 启动期 preload：autoload 单例在引擎装配期已构造 BootPiece
	var auto := get_node("/root/FormAuto")
	assert_eq(auto.score(), 6)


func test_preload_late_form() -> void:
	var inst: RefCounted = Late.new()
	assert_eq(inst.factor(), 6)


func test_class_name_form() -> void:
	var inst: RefCounted = FormClass.new()
	assert_eq(inst.factor(), 14)


func test_inner_class_form() -> void:
	var script: GDScript = load("res://forms/inner_class_form.gd")
	var outer: RefCounted = script.new()
	var counter: RefCounted = outer.make_counter()
	assert_eq(counter.value(), 8)


func test_scene_script_form() -> void:
	var scene: PackedScene = load("res://forms/scene.tscn")
	var node: Node2D = scene.instantiate()
	add_child(node)
	assert_eq(node.label(), "scene-ok")
	assert_eq(node.score(), 11)


func test_composition_root_form() -> void:
	var scene: PackedScene = load("res://forms/main.tscn")
	var node: Node2D = scene.instantiate()
	add_child(node)
	assert_eq(node.total(), 46)
