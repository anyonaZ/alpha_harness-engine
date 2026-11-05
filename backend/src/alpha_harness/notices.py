"""Things worth a system notification, worked out here and pushed to the UI.

The desktop window used to work these out itself: it kept the task list and the engine's
status fresh and compared each reading with the last, so it went on fetching both — every
30 seconds — while it sat hidden in the tray, which is when a notification matters and
nothing else is shown. The backend already knows the moment a task ends or the engine
pauses, so it compares here, on each change it pushes anyway, and sends only the news on
the ``notices`` topic. A hidden window now listens and fetches nothing.

Each notice is raised when its state changes, never for what was already so when the app
started, as before.
"""

from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from .db.models import Study, StudyStatus
from .labs.params import TASK_SAMPLERS, TEMPLATE_SAMPLER

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .db.sqlite import Database
    from .engine.slots import BatchEngine

log = structlog.get_logger(__name__)

#: A task in one of these has ended, for the reader: finished, paused or failed.
ENDED = (StudyStatus.COMPLETE, StudyStatus.PAUSED, StudyStatus.FAILED)
#: ...and it ended now if it was in one of these at the last look.
LIVE = (StudyStatus.QUEUED, StudyStatus.RUNNING)


class Notices:
    def __init__(
        self,
        db: Database,
        engine: BatchEngine,
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._db = db
        self._engine = engine
        self._send = send
        #: Task id -> status at the last look; ``None`` until the first look, which sets the
        #: baseline and announces nothing.
        self._statuses: dict[int, StudyStatus] | None = None
        self._flags: tuple[bool, bool] | None = None

    async def tasks_changed(self) -> None:
        """Announce the tasks that ended since the last look. Reads one small table."""
        try:
            async with self._db.session() as session:
                rows = (
                    await session.execute(
                        select(
                            Study.id,
                            Study.status,
                            Study.label,
                            Study.template_name,
                            Study.sampler,
                            Study.message,
                        ).where(Study.sampler.in_(TASK_SAMPLERS))
                    )
                ).all()
        except Exception:
            log.exception("notices.tasks_failed")
            return
        before = self._statuses
        self._statuses = {row.id: StudyStatus(row.status) for row in rows}
        if before is None:
            return
        ended = [
            {
                "id": row.id,
                "status": str(row.status),
                "name": row.label
                or (row.template_name if row.sampler == TEMPLATE_SAMPLER else None)
                or TASK_SAMPLERS.get(row.sampler, row.sampler),
                "message": row.message or TASK_SAMPLERS.get(row.sampler, row.sampler),
            }
            for row in rows
            if row.status in ENDED and (row.id not in before or before[row.id] in LIVE)
        ]
        if ended:
            await self._send({"kind": "tasks_ended", "tasks": ended})

    async def engine_changed(self) -> None:
        """Announce BRAIN's daily limit being reached, or the session being refused."""
        now = (self._engine.daily_limit_hit, self._engine.session_lost)
        before = self._flags
        self._flags = now
        if before is None:
            return
        if now[0] and not before[0]:
            await self._send({"kind": "daily_limit"})
        if now[1] and not before[1]:
            await self._send({"kind": "session_lost"})
