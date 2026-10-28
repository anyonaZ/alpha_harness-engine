"""How much of the computer local work may use: the Resource profile.

BRAIN throughput is the same in every profile: slots, batching and polling never change. A
profile only bounds what this machine spends on its own arithmetic — numpy and BLAS threads,
DuckDB's threads and memory, the worker threads ``asyncio.to_thread`` hands work to, the CPU
priority of the process, how long the Search and Template Labs' sampler may think per refill,
and how the machine is kept awake.

The profile lives in a one-word file in the data folder, not only in ``harness.db``, because
thread counts must be fixed before numpy is first imported, long before the database opens.
``HARNESS_PROFILE`` in the environment is the default when no choice was saved there (a
systemd unit sets it); a choice made on the Settings screen wins over it.
"""

import contextlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from .config import DATA_DIR

type Profile = Literal["lean", "balanced", "max"]

PROFILES: tuple[Profile, ...] = get_args(Profile.__value__)
DEFAULT: Profile = "balanced"
ENV = "HARNESS_PROFILE"
FILE_NAME = "profile"

#: Variables numpy's BLAS and OpenMP read once, at import, for their thread pools.
THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


@dataclass(frozen=True, slots=True)
class Budget:
    """What one profile allows. ``None`` means as many as the machine has."""

    #: numpy / BLAS / OpenMP threads.
    math_threads: int | None
    #: DuckDB's worker threads.
    duckdb_threads: int | None
    #: Share of the machine's memory DuckDB may keep, of the 80% it would take by itself.
    duckdb_memory_share: float
    #: Worker threads behind ``asyncio.to_thread``.
    executor_workers: int | None
    #: Added to the process's niceness at start (POSIX). Only ever raised: lowering it again
    #: needs privileges, so a change of profile reaches it on the next start.
    nice: int
    #: CPU seconds the sampler may spend on one refill before the rest of the round is drawn
    #: uniformly from points not tried yet. ``None`` is no limit, as shipped.
    sampler_seconds: float | None
    #: Trials the sampler learns from: the best and the most recent. ``None`` is every trial.
    sampler_best: int | None
    sampler_recent: int | None
    #: A study held in memory past this many trials is rebuilt from the window above.
    sampler_rebuild_at: int | None
    #: What the keep-awake lock blocks: ``sleep`` leaves screen blanking alone.
    awake_what: str
    #: Keep the machine awake on battery too.
    awake_on_battery: bool


BUDGETS: dict[Profile, Budget] = {
    "lean": Budget(
        math_threads=1,
        duckdb_threads=1,
        duckdb_memory_share=0.15,
        executor_workers=2,
        nice=10,
        sampler_seconds=1.0,
        sampler_best=500,
        sampler_recent=1500,
        sampler_rebuild_at=4000,
        awake_what="sleep",
        awake_on_battery=False,
    ),
    "balanced": Budget(
        math_threads=2,
        duckdb_threads=2,
        duckdb_memory_share=0.25,
        executor_workers=4,
        nice=5,
        sampler_seconds=2.0,
        sampler_best=500,
        sampler_recent=1500,
        sampler_rebuild_at=4000,
        awake_what="sleep",
        awake_on_battery=False,
    ),
    "max": Budget(
        math_threads=None,
        duckdb_threads=None,
        duckdb_memory_share=0.25,
        executor_workers=None,
        nice=0,
        sampler_seconds=5.0,
        sampler_best=1000,
        sampler_recent=4000,
        sampler_rebuild_at=10000,
        awake_what="sleep:idle",
        awake_on_battery=True,
    ),
}


def _file(data_dir: Path = DATA_DIR) -> Path:
    return data_dir / FILE_NAME


def _parse(text: str | None) -> Profile | None:
    word = (text or "").strip().lower()
    for profile in PROFILES:
        if word == profile:
            return profile
    return None


def current(data_dir: Path = DATA_DIR) -> Profile:
    """The profile in force: the saved choice, else ``HARNESS_PROFILE``, else Balanced."""
    saved: str | None = None
    with contextlib.suppress(OSError):
        saved = _file(data_dir).read_text(encoding="utf-8")
    return _parse(saved) or _parse(os.environ.get(ENV)) or DEFAULT


def budget(data_dir: Path = DATA_DIR) -> Budget:
    return BUDGETS[current(data_dir)]


def save(profile: Profile, data_dir: Path = DATA_DIR) -> None:
    """Remember a choice made on the Settings screen. Written beside and renamed over."""
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = _file(data_dir)
    partial = target.with_suffix(".partial")
    partial.write_text(profile, encoding="utf-8")
    partial.replace(target)


def limit_math_threads() -> None:
    """Cap numpy's thread pools. Call before numpy is imported; a value already set wins."""
    threads = budget().math_threads
    if threads is None:
        return
    for name in THREAD_VARIABLES:
        os.environ.setdefault(name, str(threads))


def lower_priority() -> None:
    """Raise this process's niceness by the profile's amount, so local arithmetic yields to
    whatever the person is doing. Never raises."""
    amount = budget().nice
    if amount <= 0 or sys.platform == "win32":
        return
    with contextlib.suppress(OSError, AttributeError):
        os.nice(amount)


def on_battery() -> bool:
    """Whether the machine runs on battery now. ``False`` when it cannot tell."""
    with contextlib.suppress(Exception):
        if sys.platform.startswith("linux"):
            supplies = Path("/sys/class/power_supply")
            mains = [
                p
                for p in supplies.iterdir()
                if (p / "type").read_text(encoding="utf-8").strip() == "Mains"
            ]
            if mains:
                return not any(
                    (p / "online").read_text(encoding="utf-8").strip() == "1" for p in mains
                )
            return False
        if sys.platform == "win32":
            import ctypes

            class _PowerStatus(ctypes.Structure):
                _fields_ = [
                    ("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong),
                    ("BatteryFullLifeTime", ctypes.c_ulong),
                ]

            status = _PowerStatus()
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
                return status.ACLineStatus == 0
            return False
        if sys.platform == "darwin":
            import subprocess

            out = subprocess.run(
                ["/usr/bin/pmset", "-g", "ps"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            return "Battery Power" in out
    return False
