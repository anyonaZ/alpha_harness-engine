"""The sampler with ``labs.tpe_speed``'s shortcuts asks exactly what Optuna's own asks.

Replays one synthetic Search Lab history into two seeded samplers, Optuna's as it comes and the
app's, asks each for the same points (telling each the same scores back), and fails on the
first point that differs. Also reports CPU per ask for both. Run from ``backend/``:

    uv run python ../bench/tpe_equivalence.py --trials 3000 --asks 120
"""

# ruff: noqa: T201 - a benchmark reports on stdout

import argparse
import random
import time
from typing import Any

import optuna
from optuna.samplers import TPESampler
from optuna.trial import TrialState
from sampler_bench import RUN, history, make_space

from alpha_harness.labs import search, study


def asks(
    sampler: Any, space: dict[str, Any], rows: list[dict[str, Any]], n: int
) -> tuple[list[dict[str, Any]], float]:
    built = study._rebuild(sampler, rows, study._Distributions(search.slots(space)))
    run = RUN(space)
    choices = search.field_choices(space)
    scores = random.Random(7)
    asked, spent = [], 0.0
    live: list[Any] = []
    for i in range(n):
        started = time.thread_time()
        trial = built.ask()
        search.draw(trial, run, choices)
        spent += time.thread_time() - started
        asked.append(dict(trial.params))
        live.append(trial)
        # Batches of ten in flight, as a slot holds: the constant liar sees running trials.
        if len(live) >= 10 or i == n - 1:
            for open_trial in live:
                roll = scores.random()
                if roll < 0.15:
                    built.tell(open_trial, state=TrialState.PRUNED)
                elif roll < 0.25:
                    built.tell(open_trial, state=TrialState.FAIL)
                else:
                    built.tell(open_trial, scores.gauss(0.4, 0.7))
            live = []
    return asked, spent / n


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3000)
    parser.add_argument("--asks", type=int, default=120)
    parser.add_argument("--fields", type=int, default=1500)
    args = parser.parse_args()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    space = make_space(args.fields)
    rows = history(space, args.trials, seed=3)
    # Optuna's own first: the app's sampler patches a class method on first use.
    plain = TPESampler(
        n_startup_trials=20, multivariate=True, group=True, constant_liar=True, seed=11
    )
    expected, plain_cost = asks(plain, space, rows, args.asks)
    fast = study.make_sampler(20, 80, seed=11)
    got, fast_cost = asks(fast, space, rows, args.asks)
    for i, (a, b) in enumerate(zip(expected, got, strict=True)):
        if a != b:
            raise SystemExit(f"ask {i} differs:\n  optuna {a}\n  app    {b}")
    print(
        f"{args.asks} asks over {args.trials} trials, {args.fields} fields: identical. "
        f"{plain_cost * 1000:.0f} ms per ask as Optuna ships, "
        f"{fast_cost * 1000:.0f} ms with the shortcuts"
    )


if __name__ == "__main__":
    main()
