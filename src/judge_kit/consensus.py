"""The blind consensus key: an item gets a label only when every grader agrees on it.

Unanimity throws data away on purpose. The cases graders split on are the ambiguous ones, and a key
built on a majority there would teach the judge the graders' coin flips. The dropped ids are kept and
reported, because "the key covers 82% of the data" is part of the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from judge_kit.graders import Grader, GraderError, Item, shuffled


@dataclass
class Key:
    labels: dict[str, bool]            # unanimous items only
    dropped: list[str]                 # graders disagreed
    errored: list[str] = field(default_factory=list)  # some grader could not answer
    votes: dict[str, dict[str, bool]] = field(default_factory=dict)  # item id -> grader -> call
    graders: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.labels) + len(self.dropped) + len(self.errored)

    @property
    def coverage(self) -> float:
        return len(self.labels) / self.total if self.total else 0.0

    def summary(self) -> str:
        pos = sum(self.labels.values())
        return (
            f"consensus key (not ground truth): {len(self.labels)}/{self.total} items unanimous "
            f"({self.coverage:.0%}); {pos} flagged, {len(self.labels) - pos} clean; "
            f"{len(self.dropped)} split, {len(self.errored)} errored; graders: {', '.join(self.graders)}"
        )


def key_from_votes(votes: Mapping[str, Mapping[str, bool | None]], graders: Sequence[str]) -> Key:
    """Build a key from recorded votes. A vote of None means that grader errored on that item."""
    if len(graders) < 2:
        raise ValueError("a consensus key needs at least two graders")
    labels: dict[str, bool] = {}
    dropped: list[str] = []
    errored: list[str] = []
    clean_votes: dict[str, dict[str, bool]] = {}
    for item_id in sorted(votes):
        v = votes[item_id]
        missing = [g for g in graders if g not in v]
        if missing:
            raise ValueError(f"item {item_id} has no vote from {missing}")
        calls = [v[g] for g in graders]
        if any(c is None for c in calls):
            errored.append(item_id)
            continue
        clean_votes[item_id] = {g: bool(v[g]) for g in graders}
        if all(calls):
            labels[item_id] = True
        elif not any(calls):
            labels[item_id] = False
        else:
            dropped.append(item_id)
    return Key(labels=labels, dropped=dropped, errored=errored, votes=clean_votes, graders=list(graders))


def build_key(items: Iterable[Item], graders: Sequence[Grader], rubric: str, seed: int = 0) -> Key:
    """Run each grader alone over the items, in its own shuffled order, then keep the unanimous ones."""
    items = list(items)
    ids = [i.id for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError("item ids must be unique")
    names = [g.name for g in graders]
    if len(set(names)) != len(names):
        raise ValueError("grader names must be unique")
    votes: dict[str, dict[str, bool | None]] = {i: {} for i in ids}
    for n, g in enumerate(graders):
        for item in shuffled(items, seed + n):
            try:
                votes[item.id][g.name] = g.grade(item, rubric)
            except GraderError:
                votes[item.id][g.name] = None
    return key_from_votes(votes, names)
