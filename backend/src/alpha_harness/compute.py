"""Where the app's heavy arithmetic runs: a few threads at the bottom of the CPU's queue.

The sampler's asks, the Portfolio's correlation grids, the Evolution Lab's independence checks
and the Power Pool's correlations are seconds of pure arithmetic. They used to share
``asyncio.to_thread``'s pool with quick work — a DuckDB read, decoding a page of JSON — at
the process's own priority, so a long correlation read competed with whatever the person was
doing on the machine.

Here they get threads of their own, as many as the Resource profile's math threads, and each
thread lowers its own priority as it starts: on Linux to ``SCHED_IDLE`` under Lean and
Balanced, so it runs only on CPU time nothing else wants, and by ten nice steps under Max.
Linux schedules threads, not processes, so this leaves the event loop thread — which polls
BRAIN and answers the UI — at the process's priority. Elsewhere the threads keep the
process's priority; the thread count still bounds them.

Threads rather than a worker process: the sampler's studies live in this process's memory,
a second interpreter would hold a second copy of numpy, Optuna and DuckDB, and the cost being
bounded is CPU time, which a process boundary does not reduce.
"""

import asyncio
import contextlib
import functools
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

import structlog

from . import resources

if TYPE_CHECKING:
    from collections.abc import Callable

log = structlog.get_logger(__name__)

#: Nice steps a compute thread adds under Max, where it is not made idle.
MAX_PROFILE_NICE = 10

_executor: ThreadPoolExecutor | None = None
_lock = threading.Lock()


def _lower_this_thread(idle: bool) -> None:
    """Drop the calling thread's priority. Never raises: a refusal leaves it as it was."""
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(OSError, AttributeError):
        if idle:
            # Pid 0 is the calling thread: Linux applies scheduling policy per thread.
            os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
            return
        tid = threading.get_native_id()
        os.setpriority(
            os.PRIO_PROCESS, tid, os.getpriority(os.PRIO_PROCESS, tid) + MAX_PROFILE_NICE
        )


def executor() -> ThreadPoolExecutor:
    """The compute pool, made on first use with the profile in force then."""
    global _executor
    with _lock:
        if _executor is None:
            profile = resources.current()
            workers = resources.BUDGETS[profile].math_threads or os.cpu_count() or 2
            _executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="harness-compute",
                initializer=_lower_this_thread,
                initargs=(profile != "max",),
            )
            log.info("compute.started", profile=profile, threads=workers)
        return _executor


async def run[T](fn: Callable[..., T], /, *args: Any) -> T:
    """``asyncio.to_thread`` for heavy arithmetic: ``fn(*args)`` on a compute thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor(), functools.partial(fn, *args))


def shutdown() -> None:
    """Let running jobs finish and stop the pool; the next :func:`run` makes a new one."""
    global _executor
    with _lock:
        pool, _executor = _executor, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
