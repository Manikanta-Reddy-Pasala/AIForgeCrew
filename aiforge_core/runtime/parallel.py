"""Parallel fan-out / fan-in helper — ADK 2.0 ``JoinNode`` shape
without depending on ADK 2.0 itself.

KISS: thread-pool based. One ``fan_out_join(tasks, joiner)`` call
runs every task concurrently (bounded), then calls ``joiner`` on the
ordered list of results. Used wherever a single agent decision can
be split into N independent sub-queries.

Caller responsibilities:
- Tasks are ``Callable[[], R]`` — fully bound, no shared state.
- ``joiner(results: list[R]) -> Any`` produces the merged value.

Future-compat: when we cut over to ADK 2.0 ``Workflow(BaseNode)``,
each task here becomes a ``Node`` and ``joiner`` becomes a
``JoinNode`` — interface stays the same.

Public surface:
- ``fan_out_join(tasks, joiner=None, *, max_workers=4, timeout_s=120)``
- ``Result`` dataclass for typed results
"""
from __future__ import annotations

import concurrent.futures as _cf
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")
R = TypeVar("R")


@dataclass
class Result:
    """One sub-task outcome."""
    index: int
    ok: bool
    value: Any = None
    error: str | None = None
    wall_s: float = 0.0


def fan_out_join(
    tasks: Iterable[Callable[[], T]],
    joiner: Callable[[list[Result]], R] | None = None,
    *,
    max_workers: int = 4,
    timeout_s: float = 120.0,
) -> R | list[Result]:
    """Run ``tasks`` in parallel; pass ordered ``Result`` list to
    ``joiner``. Returns the joiner output (or raw results when no
    joiner supplied).
    """
    import time
    task_list = list(tasks)
    if not task_list:
        return joiner([]) if joiner is not None else []

    n = len(task_list)
    workers = max(1, min(int(max_workers), n))
    results: list[Result] = [
        Result(index=i, ok=False) for i in range(n)
    ]

    with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_timed, task): i for i, task in enumerate(task_list)
        }
        for fut in _cf.as_completed(futures, timeout=timeout_s):
            i = futures[fut]
            try:
                value, wall_s = fut.result()
                results[i] = Result(index=i, ok=True, value=value,
                                    wall_s=wall_s)
            except Exception as exc:
                results[i] = Result(
                    index=i, ok=False, error=str(exc)[:300],
                )

    if joiner is None:
        return results
    return joiner(results)


def _timed(fn: Callable[[], T]) -> tuple[T, float]:
    import time
    t0 = time.time()
    out = fn()
    return out, round(time.time() - t0, 3)


# ───────── Convenience joiners ─────────────────────────────────────


def joiner_concat_strings(results: list[Result]) -> str:
    """Join successful str results in original index order."""
    return "\n\n---\n\n".join(
        str(r.value) for r in results if r.ok and r.value is not None
    )


def joiner_first_success(results: list[Result]) -> Any:
    """First ok result, or None when all failed."""
    for r in results:
        if r.ok:
            return r.value
    return None


def joiner_majority_vote(results: list[Result]) -> Any:
    """Pick the value that appears most often among successes.
    KISS: simple counter on str(value). Ties broken by lowest index."""
    from collections import Counter
    successes = [r for r in results if r.ok]
    if not successes:
        return None
    counts = Counter(str(r.value) for r in successes)
    top, _ = counts.most_common(1)[0]
    for r in successes:
        if str(r.value) == top:
            return r.value
    return None
