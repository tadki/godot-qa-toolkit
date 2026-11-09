# Form 8 (negative control): orphan — nothing references this script
# (no autoload wiring, no scene ext_resource, no tests/ literal, no class_name).
# Mutants here must SURVIVE and the report must disclose suspected_orphan.
extends RefCounted


func factor() -> int:
	return 99 + 1
