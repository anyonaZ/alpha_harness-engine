"""What the app is costing this computer, for the Resources panel in Settings."""

import os
import sys
import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter

from .. import compute, resources
from .. import usage as usage_history
from ..schemas import Out
from .deps import State

router = APIRouter(prefix="/api/resources", tags=["resources"])

#: When this process started, on the clock ``time.monotonic`` reads.
_STARTED = time.monotonic()


class HourUsage(Out):
    """One clock hour of the last day."""

    started_at: datetime
    #: How much of the hour the backend was running: the first and current ones are partial.
    seconds: float
    cpu_seconds: float
    #: Of which spent in the compute pool's jobs.
    compute_cpu_seconds: float


class ResourceUsage(Out):
    """Totals since the backend started. The panel works out rates from two readings."""

    profile: resources.Profile
    #: Seconds on the monotonic clock when read: the panel divides CPU by the change in it.
    read_at: float
    uptime_seconds: float
    #: CPU time of the whole backend, every thread, since start.
    process_cpu_seconds: float
    #: Of which spent in the compute pool's jobs: the sampler, Portfolio and correlations.
    compute_cpu_seconds: float
    compute_threads: int | None
    compute_priority: Literal["idle", "lowered", "refused", "unchanged", "pending"]
    #: numpy / BLAS threads in force (fixed at start), and DuckDB's (``None``: every core).
    math_threads: int | None
    duckdb_threads: int | None
    #: The process's niceness, where the system has one.
    nice: int | None
    cpus: int
    awake: Literal["held", "idle", "unavailable"]
    #: CPU by clock hour over the last day, oldest first, the current hour so far last.
    hours: list[HourUsage]


@router.get("")
async def usage(state: State) -> ResourceUsage:
    times = os.times()
    pool = compute.usage()
    math = os.environ.get("OPENBLAS_NUM_THREADS", "")
    return ResourceUsage(
        profile=resources.current(),
        read_at=time.monotonic(),
        uptime_seconds=time.monotonic() - _STARTED,
        process_cpu_seconds=times.user + times.system,
        compute_cpu_seconds=pool["cpu_seconds"],
        compute_threads=pool["threads"],
        compute_priority=pool["priority"],
        math_threads=int(math) if math.isdigit() else None,
        duckdb_threads=resources.budget().duckdb_threads,
        nice=None if sys.platform == "win32" else os.getpriority(os.PRIO_PROCESS, 0),
        cpus=os.cpu_count() or 1,
        awake=state.engine.awake_state,
        hours=[
            HourUsage(
                started_at=datetime.fromtimestamp(hour.start, UTC),
                seconds=hour.seconds,
                cpu_seconds=hour.cpu_seconds,
                compute_cpu_seconds=hour.compute_cpu_seconds,
            )
            for hour in usage_history.history.hours()
        ],
    )
