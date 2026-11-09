# Form 7: startup composition root — main_scene root script; it wires child
# pieces together during scene instantiation (startup composition).
extends Node2D

const Piece := preload("res://forms/preload_late.gd")


func total() -> int:
	var piece: RefCounted = Piece.new()
	return 40 + piece.factor()
