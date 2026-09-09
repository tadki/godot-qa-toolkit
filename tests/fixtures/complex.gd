extends Node


func simple(a: int) -> int:
	return a + 1


func complex(items: Array, flag: bool) -> int:
	var total := 0
	for item in items:
		if flag:
			if item > 0:
				total += item
			elif item < -10:
				total -= 1
		else:
			total += 2
	while total > 100:
		total -= 100
	return total
