"""Does the windowed sampler find alphas as good as one that learns from every trial?

The plan's quality gate. Runs one synthetic Search Lab task twice per seed, with the same
sampler and the same scoring, differing only in the history the sampler learns from:

- **all**: every trial, as upstream does (Optuna's own sampler, with ``labs.tpe_speed``'s
  shortcuts, which ask exactly what it asks: ``bench/tpe_equivalence.py``).
- **window**: the Resource profile's window (best 500 + latest 1,500, rebuilt past 2,500),
  as ``labs.study.Optimizer.optuna_study`` keeps it.

Points are asked in batches of ten with the constant liar, as a slot's batch is, and scored
by a hidden objective with structure TPE can learn: some fields, operators, lookbacks and
neutralizations are better than others, some fields only with one family, plus noise, and
8% of points fail. The same point
always scores the same in both runs. Reported per arm, averaged over seeds:

- **hits per 1,000**: points scoring in the top 1% of what uniform sampling finds,
  the ones worth checking for submission;
- **top-25 mean** of true (noise-free) scores, and the best.

Pass: the window keeps at least 95% of all-history's hits and top-25 mean. Run from
``backend/``:

    uv run python ../bench/quality_replay.py --trials 6000 --seeds 3
    uv run python ../bench/quality_replay.py --start 10000 --trials 3000 --seeds 3
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import hashlib
import json
import math
import random
import statistics
import time
from typing import Any

import optuna
from optuna.distributions import distribution_to_json
from optuna.trial import TrialState
from sampler_bench import RUN, Recorder, make_space, window

from alpha_harness.labs import scheduler, search, study

BATCH = 10
FAIL_SHARE = 0.08


class Objective:
    """A hidden score over the Search Lab space: additive effects plus fixed noise."""

    def __init__(self, space: dict[str, Any], seed: int) -> None:
        rng = random.Random(seed)
        self.seed = seed
        self.effects: dict[tuple[str, Any], float] = {}
        for i, name in enumerate(space["fields"]):
            # A few fields carry real signal; most are noise around zero.
            good = 0.5 if i % 151 == 7 else 0.0
            self.effects["field", name] = rng.gauss(0.0, 0.15) + good
        for key in ("families", "cs", "ts", "lookbacks", "group", "groups", "vector"):
            for value in space[key]:
                self.effects[key, value] = rng.gauss(0.0, 0.12)
        for value in space["neutralizations"]:
            self.effects["neutralizations", value] = rng.gauss(0.0, 0.1)
        #: Some fields only pay with one family: structure no single parameter shows.
        self.pairs = {
            (name, rng.choice(space["families"])): 0.8
            for i, name in enumerate(space["fields"])
            if i % 97 == 11
        }

    _KEYS = (
        ("field", "field"),
        ("family", "families"),
        ("cs", "cs"),
        ("ts", "ts"),
        ("d", "lookbacks"),
        ("group_op", "group"),
        ("group", "groups"),
        ("vector_op", "vector"),
        ("neutralization", "neutralizations"),
    )

    def true(self, params: dict[str, Any]) -> float:
        return 0.3 + sum(
            self.effects.get((key, params[name]), 0.0) for name, key in self._KEYS if name in params
        )

    def _unit(self, text: str, salt: str) -> float:
        digest = hashlib.sha256(f"{self.seed}:{salt}:{text}".encode()).digest()
        return int.from_bytes(digest[:8], "big") / 2**64

    def observe(self, params: dict[str, Any], expression: str) -> float | None:
        """What a simulation reports: ``None`` for a failure, else the score with noise."""
        if self._unit(expression, "fail") < FAIL_SHARE:
            return None
        u1, u2 = max(self._unit(expression, "a"), 1e-12), self._unit(expression, "b")
        noise = math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)
        return self.true(params) + 0.5 * noise


def uniform_rows(
    space: dict[str, Any], objective: Objective, n: int, seed: int
) -> tuple[list[dict[str, Any]], list[float]]:
    """``n`` uniformly drawn, scored trial rows, as a task's history before this run."""
    rng = random.Random(seed)
    choices = search.field_choices(space)
    slots = search.slots(space)
    rows, truths = [], []
    for _ in range(n):
        rec = Recorder(rng)
        params = search.suggest(rec, space, choices)
        request = search.request_for(params, RUN(space))
        seen = objective.observe(params, request.regular)
        truths.append(objective.true(params))
        rows.append(
            {
                "params": params,
                "distributions": {
                    name: scheduler.FROM_SPACE
                    if name in slots
                    else distribution_to_json(optuna.distributions.CategoricalDistribution(c))
                    for name, c in rec.choices.items()
                },
                "values": [seen] if seen is not None else None,
                "state": "COMPLETE" if seen is not None else "FAIL",
            }
        )
    return rows, truths


#: What each arm learns from: best, latest, an even sample of the rest, and the size at which
#: the study is rebuilt. ``None``: every trial, as upstream.
ARMS: dict[str, tuple[int, int, int, int] | None] = {
    "all": None,
    "window": (500, 1500, 0, 2500),
    "spread": (500, 1500, 1000, 3500),
}


def spread_window(
    rows: list[dict[str, Any]], best: int, recent: int, spread: int
) -> list[dict[str, Any]]:
    """``window`` plus every k-th older trial, ``spread`` of them: a thin, even record of
    everything tried, so TPE still knows which regions were tried and found poor."""
    kept = window(rows, best, recent)
    if not spread:
        return kept
    replayed = [i for i, r in enumerate(rows) if r["state"] in ("COMPLETE", "FAIL", "PRUNED")]
    older = replayed[:-recent] if recent else replayed
    stride = max(1, -(-len(older) // spread))
    chosen = {id(rows[i]) for i in older[::stride]} | {id(r) for r in kept}
    return [r for r in rows if id(r) in chosen]


def run_arm(
    space: dict[str, Any],
    objective: Objective,
    start: list[dict[str, Any]],
    trials: int,
    seed: int,
    arm: tuple[int, int, int, int] | None,
) -> dict[str, Any]:
    slots = search.slots(space)
    loader = study._Distributions(slots)
    rows = list(start)
    windowed = arm is not None
    ceiling = arm[3] if arm else 0

    def build() -> optuna.Study:
        kept = spread_window(rows, *arm[:3]) if arm else rows
        # A rebuild is a new sampler, as in the app; the seed keeps runs repeatable.
        sampler = study.make_sampler(20, 80, seed=seed + len(rows))
        return study._rebuild(sampler, kept, loader)

    built = build()
    run = RUN(space)
    choices = search.field_choices(space)
    scores: list[float] = []
    truths: list[float] = []
    cpu = time.thread_time()
    while len(scores) < trials:
        if windowed and len(built.get_trials(deepcopy=False)) > ceiling:
            built = build()
        batch = []
        for _ in range(min(BATCH, trials - len(scores))):
            trial = built.ask()
            params, request = search.draw(trial, run, choices)
            batch.append((trial, params, request))
        for trial, params, request in batch:
            seen = objective.observe(params, request.regular)
            truth = objective.true(params)
            if seen is None:
                built.tell(trial, state=TrialState.FAIL)
            else:
                built.tell(trial, seen)
            scores.append(seen if seen is not None else float("-inf"))
            truths.append(truth)
            rows.append(
                {
                    "params": params,
                    "distributions": {
                        name: scheduler.FROM_SPACE
                        if name in slots
                        else distribution_to_json(distribution)
                        for name, distribution in trial.distributions.items()
                    },
                    "values": [seen] if seen is not None else None,
                    "state": "COMPLETE" if seen is not None else "FAIL",
                }
            )
    return {"scores": scores, "truths": truths, "cpu": time.thread_time() - cpu}


def summary(arm: dict[str, Any], hit: float) -> dict[str, float]:
    scores, truths = arm["scores"], arm["truths"]
    n = len(scores)
    top = sorted(truths, reverse=True)[:25]
    #: The last third of the run: where a sampler that lost what it learned would show it.
    late = scores[n - n // 3 :]
    return {
        "hits": sum(1 for s in scores if s >= hit) / n * 1000,
        "late_hits": sum(1 for s in late if s >= hit) / len(late) * 1000,
        "top25": statistics.fmean(top),
        "best": top[0],
        "cpu": arm["cpu"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=6000, help="points asked in the run")
    parser.add_argument("--start", type=int, default=0, help="uniform history before the run")
    parser.add_argument("--seeds", type=int, default=3)
    parser.add_argument("--seed-from", type=int, default=1)
    parser.add_argument("--arms", default="all,window", help=f"from {', '.join(ARMS)}")
    parser.add_argument("--fields", type=int, default=1500)
    parser.add_argument("--out", help="also append each result as a JSON line here")
    args = parser.parse_args()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    space = make_space(args.fields)
    arms = args.arms.split(",")

    print(
        f"Search Lab task, {args.fields} fields, {args.start} trials before, "
        f"{args.trials} asked, batches of {BATCH}"
    )
    print(f"{'seed':>4} {'arm':>7} {'hits/1k':>8} {'late':>6} {'top-25':>7} {'best':>6} {'CPU':>7}")
    totals: dict[str, list[dict[str, float]]] = {name: [] for name in arms}
    for seed in range(args.seed_from, args.seed_from + args.seeds):
        objective = Objective(space, seed)
        # The hit line: the top 1% of what uniform sampling observes.
        probe, _ = uniform_rows(space, objective, 20000, seed=10_000 + seed)
        observed = sorted(r["values"][0] for r in probe if r["values"])
        hit = observed[int(len(observed) * 0.99)]
        start, _ = uniform_rows(space, objective, args.start, seed=seed) if args.start else ([], [])
        for name in arms:
            result = summary(run_arm(space, objective, start, args.trials, seed, ARMS[name]), hit)
            totals[name].append(result)
            print(
                f"{seed:>4} {name:>7} {result['hits']:>8.1f} {result['late_hits']:>6.1f} "
                f"{result['top25']:>7.3f} {result['best']:>6.3f} {result['cpu']:>6.0f}s",
                flush=True,
            )
            if args.out:
                with open(args.out, "a") as out:  # noqa: PTH123
                    out.write(json.dumps({"seed": seed, "arm": name, **result}) + "\n")

    mean = {
        name: {k: statistics.fmean(r[k] for r in rs) for k in rs[0]} for name, rs in totals.items()
    }
    for name, m in mean.items():
        print(
            f"mean {name:>6} {m['hits']:>8.1f} {m['late_hits']:>6.1f} {m['top25']:>7.3f} "
            f"{m['best']:>6.3f} {m['cpu']:>6.0f}s"
        )
    if "all" not in mean:
        return
    a = mean["all"]
    for name, m in mean.items():
        if name == "all":
            continue
        hits = m["hits"] / a["hits"] if a["hits"] else 1.0
        top = m["top25"] / a["top25"] if a["top25"] else 1.0
        verdict = "PASS" if hits >= 0.95 and top >= 0.95 else "FAIL"
        print(
            f"{name} / all: hits {hits:.1%}, top-25 mean {top:.1%}, "
            f"CPU {m['cpu'] / a['cpu']:.0%} -> {verdict}"
        )


if __name__ == "__main__":
    main()
