# Verification

This file records what was run on this code, on which commit, and what wasn't. The raw outputs
are in [`evidence/`](evidence/).

**Code commit tested: `bf574e1`.** Every check in the table ran on that commit, except the blind
review rounds 1 to 4, which ran on the earlier commits named below, and the clean clone, which ran on
`710d2b0`. Later commits change only `README.md`, `VERIFICATION.md` and `evidence/`. After the last
README change, the suite and the demo docs check were run again.

Most checks ran in disposable sandboxes on a separate build host
([lid](https://github.com/thisjody/lid)): Python 3.13.5, 6 vCPU, 20 GB, no network egress, one
sandbox per check. Live acceptance ran in one of lid's agent sandboxes, which have open egress;
its traffic log is in `evidence/live-bf574e1/audit.txt`.

| check | what ran | result |
|---|---|---|
| Tests that can fail | `scripts/red_check.py` plants 31 defects one at a time and checks that the suite fails for each. Examples: majority instead of unanimity, opening a set twice, tuning after the freeze, verify ignoring a swapped config, a deleted FREEZE.json or a forged ledger event, an id leaking into the grader prompt, the `claude -p` grader running with tools, false alarms counted as hits | 31/31 turned the suite red; the restored tree is green |
| Suite | 528 tests, including 200 kill -9 trials each for freeze and open | all pass |
| Stateful property test | a Hypothesis state machine over seal, tune, freeze, open, reopen-from-disk and repair: 3,000 examples of up to 30 steps, with the invariants checked after every step | pass |
| Mutation testing | mutmut 3.8.1 over consensus, splits, vault, agreement and graders: 1,778 mutants | 1,552 killed, 218 survived, 7 crashed the test run (segfault), 1 timed out; classes below |
| Chaos | kill -9 at every named point inside seal, tune, freeze and open; 200 random-delay kills each of freeze and open; 2, 8 and 16 processes racing to open | every trial reconciled. After `repair`, `verify` shows only the "recovered" notes the crash earned. Exactly one open is in the ledger. A crashed open counts as spent. Exactly one racer wins |
| Live acceptance | job `jk-live-bf574e1`, built from `git archive bf574e1` (`evidence/live-bf574e1/run.log`); `scripts/live_acceptance.py`: 3 Claude graders through the locked-down `claude -p` (one fresh process per call, in an empty directory, with no tools, MCP servers, settings or saved session), 20 synthetic items, 60 calls | PASS. No grader errors, all 20 items unanimous, and the key agreed with planted truth on all 20 |
| Load | `scripts/load.py` runs key, split, seal, freeze, open and agreement end to end with 3 graders, from 1,000 items up, each size in its own process | measured to 10M items; table below |
| Clean clone | a fresh `gh repo clone` on macOS (arm64), then every README command: the tests, the demo, the five file-workflow commands, the refusal on an incomplete `judged.json`, the Python sketch, and `uv tool install` | every command did what the README says (`evidence/cleanclone/macos.txt`); the walkthrough found a missing install note and a wrong test-time claim, both fixed in the README |
| Docs | `gate/docs_check.py` matches 15 hand-written patterns, one for each number the README states about the demo, against a fresh demo run. The rest of the README and this file were checked against the evidence by reading | 15/15 |
| Blind review | six rounds by fresh AI reviewer agents that did not write the code; below | the fifth, limited to the core invariants, found no break; the sixth, a whole-tree read, found doc and evidence errors (fixed here) and two low code findings (under Not covered) |
| Security scan | | **not run** |

## Blind review rounds

Each round's findings got a failing test before the fix. The red runs are in `evidence/review*-red.txt`.

1. **The whole tree at 8130d9a: 14 findings.**
   - `verify` caught only internal inconsistency. A swapped config, flipped labels, a deleted FREEZE.json or a deleted tune file all passed.
   - The tuning cap could be bypassed.
   - A torn ledger line swallowed the next event.
   - One grader timeout ended the run.
   - The `claude -p` grader loaded the user's tools, MCP servers and project memory.
   - Graders shared one payload object.
   - `verify` on a mistyped directory said "consistent".
   - Smaller CLI gaps.

   All fixed in d50af83.
2. **The fix diff: 7 findings.**
   - `repair` could rebuild a swapped freeze after a crash-like ledger truncation, and `verify` then came back clean.
   - A deleted generation went unnoticed.
   - A crash mid-tune could never be repaired.
   - An older-format vault was spent and then refused.
   - Three small gaps.

   All fixed in 1303ce5. A step rebuilt from its file is now reported as recovered for good.
3. **That fix diff: 7 findings.** Two ways for a vault to get stuck, a padded file name crashing `repair`, a file given as the vault directory, and two doc mismatches. Fixed in 26731d2.
4. **That fix diff.** One appended ledger line could make an unopened set count as spent, and `verify` stayed clean. Fixed in bf574e1 by removing the feature the line exploited.
5. **A review of bf574e1, limited to the core invariants.** The reviewer reports fuzzing about 2,000 one-file edits (one data file, or the ledger alone) across four vault stages. None changed what was sealed, frozen or opened and still passed `verify`. Its scripts were not kept, so this result is the reviewer's report and can't be rerun from this repo. Its three low findings are listed under Not covered.
6. **A whole-tree read of the docs-only commit d5f997c.** It found:
   - errors in this file, all corrected: the commit scope, the mutant count, the round count, the docs-check wording, two load numbers, and the live run's commit not being recorded;
   - sandbox ids in the evidence, since removed;
   - two low code findings, under Not covered.

## Mutation survivors (218)

- **111 change only the text of an error or problem message.** The tests match a fragment of each message, not every word.
- **34 are in `repair()`:**
  - mostly the fields of the ledger lines it writes for a recovered step (the recovered flag, the config hash it copies), whose exact values the tests don't pin;
  - and the arithmetic acknowledging torn lines when there is only one.
- **33 are in `verify()`:**
  - mostly in checks that sit behind an earlier check reporting the same tampering, so removing one leaves the other to fail the vault;
  - and the reuse check across three or more generations, which the tests exercise with two.
- **40 are spread across 19 other functions.** Examples:
  - `generation()` falling back to the ledger when a file is missing, which the tests reach only with both records present;
  - the `bare` option of `ClaudeCodeGrader`, never tested;
  - upper clamps of `min(1.0, ...)` that can't bind;
  - `<` against `<=` on a uniform 64-bit draw;
  - `int.from_bytes`' default byte order.

The 107 that aren't message text were sorted by class, not triaged one by one. The full list is in `evidence/gate-bf574e1/shard-3/box/out/survivors.txt`.

## Load

| items | seconds | seconds per 1k | peak RSS (MB) | kappa | key coverage |
|---|---|---|---|---|---|
| 1,000 | 0.21 | 0.21 | 22 | 0.993 | 0.743 |
| 3,000 | 0.22 | 0.075 | 24 | 0.995 | 0.742 |
| 10,000 | 0.78 | 0.078 | 32 | 0.995 | 0.756 |
| 30,000 | 2.6 | 0.086 | 57 | 0.991 | 0.753 |
| 100,000 | 10.2 | 0.10 | 139 | 0.992 | 0.751 |
| 300,000 | 40.9 | 0.14 | 358 | 0.993 | 0.750 |
| 1,000,000 | 159 | 0.16 | 1,152 | 0.992 | 0.751 |
| 3,000,000 | 486 | 0.16 | 3,466 | 0.993 | 0.752 |
| 10,000,000 | 1,248 | 0.12 | 11,042 | 0.993 | 0.751 |

Time grows linearly: seconds per 1,000 items stay between 0.075 and 0.21 across four orders of
magnitude. Memory grows at about 1.1 KB per item, all of it held in memory. About 16 to 17 GB was
available inside a sandbox, which puts the ceiling at roughly 15M items on this host. It was **not run to out-of-memory, by design**: the
sandboxes share one VM, and a deliberate OOM there would hit every sandbox on it. The 20M step was
stopped, which is why that gate run's summary reads FAIL: shard 4 has no exit code. The ceiling is
a projection, not a measurement.

## Not covered

- **No security scan** has been run.
- **The consensus key is not ground truth,** and nothing here can make it so. The live run used three graders from one model family, on 20 easy-to-call synthetic items. That it agreed with planted truth there says nothing about harder cases.
- **Live acceptance covers `ClaudeCodeGrader` only.** `ClaudeGrader`, the Messages API adapter, is tested against a fake with the SDK's shape, not against the live API. The `bare=True` option was not run.
- **The vault is a discipline tool.** Anyone with file access can read `heldout.json`. Someone who edits the files and the ledger to match can forge a history, and `verify` checks that the records agree, not that they are true.
- **Python versions:** the suite ran on 3.13 (Linux sandboxes) and 3.11 (macOS). 3.10 and 3.12 were not run.
- **Local filesystems only.** The exactly-once guarantees rest on `link`, `O_EXCL` and `flock` behaving as they do on a local POSIX filesystem. Network filesystems were not tested.
- **Three low findings from the final review, not fixed:**
  - A forged ledger event whose name contains the "recovered from its file after a crash" wording is forgiven by `verify --allow-recovered`, though plain `verify` reports it. Such an event changes no state.
  - A ledger line that is valid JSON but not an object, such as a bare `5`, makes `verify` crash instead of reporting it.
  - `verify` doesn't compare each `tune-N.json` config with the hash the ledger logged for that round. That doesn't affect anything sealed, frozen or opened.
- **Two low findings from the whole-tree read, not fixed:**
  - `open` checks `heldout.json` against the hashes stored in that same file, not against the
    freeze. Someone who edits the labels and recomputes their hash after the freeze gets those labels
    back from `open`. The set is spent, and `verify` then reports the mismatch.
  - A grader's return value isn't type-checked. A custom grader that returns the string `"false"`
    counts as a flag.
- **Mutation survivors:** 107 were sorted by class rather than triaged one by one; see above.
