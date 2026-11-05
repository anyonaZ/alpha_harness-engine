"""WebSocket fan-out for live telemetry.

One multiplexed channel carries every live update — simulation status, elapsed time,
catalog sync progress — so the UI opens a single socket rather than polling several
endpoints. Commands still go over REST; this is strictly server to client.
"""

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import WebSocket

log = structlog.get_logger(__name__)

#: Longest a single client may take to accept one message before it is dropped.
SEND_TIMEOUT = 1.0


class Hub:
    """Tracks connected clients and broadcasts messages to them.

    No lock: the set is only ever mutated by a single statement with no ``await`` in it,
    which the event loop cannot interleave. Sends do yield, so they run against a copy.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        #: Last message per topic, replayed to a client on connect so a freshly opened
        #: tab shows current state immediately instead of waiting for the next change.
        self._latest: dict[str, dict[str, Any]] = {}
        #: Topics whose current state is read fresh for each client that connects, so their
        #: owners need not keep a snapshot up to date while no client is connected.
        self._providers: dict[str, Callable[[], Awaitable[Any]]] = {}

    def provide(self, topic: str, current: Callable[[], Awaitable[Any]]) -> None:
        """Read ``topic``'s state with ``current`` for each client as it connects."""
        self._providers[topic] = current

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)
        messages = [m for topic, m in self._latest.items() if topic not in self._providers]
        for topic, current in list(self._providers.items()):
            try:
                messages.append({"topic": topic, "payload": await current()})
            except Exception:
                log.exception("hub.snapshot_failed", topic=topic)
        for message in messages:
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(message, default=str))
        log.debug("hub.connected", clients=len(self._clients))

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)
        log.debug("hub.disconnected", clients=len(self._clients))

    async def broadcast(self, topic: str, payload: Any, *, replay: bool = True) -> None:
        """Send to every client at once, dropping any that fail or stall.

        A browser tab that stops reading must not hold up the engine and tracker loops, so
        each send gets :data:`SEND_TIMEOUT` seconds and they run side by side. ``replay=False``
        keeps a side-channel message from replacing the snapshot new clients get on connect.
        """
        message = {"topic": topic, "payload": payload}
        if replay:
            self._latest[topic] = message
        text = json.dumps(message, default=str)

        clients = list(self._clients)
        if not clients:
            return

        async def send(client: WebSocket) -> None:
            try:
                await asyncio.wait_for(client.send_text(text), timeout=SEND_TIMEOUT)
            # A socket that fails to send for any reason is dropped, and closed: left open it
            # kept its pings, so the browser never reconnected and never saw another update.
            except Exception:  # noqa: BLE001
                self._clients.discard(client)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.close(), timeout=SEND_TIMEOUT)

        await asyncio.gather(*(send(client) for client in clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)


#: Topic names shared with the frontend. Keep in sync with ``Topic`` in frontend/src/lib/ws.ts.
TOPIC_SIMULATIONS = "simulations"
TOPIC_SYNC = "sync"
TOPIC_SESSION = "session"
TOPIC_STUDIES = "studies"
TOPIC_TASKS = "tasks"
#: News worth a system notification (``notices``). Never replayed: old news is not news.
TOPIC_NOTICES = "notices"
