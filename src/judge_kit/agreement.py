"""How a judge agrees with the key, with confidence intervals.

A false-alarm rate of 0.05 on 40 clean items and one on 4,000 are different claims. Every rate here
carries a Wilson 95% interval, and kappa carries a seeded percentile-bootstrap interval.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Mapping

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. (0, 1) when n == 0: nothing is known."""
    if n < 0 or k < 0 or k > n:
        raise ValueError("need 0 <= k <= n")
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def kappa(tp: int, fp: int, fn: int, tn: int) -> float:
    """Cohen's kappa between judge and key. 1.0 when both agree perfectly, even on a single class."""
    n = tp + fp + fn + tn
    if n == 0:
        return float("nan")
    po = (tp + tn) / n
    pe = ((tp + fp) * (tp + fn) + (fn + tn) * (fp + tn)) / (n * n)
    if pe == 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


@dataclass
class Rate:
    k: int
    n: int
    lo: float = field(init=False)
    hi: float = field(init=False)

    def __post_init__(self):
        self.lo, self.hi = wilson(self.k, self.n)

    @property
    def value(self) -> float:
        return self.k / self.n if self.n else float("nan")

    def __str__(self) -> str:
        if not self.n:
            return "n/a (no cases)"
        return f"{self.value:.3f} [{self.lo:.3f}, {self.hi:.3f}] ({self.k}/{self.n})"


@dataclass
class Report:
    tp: int
    fp: int
    fn: int
    tn: int
    false_alarm: Rate   # judge flags, key says clean
    miss: Rate          # judge passes, key says flagged
    precision: Rate
    recall: Rate
    kappa: float
    kappa_ci: tuple[float, float]
    missing: list[str]

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    def lines(self) -> list[str]:
        return [
            f"items scored: {self.n}  (tp {self.tp}, fp {self.fp}, fn {self.fn}, tn {self.tn})",
            f"false-alarm rate: {self.false_alarm}",
            f"miss rate:        {self.miss}",
            f"precision:        {self.precision}",
            f"recall:           {self.recall}",
            f"kappa:            {self.kappa:.3f} [{self.kappa_ci[0]:.3f}, {self.kappa_ci[1]:.3f}] (bootstrap 95%)",
            "intervals are 95%: Wilson for rates, percentile bootstrap for kappa; agreement is with the consensus key, not with truth",
        ]


def _counts(pairs: list[tuple[bool, bool]]) -> tuple[int, int, int, int]:
    tp = fp = fn = tn = 0
    for judged, keyed in pairs:
        if judged and keyed:
            tp += 1
        elif judged and not keyed:
            fp += 1
        elif keyed:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def agreement(judged: Mapping[str, bool], key: Mapping[str, bool], boot: int = 2000, seed: int = 0) -> Report:
    """Score the judge's calls against the key, over the key's items.

    Items the judge did not call are listed in `missing` and left out of the counts. A judge that
    skips the hard cases should not look better for it, so check `missing` is empty.
    """
    ids = sorted(key)
    missing = [i for i in ids if i not in judged]
    pairs = [(bool(judged[i]), bool(key[i])) for i in ids if i in judged]
    tp, fp, fn, tn = _counts(pairs)
    k = kappa(tp, fp, fn, tn)
    if pairs and boot > 0:
        rng = random.Random(seed)
        ks = []
        for _ in range(boot):
            sample = [pairs[rng.randrange(len(pairs))] for _ in pairs]
            v = kappa(*_counts(sample))
            if not math.isnan(v):
                ks.append(v)
        ks.sort()
        lo = ks[int(0.025 * (len(ks) - 1))]
        hi = ks[int(math.ceil(0.975 * (len(ks) - 1)))]
        ci = (lo, hi)
    else:
        ci = (float("nan"), float("nan"))
    return Report(
        tp=tp, fp=fp, fn=fn, tn=tn,
        false_alarm=Rate(fp, fp + tn),
        miss=Rate(fn, fn + tp),
        precision=Rate(tp, tp + fp),
        recall=Rate(tp, tp + fn),
        kappa=k, kappa_ci=ci, missing=missing,
    )
