"""judge-kit command line.

  judge-kit demo   [--dir D] [--n N] [--seed S]   the whole method on synthetic tool calls, offline
  judge-kit seal   --dir D --labels key.json [--heldout-frac F] [--seed S]
  judge-kit tune   --dir D --config cfg.json [--note TEXT]
  judge-kit freeze --dir D --config cfg.json
  judge-kit open   --dir D --config cfg.json --judged judged.json
  judge-kit status --dir D
  judge-kit verify --dir D
  judge-kit repair --dir D

JSON files: key.json and judged.json are {"<item id>": true|false, ...}; cfg.json is any JSON object
that fully describes the judge (for an LLM judge: model, prompt text, temperature).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from judge_kit.agreement import agreement
from judge_kit.consensus import build_key
from judge_kit.graders import SimulatedGrader
from judge_kit.splits import split
from judge_kit.vault import Vault, VaultError


class InputError(ValueError):
    pass


def _load(p: str) -> dict:
    try:
        obj = json.loads(Path(p).read_text())
    except FileNotFoundError:
        raise InputError(f"{p}: no such file") from None
    except json.JSONDecodeError as e:
        raise InputError(f"{p}: not valid JSON ({e.msg}, line {e.lineno})") from None
    except OSError as e:
        raise InputError(f"{p}: cannot read ({e.strerror})") from None
    if not isinstance(obj, dict):
        raise InputError(f"{p}: expected a JSON object")
    return obj


def _bools(p: str) -> dict[str, bool]:
    obj = _load(p)
    bad = [k for k, v in obj.items() if not isinstance(v, bool)]
    if bad:
        raise InputError(f"{p}: values must be true/false, e.g. {bad[:3]}")
    return obj


def cmd_demo(a) -> int:
    from judge_kit.example import BASELINE_CONFIG, RUBRIC, TUNE_ROUNDS, RuleJudge, generate

    out = print
    root = Path(a.dir) if a.dir else Path(tempfile.mkdtemp(prefix="judge-kit-demo-"))
    items, truth, hard_ids = generate(a.n, seed=a.seed)
    by_id = {i.id: i for i in items}

    def t(item):
        return truth[item.id]

    def hard(item):
        return item.id in hard_ids

    def blind(item):  # one shared blind spot: every grader reads "10" as a number
        args = item.payload["args"]
        return item.payload["tool"] == "http.get" and isinstance(args.get("timeout_s"), str)

    graders = [
        SimulatedGrader("grader-a", t, false_alarm=0.04, miss=0.06, seed=a.seed, hard=hard, hard_extra=0.15, blind=blind),
        SimulatedGrader("grader-b", t, false_alarm=0.05, miss=0.05, seed=a.seed, hard=hard, hard_extra=0.15, blind=blind),
        SimulatedGrader("grader-c", t, false_alarm=0.03, miss=0.08, seed=a.seed, hard=hard, hard_extra=0.15, blind=blind),
    ]

    out(f"== 1. key: {len(items)} synthetic tool calls, 3 isolated graders, unanimous cells only")
    key = build_key(items, graders, RUBRIC, seed=a.seed)
    out("   " + key.summary())
    wrong = sorted(i for i, v in key.labels.items() if v != truth[i])
    hard_share_all = len(hard_ids) / len(items)
    hard_share_key = sum(1 for i in key.labels if i in hard_ids) / max(1, len(key.labels))
    out(f"   synthetic data lets us check the key against planted truth: {len(wrong)} unanimous labels are WRONG")
    out(f"   (the graders share a blind spot); hard cases are {hard_share_all:.0%} of the data but {hard_share_key:.0%} of the key")
    out("   -> consensus is not truth. Report coverage and what was dropped with every result.")

    out("== 2. split: seeded, stratified; seal the held-out set")
    sp = split(key.labels, heldout_frac=0.4, seed=a.seed)
    v = Vault(root)
    g = v.seal(sp, key.labels)
    out(f"   generation {g}: tune {len(sp.tune)}, held-out {len(sp.heldout)} (manifest {sp.manifest_hash()[:12]})")

    tune_items = [by_id[i] for i in sp.tune]
    tune_key = v.tune_set()

    def score(cfg):
        return agreement(RuleJudge(cfg).judge_all(tune_items), tune_key, boot=500, seed=a.seed)

    out("== 3. tune on the tune set only (cap: 3 rounds)")
    base = score(BASELINE_CONFIG)
    out(f"   baseline: kappa {base.kappa:.3f}, false alarms {base.fp}, misses {base.fn}")
    best_cfg, best = BASELINE_CONFIG, base
    for note, cfg in TUNE_ROUNDS:
        r = v.record_tune(cfg, note)
        rep = score(cfg)
        out(f"   round {r}: {note}: kappa {rep.kappa:.3f}, false alarms {rep.fp}, misses {rep.fn}")
        if rep.kappa > best.kappa:
            best_cfg, best = cfg, rep
    try:
        v.record_tune(BASELINE_CONFIG, "a fourth round")
    except VaultError as e:
        out(f"   a 4th round is refused: {e}")

    out("== 4. freeze the chosen judge BEFORE opening the held-out set")
    ch = v.freeze(best_cfg)
    out(f"   frozen config {ch[:12]} against held-out manifest {sp.manifest_hash()[:12]}")

    out("== 5. open once")
    held_key = v.open(best_cfg)
    held_items = [by_id[i] for i in held_key]
    rep = agreement(RuleJudge(best_cfg).judge_all(held_items), held_key, seed=a.seed)
    for line in rep.lines():
        out("   " + line)
    vs_truth = agreement(RuleJudge(best_cfg).judge_all(held_items), {i: truth[i] for i in held_key}, boot=0)
    out(f"   (synthetic only) against planted truth: false alarms {vs_truth.fp}, misses {vs_truth.fn}")
    out("   the gap between those two lines is the key's error, not the judge's")
    try:
        v.open(best_cfg)
    except VaultError as e:
        out(f"   a second open is refused: {e}")
    try:
        v.record_tune(best_cfg, "retune after peeking")
    except VaultError as e:
        out(f"   retuning is refused: {e}")

    out("== 6. a spent set is replaced, never reused")
    items2, truth2, hard2 = generate(max(40, a.n // 2), seed=a.seed + 1000)
    items2 = [type(i)(id="r" + i.id, payload=i.payload) for i in items2]
    truth.update({"r" + k: v2 for k, v2 in truth2.items()})
    hard_ids.update("r" + h for h in hard2)
    key2 = build_key(items2, graders, RUBRIC, seed=a.seed + 1)
    g2 = v.seal(split(key2.labels, heldout_frac=0.4, seed=a.seed + 1), key2.labels)
    out(f"   generation {g2} sealed from fresh items; generation {g} stays spent")

    problems = v.verify()
    out(f"== verify: {'consistent' if not problems else problems}")
    out(f"   vault: {root}")
    return 0 if not problems else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="judge-kit", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--dir")
    d.add_argument("--n", type=int, default=400)
    d.add_argument("--seed", type=int, default=7)
    s = sub.add_parser("seal")
    s.add_argument("--dir", required=True)
    s.add_argument("--labels", required=True)
    s.add_argument("--heldout-frac", type=float, default=0.4)
    s.add_argument("--seed", type=int, default=0)
    for name in ("tune", "freeze"):
        x = sub.add_parser(name)
        x.add_argument("--dir", required=True)
        x.add_argument("--config", required=True)
        if name == "tune":
            x.add_argument("--note", default="")
    o = sub.add_parser("open")
    o.add_argument("--dir", required=True)
    o.add_argument("--config", required=True)
    o.add_argument("--judged", required=True)
    for name in ("status", "verify", "repair"):
        sub.add_parser(name).add_argument("--dir", required=True)
    a = p.parse_args(argv)

    try:
        if a.cmd == "demo":
            return cmd_demo(a)
        v = Vault(a.dir, create=(a.cmd == "seal"))
        if a.cmd == "seal":
            labels = _bools(a.labels)
            sp = split(labels, a.heldout_frac, a.seed)
            g = v.seal(sp, labels)
            print(f"sealed generation {g}: tune {len(sp.tune)}, held-out {len(sp.heldout)}, manifest {sp.manifest_hash()}")
        elif a.cmd == "tune":
            print(f"tune round {v.record_tune(_load(a.config), a.note)} recorded")
        elif a.cmd == "freeze":
            print(f"frozen: config {v.freeze(_load(a.config))}")
        elif a.cmd == "open":
            judged = _bools(a.judged)
            cfg = _load(a.config)
            uncalled = [i for i in v.heldout_ids() if i not in judged]
            if uncalled:  # refuse BEFORE opening: an open with nothing to score would spend the set for nothing
                raise VaultError(f"the judge made no call on {len(uncalled)} held-out items (e.g. {uncalled[:3]}); "
                                 "judge every item before opening")
            held = v.open(cfg)
            rep = agreement(judged, held)
            for line in rep.lines():
                print(line)
        elif a.cmd == "status":
            g = v.current()
            print(json.dumps(g.__dict__))
        elif a.cmd == "verify":
            probs = v.verify()
            print("consistent" if not probs else "\n".join(probs))
            return 0 if not probs else 1
        elif a.cmd == "repair":
            fixed = v.repair()
            print("\n".join(fixed) if fixed else "nothing to repair")
    except (VaultError, InputError, ValueError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
