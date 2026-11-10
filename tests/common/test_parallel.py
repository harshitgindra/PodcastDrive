"""Tests for the shared parallel execution primitive."""

import threading
import time
from contextlib import closing

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


def test_closing_after_an_early_break_cancels_work_that_never_started():
    started: list[int] = []
    lock = threading.Lock()

    def work(item):
        with lock:
            started.append(item)
        if item != 0:
            # Keep the single worker busy so the queue cannot drain while the
            # caller is deciding to stop.
            time.sleep(0.05)
        return item

    items = list(range(12))
    with closing(iter_parallel(work, items, max_workers=1)) as completed:
        for _ in completed:
            break

    assert started, "the first item should have run"
    assert len(started) < len(items), f"queued work was not cancelled: {started}"


def test_closing_waits_for_work_that_is_already_running():
    release = threading.Event()
    finished: list[str] = []
    lock = threading.Lock()

    def work(item):
        if item == "slow":
            assert release.wait(timeout=5)
        with lock:
            finished.append(item)
        return item

    with closing(iter_parallel(work, ["slow", "fast"], max_workers=2)) as completed:
        for item, _ in completed:
            assert item == "fast"
            break
        release.set()

    # Leaving the block must not return before the running worker is joined.
    assert finished == ["fast", "slow"]
