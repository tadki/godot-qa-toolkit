# Form 2a: boot-time preload — const-preloaded by the autoload script, so the
# engine parses this script during startup assembly (before any test runs).
extends RefCounted


func factor() -> int:
	return 3 + 1
