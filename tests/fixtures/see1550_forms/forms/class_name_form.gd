# Form 4: class_name — global class registry; tests reference it by
# identifier (no explicit load path anywhere in tests/).
class_name FormClass
extends RefCounted


func factor() -> int:
	return 7 + 7
