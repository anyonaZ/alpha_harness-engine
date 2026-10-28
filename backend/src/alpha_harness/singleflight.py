"""One run of an expensive read at a time, shared by everyone who asks for it meanwhile.

The Portfolio page recomputes every submitted Alpha's correlations, and the screens ask for
it again on pushes, on focus and from more than one tab. Before, each request started a run
of its own, and runs piled up on the CPU while the first was still going. Here a request whose
inputs match a run in progress waits for that run's answer instead.

A run is cancelled once every request waiting on it has gone, at its next ``await``: the
arithmetic inside one compute step finishes, the steps after it never start.
"""

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable


class SingleFlight:
    def __init__(self) -> None:
        #: Inputs -> the run in progress and how many requests wait on it.
        self._runs: dict[Hashable, tuple[asyncio.Future[Any], list[int]]] = {}

    async def run[T](self, key: Hashable, make: Callable[[], Awaitable[T]]) -> T:
        """``make()``'s answer, from the run in progress for ``key`` if there is one."""
        entry = self._runs.get(key)
        # A run cancelled a moment ago may not have been forgotten yet; it serves nobody.
        if entry is None or entry[0].cancelled():
            future: asyncio.Future[Any] = asyncio.ensure_future(make())
            entry = (future, [0])
            self._runs[key] = entry

            def forget(done: asyncio.Future[Any], key: Hashable = key) -> None:
                current = self._runs.get(key)
                if current is not None and current[0] is done:
                    del self._runs[key]
                if not done.cancelled():
                    # Read so a failure nobody waited for is not reported as unretrieved.
                    done.exception()

            future.add_done_callback(forget)
        future, waiting = entry
        waiting[0] += 1
        try:
            return await asyncio.shield(future)
        finally:
            waiting[0] -= 1
            if waiting[0] == 0 and not future.done():
                # Forgotten now, not when the cancellation lands: a request in between would
                # otherwise wait on a run that is about to be cancelled.
                if self._runs.get(key) is entry:
                    del self._runs[key]
                future.cancel()
