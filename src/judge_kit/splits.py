"""Split the keyed items into a tune set and a held-out set, seeded and stratified by label.

Stratified so a small held-out set still has both kinds of case. The manifest hash pins exactly which
ids are held out; the vault records it before anything is tuned.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Split:
    tune: tuple[str, ...]
    heldout: tuple[str, ...]
    seed: int

    def manifest_hash(self) -> str:
        return manifest_hash(self.heldout)


def manifest_hash(ids) -> str:
    return hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()


def split(labels: Mapping[str, bool], heldout_frac: float, seed: int) -> Split:
    if not 0.0 < heldout_frac < 1.0:
        raise ValueError("heldout_frac must be between 0 and 1")
    bad = [i for i, v in labels.items() if not isinstance(v, bool)]
    if bad:
        raise ValueError(f"labels must be true/false, e.g. {bad[:3]}")
    rng = random.Random(seed)
    tune: list[str] = []
    held: list[str] = []
    for value in (True, False):
        stratum = sorted(i for i, v in labels.items() if v is value)
        rng.shuffle(stratum)
        k = round(len(stratum) * heldout_frac)
        if len(stratum) >= 2:
            k = min(max(k, 1), len(stratum) - 1)  # both sides keep at least one of each label
        held.extend(stratum[:k])
        tune.extend(stratum[k:])
    return Split(tune=tuple(sorted(tune)), heldout=tuple(sorted(held)), seed=seed)
