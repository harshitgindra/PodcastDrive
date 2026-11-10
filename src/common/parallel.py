"""Small concurrency helpers shared by source-specific pipelines."""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def iter_parallel(
    function: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int,
) -> Generator[tuple[T, Future[R]], None, None]:
    """Submit work concurrently and yield each source item with its completed future.

    Results are yielded in completion order. The caller owns result/error handling,
    including whether to stop early or continue aggregating failures.

    Stopping early (``break`` or ``return``) abandons this generator, and the pool
    is only shut down when the generator is closed. CPython closes it promptly
    when the last reference goes away, but that is an implementation detail, so a
    caller that may stop early should close it explicitly::

        with closing(iter_parallel(work, items, max_workers=4)) as completed:
            for item, future in completed:
                ...

    Closing cancels work that has not started yet; work already running is still
    waited for, so the pool never outlives this call.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(function, item): item for item in items}
        try:
            for future in as_completed(futures):
                yield futures[future], future
        finally:
            for future in futures:
                future.cancel()
