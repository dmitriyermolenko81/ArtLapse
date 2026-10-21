import random

import pytest

from sculptcut.budget import distribute


def test_budget_properties() -> None:
    rng = random.Random(42)
    for _ in range(300):
        count = rng.randint(1, 20)
        minimum = [rng.randint(1, 10) for _ in range(count)]
        maximum = [m + rng.randint(0, 100) for m in minimum]
        preferred = [rng.uniform(1, 50) for _ in minimum]
        target = rng.randint(sum(minimum), sum(maximum))
        result = distribute(minimum, preferred, maximum, target)
        assert sum(result) == target
        assert all(a <= x <= b for a, x, b in zip(minimum, result, maximum, strict=True))
        assert distribute(minimum, preferred, maximum, target) == result
    with pytest.raises(ValueError, match="Infeasible"):
        distribute([5], [10], [20], 1)
