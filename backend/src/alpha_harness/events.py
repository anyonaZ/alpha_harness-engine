"""Wake-ups between the background loops.

The tracker, the batch engine and the optimizer used to look for work on fixed beats of 1, 2
and 5 seconds, open or idle, for as long as the app ran. Each now sleeps until something it
cares about happens, with a long safety tick in case a signal is ever missed:

- ``SUBMITTED``: a simulation has a platform id to read. Wakes the tracker.
- ``QUEUED``: work entered the local queue. Wakes the engine.
- ``SETTLED``: a simulation reached a terminal state, or a batch handed over its children.
  Wakes the engine (a slot may be free) and the optimizer (a trial may be scored).
- ``TASK_CHANGED``: a lab task was added, started, paused, resized or advanced. Wakes the
  optimizer.
- ``STORED``: an Alpha reached the local vault, which a waiting trial may be scored from.
  Wakes the optimizer.

A wake-up coalesces: any number of signals before a loop next looks are one wake-up. A
signal is never an instruction, only a reason to look; every loop still reads its state
from disk, so a missed or extra signal changes when work happens, never what happens.
"""

import asyncio
import contextlib
from collections import defaultdict
from typing import Literal

type Topic = Literal["submitted", "queued", "settled", "task_changed", "stored"]

SUBMITTED: Topic = "submitted"
QUEUED: Topic = "queued"
SETTLED: Topic = "settled"
TASK_CHANGED: Topic = "task_changed"
STORED: Topic = "stored"


class Wake:
    """A coalescing wake-up for one loop."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    @property
    def pending(self) -> bool:
        return self._event.is_set()

    async def wait(self, seconds: float) -> bool:
        """Sleep until signalled or ``seconds`` pass. True when signalled."""
        signalled = True
        if not self._event.is_set():
            signalled = False
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event.wait(), seconds)
                signalled = True
        self._event.clear()
        return signalled


class Bus:
    """Topics to the loops that wake on them."""

    def __init__(self) -> None:
        self._wakes: defaultdict[Topic, list[Wake]] = defaultdict(list)

    def subscribe(self, *topics: Topic) -> Wake:
        wake = Wake()
        for topic in topics:
            self._wakes[topic].append(wake)
        return wake

    def publish(self, topic: Topic) -> None:
        for wake in self._wakes.get(topic, ()):
            wake.set()


#: The process's one bus: there is one app per process, and every loop belongs to it.
bus = Bus()
