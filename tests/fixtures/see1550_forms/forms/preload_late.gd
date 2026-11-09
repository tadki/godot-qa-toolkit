# Form 2b: late preload — const-preloaded by a GUT test script, so it enters
# the dependency graph only when GUT parses the test (after boot).
extends RefCounted


func factor() -> int:
	return 5 + 1
