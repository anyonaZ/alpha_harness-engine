"""The backend's CPU by the hour, over the last day, for the Resources panel.

Read from the process's own counters every :data:`SAMPLE_SECONDS`, so the panel can show
what a day of running cost without being open all day. Kept in memory: a restart starts a
new day, as the counters themselves do.
"""

import asyncio
import contextlib
import math
import os
import time
from collections import deque
from dataclasses import dataclass, replace

from . import compute

#: Between two readings. Twelve wake-ups an hour; an hour's figure is exact either way,
#: since each reading carries everything since the one before.
SAMPLE_SECONDS = 300.0
HOURS_KEPT = 24
HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class Hour:
    #: Start of the clock hour, in seconds since the epoch.
    start: float
    #: How much of the hour the backend was running and measured.
    seconds: float
    cpu_seconds: float
    #: Of which spent in the compute pool's jobs: the sampler, Portfolio and correlations.
    compute_cpu_seconds: float


def _read() -> tuple[float, float, float]:
    times = os.times()
    return time.time(), times.user + times.system, compute.usage()["cpu_seconds"]


class UsageHistory:
    def __init__(self) -> None:
        self._hours: deque[Hour] = deque(maxlen=HOURS_KEPT)
        self._last = _read()
        self._task: asyncio.Task[None] | None = None

    def sample(self) -> None:
        now = _read()
        self._hours = _spread(self._hours, self._last, now)
        self._last = now

    def hours(self) -> list[Hour]:
        """The kept hours, oldest first, the current one counted up to now."""
        return list(_spread(self._hours, self._last, _read()))

    def start(self) -> None:
        self._last = _read()
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="usage-history")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            self.sample()


def _spread(
    kept: deque[Hour], before: tuple[float, float, float], after: tuple[float, float, float]
) -> deque[Hour]:
    """``kept`` with the CPU between two readings added, split across the clock hours they
    span in proportion to time. Returns a new deque; ``kept`` is left as it was."""
    hours = deque(kept, maxlen=HOURS_KEPT)
    start, end = before[0], after[0]
    if end <= start:
        return hours
    cpu, maths = max(0.0, after[1] - before[1]), max(0.0, after[2] - before[2])
    moment = start
    while moment < end:
        hour = math.floor(moment / HOUR) * HOUR
        upto = min(end, hour + HOUR)
        share = (upto - moment) / (end - start)
        last = hours.pop() if hours and hours[-1].start == hour else Hour(hour, 0.0, 0.0, 0.0)
        hours.append(
            replace(
                last,
                seconds=last.seconds + upto - moment,
                cpu_seconds=last.cpu_seconds + cpu * share,
                compute_cpu_seconds=last.compute_cpu_seconds + maths * share,
            )
        )
        moment = upto
    return hours


history = UsageHistory()
