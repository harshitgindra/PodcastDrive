"""Small concurrency helpers shared by source-specific pipelines."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def iter_parallel(
    function: Callable[[T], R],
    items: Iterable[T],
    *,
    max_workers: int,
) -> Iterator[tuple[T, Future[R]]]:
    """Submit work concurrently and yield each source item with its completed future.

    Results are yielded in completion order. The caller owns result/error handling,
    including whether to stop early or continue aggregating failures.
    """
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(function, item): item for item in items}
        for future in as_completed(futures):
            yield futures[future], future
