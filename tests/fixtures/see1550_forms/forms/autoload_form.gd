# Form 3: autoload singleton — wired in project.godot [autoload]; its const
# preload pulls forms/preload_boot.gd into the boot wiring graph.
extends Node

const BootPiece := preload("res://forms/preload_boot.gd")

var _boot: RefCounted


func _ready() -> void:
	_boot = BootPiece.new()


func score() -> int:
	return 2 + _boot.factor()
