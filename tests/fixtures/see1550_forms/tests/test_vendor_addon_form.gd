# §SPEC-017 vendored 显式目标的杀手测试（testscan 经 preload 字面量命中）。
extends GutTest

const Vendor := preload("res://addons/gqt_forms_vendor/vendor_calc.gd")


func test_vendor_add_part_form() -> void:
	var inst: RefCounted = Vendor.new()
	assert_eq(inst.add_part(1), 7)


func test_vendor_scale_form() -> void:
	var inst: RefCounted = Vendor.new()
	assert_eq(inst.scale(3), 12)
