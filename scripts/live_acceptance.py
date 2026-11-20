"""Live acceptance: real Claude graders on 20 synthetic tool calls.

Three graders, each item graded in a fresh `claude -p` process (isolation by construction). Builds the
consensus key, then reports coverage, the key against planted truth, and the frozen example judge
against the key. Writes the full record to the path given (default live_acceptance.json).

This is the check that the fakes in the tests are faithful: the same ClaudeCodeGrader code path,
the same parse_flag, against the real CLI's real output.
"""

import json
import sys
import time

from judge_kit.agreement import agreement
from judge_kit.consensus import build_key
from judge_kit.example import RUBRIC, TUNE_ROUNDS, RuleJudge, generate
from judge_kit.graders import ClaudeCodeGrader, GraderError, parse_flag


class Recording(ClaudeCodeGrader):
    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        self.raw = []

    def grade(self, item, rubric):
        import tempfile

        argv = self.argv(item, rubric)  # the same locked-down argv the shipped grader uses
        t = time.time()
        try:
            with tempfile.TemporaryDirectory(prefix="judge-kit-grader-") as cwd:
                out = self._run(argv, cwd)
        except Exception as e:  # recorded, then surfaced as a grader error
            self.raw.append({"id": item.id, "error": repr(e)[:300]})
            raise GraderError(str(e)) from e
        self.raw.append({"id": item.id, "stdout": out[:2000], "s": round(time.time() - t, 2)})
        return parse_flag(out)


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "live_acceptance.json"
    items, truth, hard = generate(20, seed=11, bad_frac=0.5, hard_frac=0.4)
    graders = [Recording(f"claude-{i}") for i in range(3)]
    t0 = time.time()
    key = build_key(items, graders, RUBRIC, seed=11)
    wrong = sorted(i for i, v in key.labels.items() if v != truth[i])
    cfg = TUNE_ROUNDS[-1][1]
    judged = RuleJudge(cfg).judge_all(items)
    rep = agreement(judged, key.labels, boot=500, seed=1)
    out = {
        "items": len(items), "hard": sorted(hard), "planted_truth": truth,
        "key_summary": key.summary(), "coverage": key.coverage, "labels": key.labels,
        "dropped": key.dropped, "errored": key.errored, "votes": key.votes,
        "key_wrong_vs_truth": wrong, "judge_vs_key": rep.lines(),
        "graders_raw": {g.name: g.raw for g in graders}, "seconds": round(time.time() - t0, 1),
    }
    open(path, "w").write(json.dumps(out, indent=1, sort_keys=True))
    print(key.summary())
    print(f"key labels wrong vs planted truth: {len(wrong)} {wrong}")
    for line in rep.lines():
        print(line)
    n_calls = sum(len(g.raw) for g in graders)
    n_err = sum(1 for g in graders for r in g.raw if "error" in r)
    print(f"grader calls: {n_calls}, errors: {n_err}, seconds: {out['seconds']}")
    ok = n_calls == 60 and len(key.errored) <= 2 and key.coverage > 0
    print("LIVE ACCEPTANCE " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
