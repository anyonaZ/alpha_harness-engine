"""CPU of ``GET /api/lab-tasks``'s counting, which the UI asks for on every task push.

Builds a real ``harness.db`` in a temporary folder with the app's own schema, fills it with
synthetic trials, then times ``api.lab_tasks._progress``: a first call, which reads every
trial, and a repeat with no trial changed, which is what most pushes are. Run from
``backend/``:

    uv run python ../bench/tasks_bench.py --tasks 4 --trials 100000
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import asyncio
import json
import random
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import insert, select

from alpha_harness.api import lab_tasks
from alpha_harness.db.models import Study, StudyStatus, Trial
from alpha_harness.db.sqlite import Database
from alpha_harness.labs.params import SEARCH_SAMPLER


async def build(db: Database, tasks: int, per_task: int) -> None:
    await db.create_all()
    rng = random.Random(0)
    async with db.session() as session:
        for t in range(tasks):
            session.add(
                Study(
                    name=f"bench-{t}",
                    template_source="",
                    sampler=SEARCH_SAMPLER,
                    sampler_params={"region": "USA", "delay": 1, "space": {}},
                    status=StudyStatus.RUNNING,
                    max_trials=per_task * 2,
                )
            )
    async with db.session() as session:
        ids = list((await session.scalars(select(Study.id))).all())
        for study_id in ids:
            rows = []
            for i in range(per_task):
                state = rng.choices(["COMPLETE", "FAIL", "PRUNED", "QUEUED"], [70, 10, 15, 5])[0]
                rows.append(
                    {
                        "study_id": study_id,
                        "number": i,
                        "params": {"field": f"f{rng.randrange(1500)}", "family": "ts"},
                        "distributions": {},
                        "expression": f"rank(ts_mean(f{i}, 20))",
                        "settings": {"region": "USA", "universe": "TOP3000", "delay": 1},
                        "state": state,
                        "values": [rng.gauss(0.5, 0.6)] if state == "COMPLETE" else None,
                        "message": "Matched an Alpha already simulated; no quota spent."
                        if state == "COMPLETE" and rng.random() < 0.05
                        else (json.dumps("BRAIN refused") if state == "FAIL" else None),
                    }
                )
            for start in range(0, len(rows), 5000):
                await session.execute(insert(Trial), rows[start : start + 5000])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--trials", type=int, default=100000, help="per task")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        db = Database(Path(folder) / "harness.db")
        await build(db, args.tasks, args.trials)
        state = SimpleNamespace(db=db)
        rows = await lab_tasks._rows(state)
        for label in ("first call", "repeat, nothing changed"):
            cpu, wall = time.process_time(), time.perf_counter()
            await lab_tasks._progress(state, rows)
            print(
                f"{args.tasks} tasks x {args.trials} trials  {label:24s} "
                f"{(time.perf_counter() - wall) * 1000:8.1f} ms wall "
                f"{(time.process_time() - cpu) * 1000:8.1f} ms CPU"
            )
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
