"""Two pieces of TPE's per-ask work, done once instead of on every ask. Same answers.

Profiled on a Search Lab task with eight busy slots, two thirds of the backend's CPU went to
the sampler's asks, and a third of that to two loops that redo finished work each time:

- **Group decomposition.** With ``group=True`` Optuna splits the parameters into groups that
  always appear together, by merging every finished trial's parameter names into a running
  partition — all of them again on every ask, though the partition it merges into is kept
  between asks and merging a set already merged changes nothing (each existing group is
  either inside it or apart from it). :class:`_IncrementalGroups` merges each trial once.
- **Category positions.** Each ask turns every trial's choice into its position in the
  choice list with ``tuple.index``, a linear scan: 1,500 fields deep for the field slot,
  for each of thousands of trials. :func:`_indexed` answers from a table built once per list,
  first occurrence first, exactly as ``tuple.index`` does.

Both lean on Optuna internals (5.0, pinned in ``uv.lock``); where those are not as expected
the sampler is left as Optuna made it. ``bench/tpe_equivalence.py`` checks that a sampler
with both asks exactly what one without them asks.
"""

from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable

    import optuna

log = structlog.get_logger(__name__)

#: Choice lists shorter than this are scanned faster than a table is built.
MIN_INDEXED = 32
#: Tables kept, most recently used last. A study has a handful of long lists, each one tuple
#: shared by every trial's copy of it (``copy.deepcopy`` hands back a tuple of strings as is).
MAX_TABLES = 64

_tables: OrderedDict[int, tuple[tuple[Any, ...], dict[Any, int]]] = OrderedDict()
_patched = False


def _table(choices: tuple[Any, ...]) -> dict[Any, int] | None:
    key = id(choices)
    kept = _tables.get(key)
    if kept is not None and kept[0] is choices:
        _tables.move_to_end(key)
        return kept[1]
    try:
        table: dict[Any, int] = {}
        for position, choice in enumerate(choices):
            # The first position wins, as ``tuple.index`` returns the first match.
            table.setdefault(choice, position)
    except TypeError:
        return None
    # The tuple is kept with its table, so its id cannot be reused while the entry lives.
    _tables[key] = (choices, table)
    while len(_tables) > MAX_TABLES:
        _tables.popitem(last=False)
    return table


def _indexed(original: Callable[[Any, Any], float]) -> Callable[[Any, Any], float]:
    def to_internal_repr(self: Any, value: Any) -> float:
        choices = self.choices
        if len(choices) >= MIN_INDEXED:
            table = _table(choices)
            if table is not None:
                try:
                    return table[value]
                except KeyError, TypeError:
                    pass
        # Not found, unhashable or short: Optuna's own scan and its float-tolerant fallback.
        return original(self, value)

    to_internal_repr.__wrapped__ = original  # type: ignore[attr-defined]
    return to_internal_repr


def patch_categorical() -> None:
    """Index long choice lists for ``CategoricalDistribution.to_internal_repr``. Idempotent."""
    global _patched
    if _patched:
        return
    from optuna.distributions import CategoricalDistribution

    method = CategoricalDistribution.to_internal_repr
    if getattr(method, "__wrapped__", None) is None:
        CategoricalDistribution.to_internal_repr = _indexed(method)  # type: ignore[method-assign]
    _patched = True


def _incremental_groups() -> type | None:
    try:
        from optuna.search_space.group_decomposed import (
            _GroupDecomposedSearchSpace,  # pyright: ignore[reportPrivateUsage]
        )
        from optuna.trial import TrialState
    except ImportError:
        return None

    class _IncrementalGroups(_GroupDecomposedSearchSpace):
        """Merges each finished trial into the partition once, not on every ask."""

        def __init__(self, include_pruned: bool = False) -> None:
            super().__init__(include_pruned)
            self._merged: set[int] = set()

        def calculate(self, study: optuna.Study, use_cache: bool = False) -> Any:
            import copy

            if self._study_id is None:
                self._study_id = study._study_id  # pyright: ignore[reportPrivateUsage]
            elif self._study_id != study._study_id:  # pyright: ignore[reportPrivateUsage]
                raise ValueError("`_GroupDecomposedSearchSpace` cannot handle multiple studies.")
            states = (
                (TrialState.COMPLETE, TrialState.PRUNED)
                if self._include_pruned
                else (TrialState.COMPLETE,)
            )
            for trial in study._get_trials(deepcopy=False, states=states, use_cache=use_cache):  # pyright: ignore[reportPrivateUsage]
                # A finished trial's parameters never change, so once merged it is done.
                if trial.number in self._merged:
                    continue
                self._search_space.add_distributions(trial.distributions)
                self._merged.add(trial.number)
            return copy.deepcopy(self._search_space)

    return _IncrementalGroups


def speed_up(sampler: Any) -> Any:
    """Give a ``TPESampler`` both shortcuts; one whose internals differ is returned as is."""
    patch_categorical()
    current = getattr(sampler, "_group_decomposed_search_space", None)
    groups = _incremental_groups()
    if current is not None and groups is not None:
        if {"_search_space", "_study_id", "_include_pruned"} <= vars(current).keys():
            sampler._group_decomposed_search_space = groups(current._include_pruned)
        else:
            log.warning("tpe_speed.groups_skipped", reason="unexpected Optuna internals")
    return sampler
