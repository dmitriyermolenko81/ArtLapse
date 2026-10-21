"""Constrained temporal budget with deterministic integer rounding."""

import math


def distribute(
    minimum: list[int], preferred: list[float], maximum: list[int], target: int
) -> list[int]:
    if not len(minimum) == len(preferred) == len(maximum):
        raise ValueError("Budget vector lengths differ")
    if any(a < 0 or b < a or p <= 0 for a, p, b in zip(minimum, preferred, maximum, strict=True)):
        raise ValueError("Invalid budget constraints")
    if not sum(minimum) <= target <= sum(maximum):
        raise ValueError("Infeasible frame budget")
    low, high = 0.0, 1.0

    def amounts(multiplier: float) -> list[float]:
        return [
            max(a, min(b, p * multiplier))
            for a, p, b in zip(minimum, preferred, maximum, strict=True)
        ]

    while sum(amounts(high)) < target:
        high *= 2
    for _ in range(80):
        mid = (low + high) / 2
        if sum(amounts(mid)) <= target:
            low = mid
        else:
            high = mid
    real = amounts(low)
    result = [math.floor(x) for x in real]
    remainder = target - sum(result)
    for i in sorted(range(len(result)), key=lambda i: (-(real[i] % 1), i)):
        if remainder and result[i] < maximum[i]:
            result[i] += 1
            remainder -= 1
    if remainder:
        raise ValueError("Could not round frame budget")
    return result
