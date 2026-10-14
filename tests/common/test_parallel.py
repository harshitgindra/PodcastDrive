"""Tests for the shared parallel execution primitive."""

import threading

from common.parallel import iter_parallel


def test_iter_parallel_yields_items_in_completion_order_with_futures():
    second_finished = threading.Event()

    def work(item):
        if item == "first":
            assert second_finished.wait(timeout=2)
        else:
            second_finished.set()
        return item.upper()

    completed = list(iter_parallel(work, ["first", "second"], max_workers=2))

    assert [item for item, _ in completed] == ["second", "first"]
    assert [future.result() for _, future in completed] == ["SECOND", "FIRST"]


def test_iter_parallel_yields_future_exceptions_to_the_caller():
    def work(item):
        if item == "bad":
            raise ValueError("worker failed")
        return item

    completed = dict(iter_parallel(work, ["bad", "good"], max_workers=2))

    assert completed["good"].result() == "good"
    try:
        completed["bad"].result()
    except ValueError as exc:
        assert str(exc) == "worker failed"
    else:
        raise AssertionError("future result should preserve the worker exception")
