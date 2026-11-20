# judge-kit

Prove an LLM judge before you trust it.

When agents write the work, quality comes from how well the work gets checked. A common check is a
model acting as judge. But a judge is itself a model output, and it needs checking too. This kit is
the procedure I use for that, as runnable code:

1. **A blind consensus key.** Several graders, isolated from each other and from the intended
   answers, label the cases. Only the cases where every grader agrees get a label.
2. **Tune and held-out sets.** A seeded, stratified split. The held-out set is sealed by a hash of
   its ids before any tuning starts, and tuning is capped at a few rounds.
3. **Freeze.** The judge's full config is written down, hashed and tied to that held-out set
   before the set is opened.
4. **Open once.** The held-out set opens one time, for the frozen config only. After that it is spent.
5. **Replace a spent set.** Any further tuning needs a fresh held-out set, with no ids from an
   earlier one.

Every agreement number comes with a 95% interval: Wilson for rates, a seeded bootstrap for Cohen's kappa.

## Where it sits in the ladder

The usual ladder for checking agent-written software has four rungs:

| rung | what it checks | how I run it |
|---|---|---|
| 1. unit tests | the code does what it says | units plus mutation testing (do the tests catch planted bugs?), property tests and a failing test before every fix |
| 2. model as judge | agent output graded against a rubric | AI review agents and graders |
| **2½. prove the judge** | **is the judge right often enough to lean on?** | **this kit** |
| 3. throwaway apps | the thing works against a live service | live acceptance runs |
| 4. sandboxes with simulated users | it holds up under real load and failures | a chaos soak and a load test in disposable sandboxes |

Rung 2½ is the one that usually gets skipped. If the judge isn't checked, a system that optimizes
against it slowly learns to please the judge rather than to get things right, and nothing in rungs
3 and 4 will notice. For rungs 1, 3 and 4 run on a real system, see
[lid](https://github.com/thisjody/lid) and how it was verified:
[docs/HOW-IT-WAS-VERIFIED.md](https://github.com/thisjody/lid/blob/main/docs/HOW-IT-WAS-VERIFIED.md).

## Consensus is not truth

This is the part to keep front and centre. A key built from three graders is the best label you
have, not ground truth:

- **Shared blind spots pass unanimously.** Graders from one model family make the same mistakes, so
  unanimity can't catch those. The demo plants one, and the key gets those items wrong with every
  grader agreeing.
- **Unanimity drops the hard cases.** The cases graders split on are the ambiguous ones, so the key
  under-represents exactly the cases where a judge is most likely to fail. The kit reports coverage
  and every dropped id.
- **Agreement with the key measures agreement with the key.** Every report says so.

The kit does not make a judge right. It tells you how wrong it is, on cases it never saw, with an
honest interval.

## Install

Python 3.10 or later and [uv](https://docs.astral.sh/uv/). From a clone, prefix commands with
`uv run`, as below. To get a `judge-kit` command on your path instead:
`uv tool install git+https://github.com/thisjody/judge-kit`.

The tests: `uv run pytest -q` (about a minute; it includes the chaos and property tests).

## Run the demo

Offline, no API key, about a second:

```
uv run judge-kit demo
```

It runs every step on 400 synthetic agent tool calls, checked against a small workspace policy:
file writes stay inside `/workspace`, fetches only go to allowed hosts, no `curl | sh`, and arguments
must be well formed. Three simulated graders have known error rates, more errors on hard cases and
one shared blind spot. Because the data is synthetic, the demo can also show the thing you never get
in real life: the key and the judge compared against planted truth.

What it shows on the default seed:

- The key covers 76% of the items. Hard cases are 33% of the data but only 22% of the key. Three
  unanimous labels are wrong.
- Three tuning rounds take the rule judge from kappa 0.85 to 0.99 on the tune set. A fourth round is refused.
- On the held-out set: kappa 0.966 [0.915, 1.000], 2 false alarms out of 69 clean cases. Against
  planted truth the judge made no errors. The two "false alarms" are the key's blind spot, not the judge's.
- A second open is refused, a retune is refused, and a fresh set is sealed as generation 2.

The example judge is a deliberately simple rule judge, so the demo stays offline and repeatable.
The steps are the same for an LLM judge, whose config is its model, prompt and settings.

## Use it on your own judge

```
uv run judge-kit seal   --dir vault --labels key.json     # {"id": true|false} from your graders
uv run judge-kit tune   --dir vault --config judge.json --note "tightened the scope wording"
uv run judge-kit freeze --dir vault --config judge.json
uv run judge-kit open   --dir vault --config judge.json --judged judged.json
uv run judge-kit verify --dir vault
```

`judged.json` holds the frozen judge's call on every item, in the same form as `key.json`. `open`
refuses, without spending the set, if any held-out item is missing from it.

`judge.json` is any JSON object that fully describes the judge. For an LLM judge that's the model
id, the full prompt text and the sampling settings. `open` refuses unless that object hashes to the
frozen one.

Or from Python (a sketch: `items` is a list of `Item(id, payload)` and `rubric` is your rubric text):

```python
from judge_kit import build_key, split, Vault, agreement
from judge_kit.graders import ClaudeCodeGrader      # or ClaudeGrader (Messages API), or your own

key = build_key(items, [ClaudeCodeGrader(f"g{i}") for i in range(3)], rubric)
print(key.summary())
vault = Vault("vault")
vault.seal(split(key.labels, heldout_frac=0.4, seed=1), key.labels)
...
```

A grader is anything with a `name` and a `grade(item, rubric) -> bool`. Each grader gets its own
copy of every payload, so no grader can change what the next one sees. An exception from a grader,
such as a timeout or an API error, counts as an errored vote on that item, and the rest of the run
goes on.

The built-in LLM graders send the model the rubric and the payload, never the item id or any other
grader's call. `ClaudeCodeGrader` runs each call as a fresh `claude -p` process in a new empty
directory, with no tools, no MCP servers, no settings files and no saved session. Claude Code may
still load your user-level memory file (`~/.claude/CLAUDE.md`); `bare=True` skips that too, but it
needs an API-key login. Organization-managed Claude Code settings still apply. Your own graders receive `item.id` (the simulated graders use it), so
keeping them blind is up to you.

## What the vault guarantees, and what it doesn't

Each step writes a file that is created atomically and exactly once, and appends a line to a
ledger. A step counts as done if either record shows it, so deleting a file can't undo a freeze,
an open or a tuning round. The vault enforces:

- no held-out labels before a freeze;
- one freeze per set;
- one open per set, for the frozen config only, even with concurrent processes racing;
- tuning only before the freeze, up to a cap that is fixed when the set is sealed;
- no held-out id reused in a replacement set.

A process killed part-way through an open still spends the set, because it may already have been
read. The CLI's `open` refuses, without spending the set, if the judge hasn't made a call on every
held-out item.

`verify` recomputes the hashes of the held-out set (ids and labels), the tune set and the frozen
config, and checks each against the ledger. It reports any place where they disagree, such as an
edited label, a swapped config, a deleted file or a deleted generation.

After a crash, `repair` logs any step whose file landed but whose ledger line didn't, and
acknowledges torn ledger lines. It never creates or removes a step's file. A step rebuilt this way
can only be taken from its file, so `verify` lists it as recovered from then on, and `judge-kit
verify` exits 1 (`--allow-recovered` exits 0 when those notes are the only ones, for use in CI, unless a seal
had to be recovered, which is what a deleted ledger looks like).
A clean `verify` means the files and the ledger agree. It can't catch someone who edits both to
match, including by cutting ledger lines.

If a generation's files are deleted, `seal` refuses until they are restored, because without them
the vault can't check that a new held-out set reuses none of their ids.

It is a discipline tool, not a lock against its owner. Anyone can `cat` the held-out file, and
someone who edits the files and the ledger consistently can forge a history. What the vault does is
make the honest path the easy one, and make a careless or partial edit show up.

## Verification

What was run on this code, on which commit, and what wasn't: [VERIFICATION.md](VERIFICATION.md).

## License

Apache-2.0
