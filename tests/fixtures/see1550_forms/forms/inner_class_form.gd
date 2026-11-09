# Form 5: inner class — mutant lands inside the inner-class method body;
# reachability goes through the outer entry point.
extends RefCounted

class Counter:
	func value() -> int:
		return 9 - 1


func make_counter() -> Counter:
	return Counter.new()
