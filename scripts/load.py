"""Load: one scaling run to the knee. Key, split, seal, freeze, open and agreement over growing n.

Prints a JSON line per size with wall seconds per stage, peak RSS and seconds per 1k items, then the
knee: the first size where seconds-per-item grows by more than 1.5x over the previous size, or where
the run fails (memory, time budget). Sizes under 100k are reported but not used to find the knee:
their per-item time is timer noise. Run: python scripts/load.py [--max N] [--budget-s S]
"""

import argparse
import json
import resource
import sys
import tempfile
import time

from judge_kit.agreement import agreement
from judge_kit.consensus import build_key
from judge_kit.example import RUBRIC, TUNE_ROUNDS, RuleJudge, generate
from judge_kit.graders import SimulatedGrader
from judge_kit.splits import split
from judge_kit.vault import Vault


def rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def run(n: int, graders_n: int) -> dict:
    t0 = time.perf_counter()
    items, truth, hard = generate(n, seed=1)
    t1 = time.perf_counter()
    gs = [SimulatedGrader(f"g{i}", lambda it: truth[it.id], 0.04, 0.06, seed=1, hard=lambda it: it.id in hard, hard_extra=0.15) for i in range(graders_n)]
    key = build_key(items, gs, RUBRIC, seed=1)
    t2 = time.perf_counter()
    sp = split(key.labels, 0.4, 1)
    with tempfile.TemporaryDirectory() as d:
        v = Vault(d)
        v.seal(sp, key.labels)
        cfg = TUNE_ROUNDS[-1][1]
        v.freeze(cfg)
        held = v.open(cfg)
        t3 = time.perf_counter()
        by = {i.id: i for i in items}
        judged = RuleJudge(cfg).judge_all([by[i] for i in held])
        rep = agreement(judged, held, boot=200, seed=1)
        t4 = time.perf_counter()
        assert v.verify() == []
    total = t4 - t0
    return {"n": n, "graders": graders_n, "generate_s": round(t1 - t0, 3), "key_s": round(t2 - t1, 3),
            "vault_s": round(t3 - t2, 3), "agreement_s": round(t4 - t3, 3), "total_s": round(total, 3),
            "s_per_1k": round(total / n * 1000, 4), "peak_rss_mb": round(rss_mb(), 1), "kappa": round(rep.kappa, 4),
            "coverage": round(key.coverage, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=3_000_000)
    ap.add_argument("--graders", type=int, default=3)
    ap.add_argument("--budget-s", type=float, default=900)
    ap.add_argument("--one", type=int, help="run one size in this process and print its row")
    a = ap.parse_args()
    if a.one:
        print(json.dumps(run(a.one, a.graders)), flush=True)
        return 0
    sizes = [n for n in (1_000, 3_000, 10_000, 30_000, 100_000, 300_000, 1_000_000, 3_000_000, 10_000_000, 20_000_000, 40_000_000) if n <= a.max]
    rows, knee, start = [], None, time.perf_counter()
    for n in sizes:
        if time.perf_counter() - start > a.budget_s:
            knee = {"n": n, "why": "time budget spent before this size"}
            break
        # each size in its own process, so an out-of-memory kill is a measured knee, not a crash of the run
        import subprocess

        left = max(1.0, a.budget_s - (time.perf_counter() - start))
        try:
            p = subprocess.run([sys.executable, __file__, "--one", str(n), "--graders", str(a.graders)], capture_output=True, text=True, timeout=left)
        except subprocess.TimeoutExpired:
            knee = {"n": n, "why": "time budget ran out during this size"}
            break
        if p.returncode != 0:
            knee = {"n": n, "why": f"process exited {p.returncode} (negative = killed by signal; -9 is usually the OOM killer)", "stderr": p.stderr[-300:]}
            break
        r = json.loads(p.stdout.strip().splitlines()[-1])
        print(json.dumps(r), flush=True)
        # below 100k items a whole run takes well under a second, so its per-item time is timer noise
        if rows and n >= 100_000 and r["s_per_1k"] > 1.5 * rows[-1]["s_per_1k"] and knee is None:
            knee = {"n": n, "why": f"seconds per 1k items rose {r['s_per_1k'] / rows[-1]['s_per_1k']:.2f}x over n={rows[-1]['n']}"}
        rows.append(r)
    if not rows:
        print(json.dumps({"knee": knee, "largest_ok": None}))
        return 1
    print(json.dumps({"knee": knee or {"n": None, "why": f"no knee up to n={rows[-1]['n']}: cost stayed linear"}, "largest_ok": rows[-1]["n"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
