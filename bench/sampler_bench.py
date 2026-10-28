"""CPU of one Search Lab refill: the shipped sampler against each Resource profile.

Drives the app's own code — ``labs.search`` draws the points, ``labs.study`` builds the
sampler and replays history, ``labs.scheduler.ask_points`` asks — over a synthetic task of
the size given. Run from ``backend/``:

    uv run python ../bench/sampler_bench.py --trials 2000 10000 30000 --fields 1500
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import random
import time
from typing import Any

from optuna.distributions import CategoricalDistribution, distribution_to_json

from alpha_harness import resources
from alpha_harness.labs import scheduler, search, study
from alpha_harness.labs.params import SearchParams

FAMILIES = ["cs", "ts", "cs_ts", "ts_ts", "group", "group_ts"]


def make_space(n_fields: int, narrow: bool = False) -> dict[str, Any]:
    if narrow:
        # A late task: a space this small is mostly tried, so most draws are repeats.
        return {
            "fields": {f"fnd_{i}": "MATRIX" for i in range(n_fields)},
            "universes": ["TOP3000"],
            "absent": {},
            "vector": [],
            "families": ["cs", "ts"],
            "cs": ["rank", "zscore"],
            "ts": ["ts_mean", "ts_rank"],
            "lookbacks": [20, 60, 250],
            "group": [],
            "groups": [],
            "neutralizations": ["MARKET", "SUBINDUSTRY"],
        }
    return {
        "fields": {f"fnd_{i}": "VECTOR" if i % 25 == 0 else "MATRIX" for i in range(n_fields)},
        "universes": ["TOP3000"],
        "absent": {},
        "vector": ["vec_avg", "vec_sum"],
        "families": FAMILIES,
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


def history(space: dict[str, Any], n: int, seed: int = 0) -> list[dict[str, Any]]:
    """Trial rows as ``Optimizer.optuna_study`` reads them, oldest first."""
    rng = random.Random(seed)
    choices = search.field_choices(space)
    slots = search.slots(space)
    rows = []
    for _ in range(n):
        rec = Recorder(rng)
        params = search.suggest(rec, space, choices)
        roll = rng.random()
        state = "COMPLETE" if roll < 0.7 else ("FAIL" if roll < 0.8 else "PRUNED")
        rows.append(
            {
                "params": params,
                "distributions": {
                    name: scheduler.FROM_SPACE
                    if name in slots
                    else distribution_to_json(CategoricalDistribution(rec.choices[name]))
                    for name in rec.params
                },
                "values": [rng.gauss(0.5, 0.6)] if state == "COMPLETE" else None,
                "state": state,
            }
        )
    return rows


def window(
    rows: list[dict[str, Any]], best: int | None, recent: int | None
) -> list[dict[str, Any]]:
    """``study.history_window``'s rule, applied to rows in memory."""
    if best is None and recent is None:
        return rows
    replayed = [i for i, r in enumerate(rows) if r["state"] in ("COMPLETE", "FAIL", "PRUNED")]
    keep = set(replayed[-recent:]) if recent else set()
    if best:
        scored = [i for i in replayed if rows[i]["state"] == "COMPLETE"]
        scored.sort(key=lambda i: rows[i]["values"][0], reverse=True)
        keep.update(scored[:best])
    return [rows[i] for i in sorted(keep)]


def refill(
    space: dict[str, Any], rows: list[dict[str, Any]], profile: str | None
) -> dict[str, float]:
    budget = resources.BUDGETS[profile] if profile else None  # type: ignore[index]
    kept = window(rows, budget.sampler_best, budget.sampler_recent) if budget else rows
    built = study._rebuild(
        study.make_sampler(20, 80), kept, study._Distributions(search.slots(space))
    )
    seen: dict[tuple[str, str], float | bool] = {}
    for r in rows:
        if r["state"] in ("COMPLETE", "FAIL"):
            request = search.request_for(r["params"], RUN(space))
            key = search.identity_of(
                request.regular, request.settings.model_dump(by_alias=True, exclude_none=True)
            )
            seen[key] = float(r["values"][0]) if r["state"] == "COMPLETE" else False
    before = len(built.get_trials(deepcopy=False))
    cpu = time.thread_time()
    picked, _ = scheduler.ask_points(
        built,
        search,
        RUN(space),
        10,
        seen,
        [],
        None,
        budget.sampler_seconds if budget else None,
    )
    spent = time.thread_time() - cpu
    asks = len(built.get_trials(deepcopy=False)) - before
    return {"cpu": spent, "asks": asks, "picked": len(picked), "history": len(kept)}


def RUN(space: dict[str, Any]) -> SearchParams:  # noqa: N802 - reads as a constant
    return SearchParams(region="USA", delay=1, cores=8, space=space, decay=4)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, nargs="+", default=[2000, 10000])
    parser.add_argument("--fields", type=int, default=1500)
    parser.add_argument("--narrow", action="store_true", help="a mostly-tried space")
    args = parser.parse_args()
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    space = make_space(args.fields, args.narrow)
    print(f"Search Lab refill of 10 points, {args.fields} fields, CPU of the asking thread")
    print(
        f"{'trials':>8} {'sampler':>10} {'history':>8} {'asks':>5} {'new':>4} "
        f"{'per ask':>9} {'refill':>8}"
    )
    for n in args.trials:
        rows = history(space, n)
        for profile in (None, "max", "balanced", "lean"):
            r = refill(space, rows, profile)
            per = r["cpu"] / max(r["asks"], 1) * 1000
            label = profile or "shipped"
            print(
                f"{n:>8} {label:>10} {int(r['history']):>8} {int(r['asks']):>5} "
                f"{int(r['picked']):>4} "
                f"{per:>7.0f}ms {r['cpu']:>7.2f}s"
            )


if __name__ == "__main__":
    main()
