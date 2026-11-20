import pytest

from app.ratelimit import SlidingWindowLimiter


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_sliding_window_blocks_then_recovers():
    clock = Clock()
    lim = SlidingWindowLimiter(2, 60, clock=clock)
    assert lim.check("a") is None and lim.check("a") is None
    assert lim.check("a") == pytest.approx(60.0)
    assert lim.check("b") is None  # keys are independent
    clock.t += 60.01
    assert lim.check("a") is None


def test_retry_after_peeks_without_consuming():
    lim = SlidingWindowLimiter(1, 10, clock=Clock())
    assert lim.retry_after("k") is None and lim.retry_after("k") is None
    lim.consume("k")
    assert lim.retry_after("k") == pytest.approx(10.0)


def test_key_memory_is_bounded():
    lim = SlidingWindowLimiter(1, 10, clock=Clock(), max_keys=3)
    for key in "abcde":
        lim.consume(key)
    assert len(lim._events) == 3
