"""A stand-in for BRAIN's API, enough for a Search Lab task to run end to end.

It answers what a running lab task asks: ``POST /simulations`` (a batch of up to ten), the
status reads of the batch and its children, the Alphas they make (one by one and as the newest
page of the user's list), and each Alpha's daily PnL. A simulation takes ``--sim-seconds``;
its Alpha's figures are drawn from a hash of the expression, so the same expression always
scores the same. Anything else answers 404 and is counted, so a run shows what it skipped.

A simulation finishes, and its Alpha exists, once its time is up whether or not anyone reads
it, as on BRAIN: that is what lets the app adopt a send whose answer it lost. Every body sent
is counted, and ``/__stats`` lists any sent twice. ``--post-delay`` holds each send open
that long after BRAIN has it, the window in which a crash loses the answer.

For benchmarks only: it never sees a password, and the app is pointed at it by
``bench/busy_bench.py`` or ``bench/recovery_check.py``, never by a setting.

    python bench/fake_brain.py --port 8899 --sim-seconds 30
"""

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import re
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

#: Trading days of PnL per Alpha, about ten years as BRAIN gives.
PNL_DAYS = 2500
#: How often a running simulation tells the client to look again.
RETRY_AFTER = 2.5

_ids = itertools.count(1)


def _new_id(prefix: str) -> str:
    n, out = next(_ids), ""
    while n:
        n, r = divmod(n, 36)
        out = "0123456789abcdefghijklmnopqrstuvwxyz"[r] + out
    return f"{prefix}{out:0>6}"


def _unit(text: str, salt: str) -> float:
    """A number in [0, 1) fixed by ``text``."""
    digest = hashlib.sha256(f"{salt}:{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _normal(text: str, salt: str) -> float:
    u1, u2 = max(_unit(text, salt + "a"), 1e-12), _unit(text, salt + "b")
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


class Brain:
    def __init__(self, sim_seconds: float, post_delay: float = 0.0) -> None:
        self.sim_seconds = sim_seconds
        self.post_delay = post_delay
        #: Each simulation body as sent (canonical JSON) -> times sent.
        self.sent: Counter[str] = Counter()
        #: Sends BRAIN has but whose answer has not gone back yet.
        self.posting = 0
        #: Simulation id -> (started, wire body, child ids or alpha id).
        self.sims: dict[str, dict[str, Any]] = {}
        self.alphas: dict[str, dict[str, Any]] = {}
        #: Newest last.
        self.order: list[str] = []
        self.unknown: Counter[str] = Counter()
        self.finished = 0

    # -- simulations -------------------------------------------------------------------

    def create(self, body: Any) -> str:
        now, wall = time.monotonic(), time.time()
        for item in body if isinstance(body, list) else [body]:
            self.sent[json.dumps(item, sort_keys=True)] += 1
        if isinstance(body, list):
            parent = _new_id("P")
            children = []
            for item in body:
                child = _new_id("C")
                self.sims[child] = {"started": now, "wall": wall, "body": item, "alpha": None}
                children.append(child)
            self.sims[parent] = {"started": now, "children": children}
            return parent
        sim = _new_id("S")
        self.sims[sim] = {"started": now, "wall": wall, "body": body, "alpha": None}
        return sim

    def settle(self) -> None:
        """Finish every simulation whose time is up, read or not, as BRAIN does."""
        now = time.monotonic()
        for sim in self.sims.values():
            if "children" in sim or sim["alpha"] is not None:
                continue
            if now - sim["started"] >= self.sim_seconds:
                self._finish(sim)

    def _finish(self, sim: dict[str, Any]) -> None:
        sim["alpha"] = self._make_alpha(
            sim["body"], datetime.fromtimestamp(sim["wall"] + self.sim_seconds, UTC)
        )
        self.finished += 1

    def read(self, sim_id: str) -> tuple[int, dict[str, Any], dict[str, str]]:
        sim = self.sims.get(sim_id)
        if sim is None:
            return 404, {"detail": "Not found."}, {}
        elapsed = time.monotonic() - sim["started"]
        if elapsed < self.sim_seconds:
            return (
                200,
                {"progress": round(elapsed / self.sim_seconds, 2)},
                {"Retry-After": str(RETRY_AFTER)},
            )
        if "children" in sim:
            return 200, {"children": sim["children"], "status": "COMPLETE"}, {}
        if sim["alpha"] is None:
            self._finish(sim)
        body = sim["body"]
        return (
            200,
            {
                "id": sim_id,
                "type": body.get("type", "REGULAR"),
                "status": "COMPLETE",
                "alpha": sim["alpha"],
                "regular": body.get("regular"),
                "settings": body.get("settings"),
            },
            {},
        )

    # -- alphas ------------------------------------------------------------------------

    def _make_alpha(self, body: dict[str, Any], created: datetime) -> str:
        alpha_id = _new_id("A")
        code = str(body.get("regular") or "")
        settings = dict(body.get("settings") or {})
        sharpe = round(0.4 + 0.7 * _normal(code, "sharpe"), 2)
        turnover = round(0.05 + 0.4 * _unit(code, "turnover"), 4)
        returns = round(0.02 + 0.12 * _unit(code, "returns") * max(sharpe, 0.1), 4)
        fitness = round(sharpe * math.sqrt(abs(returns) / max(turnover, 0.125)), 2)

        def stats(scale: float) -> dict[str, Any]:
            return {
                "pnl": round(1e6 * returns * scale, 1),
                "bookSize": 20_000_000,
                "longCount": 1500,
                "shortCount": 1500,
                "turnover": turnover,
                "returns": returns,
                "drawdown": round(0.05 + 0.1 * _unit(code, "dd"), 4),
                "margin": round(returns / max(turnover, 1e-6) / 252, 6),
                "fitness": fitness,
                "sharpe": round(sharpe * scale, 2),
                "startDate": "2015-01-01",
            }

        passing = sharpe >= 1.25 and fitness >= 1.0
        checks = [
            {
                "name": "LOW_SHARPE",
                "result": "PASS" if sharpe >= 1.25 else "FAIL",
                "limit": 1.25,
                "value": sharpe,
            },
            {
                "name": "LOW_FITNESS",
                "result": "PASS" if fitness >= 1.0 else "FAIL",
                "limit": 1.0,
                "value": fitness,
            },
            {"name": "SELF_CORRELATION", "result": "PENDING" if passing else "FAIL"},
        ]
        now = created.isoformat()
        alpha = {
            "id": alpha_id,
            "type": "REGULAR",
            "author": "BENCH",
            "settings": settings,
            "regular": {"code": code, "description": None, "operatorCount": code.count("(")},
            "dateCreated": now,
            "dateSubmitted": None,
            "dateModified": now,
            "name": None,
            "favorite": False,
            "hidden": False,
            "color": None,
            "category": None,
            "tags": [],
            "grade": "AVERAGE",
            "stage": "IS",
            "status": "UNSUBMITTED",
            "is": {**stats(1.0), "checks": checks},
            "train": stats(1.05),
            "test": stats(0.85),
            "os": None,
            "prod": None,
        }
        self.alphas[alpha_id] = alpha
        self.order.append(alpha_id)
        return alpha_id

    def recordset(self, alpha_id: str, name: str) -> dict[str, Any] | None:
        """Daily ``pnl`` (cumulative) or ``turnover``, about ten years of trading days."""
        alpha = self.alphas.get(alpha_id)
        if alpha is None or name not in ("pnl", "turnover"):
            return None
        code = alpha["regular"]["code"]
        drift = alpha["is"]["returns"] * 20_000_000 / 252
        traded = alpha["is"]["turnover"]
        day, total, records = date(2015, 1, 2), 0.0, []
        for i in range(PNL_DAYS):
            if name == "pnl":
                total += drift + 0.02 * 20_000_000 / 16 * _normal(code, f"pnl{i}")
                records.append([day.isoformat(), round(total, 2)])
            else:
                records.append(
                    [day.isoformat(), round(traded * (0.8 + 0.4 * _unit(code, f"t{i}")), 4)]
                )
            day += timedelta(days=1 if day.weekday() < 4 else 3)
        kind = "amount" if name == "pnl" else "percent"
        return {
            "schema": {
                "name": name,
                "title": name.title(),
                "properties": [
                    {"name": "date", "title": "Date", "type": "date"},
                    {"name": name, "title": name.title(), "type": kind},
                ],
            },
            "records": records,
        }


def build(brain: Brain) -> Starlette:
    async def create(request: Request) -> Response:
        sim_id = brain.create(await request.json())
        if brain.post_delay:
            brain.posting += 1
            try:
                await asyncio.sleep(brain.post_delay)
            finally:
                brain.posting -= 1
        base = str(request.base_url).rstrip("/")
        return Response(status_code=201, headers={"Location": f"{base}/simulations/{sim_id}"})

    async def read(request: Request) -> Response:
        status, body, headers = brain.read(request.path_params["sim_id"])
        return JSONResponse(body, status_code=status, headers=headers)

    async def alpha(request: Request) -> Response:
        brain.settle()
        found = brain.alphas.get(request.path_params["alpha_id"])
        if found is None:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return JSONResponse(found)

    async def recordset(request: Request) -> Response:
        found = brain.recordset(request.path_params["alpha_id"], request.path_params["name"])
        if found is None:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        return JSONResponse(found)

    async def listing(request: Request) -> Response:
        brain.settle()
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
        # ``dateCreated>=…`` and ``dateCreated<…`` are not key=value pairs: read them raw.
        after = before = None
        for token in request.url.query.split("&"):
            bound = re.fullmatch(r"dateCreated(>=|<)(.+)", unquote(token))
            if bound:
                moment = datetime.fromisoformat(bound.group(2))
                after, before = (moment, before) if bound.group(1) == ">=" else (after, moment)
        rows = [brain.alphas[a] for a in brain.order]
        rows.sort(key=lambda a: a["dateCreated"])
        rows = [
            a
            for a in rows
            if (after is None or datetime.fromisoformat(a["dateCreated"]) >= after)
            and (before is None or datetime.fromisoformat(a["dateCreated"]) < before)
        ]
        if not request.query_params.get("order", "-dateCreated").lstrip().startswith("dateCreated"):
            rows.reverse()
        page = rows[offset : offset + limit]
        return JSONResponse({"count": len(rows), "results": page})

    async def schema(_: Request) -> Response:
        # A settings tree with nothing to choose from: every value the app sends passes.
        decay = {"type": "integer", "label": "Decay", "required": False}
        return JSONResponse({"actions": {"POST": {"settings": {"children": {"decay": decay}}}}})

    async def stats(_: Request) -> Response:
        brain.settle()
        return JSONResponse(
            {
                "simulations": brain.finished,
                "sent": sum(brain.sent.values()),
                "sent_twice": {body: n for body, n in brain.sent.items() if n > 1},
                "posting": brain.posting,
                "alphas": len(brain.alphas),
                "unknown": dict(brain.unknown.most_common(20)),
            }
        )

    async def anything(request: Request) -> Response:
        brain.unknown[f"{request.method} {request.url.path}"] += 1
        return JSONResponse({"detail": "Not found."}, status_code=404)

    return Starlette(
        routes=[
            Route("/simulations", create, methods=["POST"]),
            Route("/simulations", schema, methods=["OPTIONS"]),
            Route("/simulations/{sim_id}", read, methods=["GET"]),
            Route("/alphas/{alpha_id}", alpha, methods=["GET"]),
            Route("/alphas/{alpha_id}/recordsets/{name}", recordset, methods=["GET"]),
            Route("/users/self/alphas", listing, methods=["GET"]),
            Route("/__stats", stats, methods=["GET"]),
            Route(
                "/{path:path}",
                anything,
                methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS", "PUT"],
            ),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--sim-seconds", type=float, default=30.0)
    parser.add_argument("--post-delay", type=float, default=0.0)
    args = parser.parse_args()
    uvicorn.run(
        build(Brain(args.sim_seconds, args.post_delay)),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
