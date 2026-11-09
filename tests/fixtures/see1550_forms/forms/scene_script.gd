# Form 6: PackedScene scene script — attached to forms/scene.tscn root;
# reachability goes through the scene resource.
extends Node2D


func label() -> String:
	return "scene-ok"


func score() -> int:
	var base := 6
	return base + 5
