"""Write a Search Lab task, with history, into a fresh ``harness.db``, for ``busy_bench.py``.

Run with the Python of the backend under test, so the rows are that build's own:

    backend/.venv/bin/python bench/busy_seed.py --db ~/.alpha-harness/harness.db --trials 10000
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import asyncio
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

from optuna.distributions import CategoricalDistribution, distribution_to_json

from alpha_harness.db.models import Study, StudyStatus, Trial, TrialState, utcnow
from alpha_harness.db.sqlite import Database
from alpha_harness.labs import scheduler, search
from alpha_harness.labs.params import SEARCH_SAMPLER, SearchParams


def make_space(n_fields: int) -> dict[str, Any]:
    return {
        "fields": {f"fnd_{i}": "VECTOR" if i % 25 == 0 else "MATRIX" for i in range(n_fields)},
        "universes": ["TOP3000"],
        "absent": {},
        "vector": ["vec_avg", "vec_sum"],
        "families": ["cs", "ts", "cs_ts", "ts_ts", "group", "group_ts"],
        "cs": ["rank", "zscore", "scale", "normalize"],
        "ts": ["ts_mean", "ts_rank", "ts_zscore", "ts_delta", "ts_std_dev", "ts_sum"],
        "lookbacks": [5, 10, 20, 40, 60, 120, 250],
        "group": ["group_rank", "group_zscore", "group_neutralize"],
        "groups": ["industry", "subindustry", "sector", "market"],
        "neutralizations": ["NONE", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY"],
    }


class Recorder:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.params: dict[str, Any] = {}
        self.choices: dict[str, list[Any]] = {}

    def suggest_categorical(self, name: str, choices: Any) -> Any:
        value = self.rng.choice(list(choices))
        self.params[name] = value
        self.choices[name] = list(choices)
        return value


async def seed(db_path: Path, trials: int, fields: int) -> None:
    db = Database(db_path)
    await db.create_all()
    space = make_space(fields)
    run = SearchParams(region="USA", delay=1, cores=8, space=space, decay=4)
    async with db.session() as session:
        study = Study(
            name="Search Lab · bench",
            template_name="Search Lab",
            template_source="",
            sampler=SEARCH_SAMPLER,
            sampler_params=run.dump(),
            objectives=["train_sharpe"],
            directions=["maximize"],
            batch_size=80,
            max_trials=trials + 1_000_000,
            task="search-bench",
            status=StudyStatus.IDLE,
        )
        session.add(study)
        await session.flush()
        rng = random.Random(0)
        choices, slots = search.field_choices(space), search.slots(space)
        finished = utcnow() - timedelta(days=1)
        rows = []
        for number in range(trials):
            recorder = Recorder(rng)
            params = search.suggest(recorder, space, choices)
            request = search.request_for(params, run)
            roll = rng.random()
            state = (
                TrialState.COMPLETE
                if roll < 0.7
                else TrialState.FAIL
                if roll < 0.8
                else TrialState.PRUNED
            )
            rows.append(
                {
                    "study_id": study.id,
                    "number": number,
                    "params": params,
                    "distributions": {
                        name: scheduler.FROM_SPACE
                        if name in slots
                        else distribution_to_json(CategoricalDistribution(recorder.choices[name]))
                        for name in recorder.params
                    },
                    "expression": request.regular,
                    "settings": request.settings.model_dump(by_alias=True, exclude_none=True),
                    "state": state,
                    "values": [rng.gauss(0.4, 0.7)] if state == TrialState.COMPLETE else None,
                    "finished_at": finished if state != TrialState.PRUNED else None,
                }
            )
        for start in range(0, len(rows), 2000):
            await session.execute(Trial.__table__.insert(), rows[start : start + 2000])
    await db.dispose()
    print(f"seeded task {study.id} with {trials} trials over {fields} fields")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=10000)
    parser.add_argument("--fields", type=int, default=1500)
    args = parser.parse_args()
    args.db.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(seed(args.db, args.trials, args.fields))


if __name__ == "__main__":
    main()
