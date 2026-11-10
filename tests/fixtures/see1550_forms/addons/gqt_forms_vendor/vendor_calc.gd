# §SPEC-017 vendored 形态：addons/ 树默认扫描域不含（无自动枚举变异源），
# 显式指定为变异目标时必须可注入并被测试杀死。`*` 位点同时是 §SPEC-014
# mdr_expr 扫描面补全的端到端消费方。
extends RefCounted


func add_part(a: int) -> int:
	return a + 6


func scale(b: int) -> int:
	return b * 4
