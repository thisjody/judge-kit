"""Tests written from the blind review of 8130d9a, each failing on that commit before its fix."""

import json
import os
import subprocess

import pytest

from judge_kit.cli import main
from judge_kit.consensus import build_key
from judge_kit.graders import ClaudeCodeGrader, GraderError, Item, SimulatedGrader
from judge_kit.splits import split
from judge_kit.vault import Vault, VaultError, config_hash

LABELS = {f"i{n:03d}": n % 3 == 0 for n in range(60)}
A, B = {"m": "A"}, {"m": "B"}


def sealed(root, **kw):
    v = Vault(root, **kw)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    return v


def gen(v):
    return v.root / "gen-001"


# 1. a swapped frozen config, with its own hash recomputed, is caught
def test_swapped_freeze_is_caught(tmp_path):
    v = sealed(tmp_path)
    v.freeze(A)
    fz = json.loads((gen(v) / "FREEZE.json").read_text())
    fz["config"], fz["config_hash"] = B, config_hash(B)
    (gen(v) / "FREEZE.json").write_text(json.dumps(fz))
    assert any("ledger" in p and "freeze" in p for p in v.verify())
    with pytest.raises(VaultError, match="not the frozen config"):
        v.open(B)  # the ledger still names A


# 2. held-out labels and ids are covered, and the seal's hash is checked against the ledger
def test_flipped_heldout_labels_are_caught(tmp_path):
    v = sealed(tmp_path)
    p = gen(v) / "heldout.json"
    body = json.loads(p.read_text())
    body["labels"] = {k: not x for k, x in body["labels"].items()}
    p.write_text(json.dumps(body))
    assert any("held-out" in q for q in v.verify())


def test_shrunk_heldout_with_recomputed_hash_is_caught(tmp_path):
    from judge_kit.splits import manifest_hash

    v = sealed(tmp_path)
    p = gen(v) / "heldout.json"
    body = json.loads(p.read_text())
    body["ids"] = body["ids"][:2]
    body["labels"] = {k: body["labels"][k] for k in body["ids"]}
    body["manifest_hash"] = manifest_hash(body["ids"])
    p.write_text(json.dumps(body))
    assert any("ledger" in q for q in v.verify())


def test_tune_file_edit_is_caught(tmp_path):
    v = sealed(tmp_path)
    p = gen(v) / "tune.json"
    body = json.loads(p.read_text())
    k = next(iter(body["labels"]))
    body["labels"][k] = not body["labels"][k]
    p.write_text(json.dumps(body))
    assert any("tune set" in q for q in v.verify())


# 3. the tuning cap is fixed at seal time and can't be bypassed
def test_deleted_tune_round_is_caught_and_cap_holds(tmp_path):
    v = sealed(tmp_path)
    for _ in range(3):
        v.record_tune(A)
    (gen(v) / "tune-3.json").unlink()
    with pytest.raises(VaultError, match="cap"):
        v.record_tune(A)
    assert any("tune" in q for q in v.verify())


def test_cap_is_the_sealed_one(tmp_path):
    sealed(tmp_path)  # default cap 3
    v = Vault(tmp_path, max_tune_rounds=50)
    for _ in range(3):
        v.record_tune(A)
    with pytest.raises(VaultError, match="cap"):
        v.record_tune(A)


def test_missing_middle_round_is_a_vault_error(tmp_path):
    v = sealed(tmp_path)
    for _ in range(3):
        v.record_tune(A)
    (gen(v) / "tune-2.json").unlink()
    with pytest.raises(VaultError):
        v.record_tune(A)


# 4. a deleted freeze is caught
def test_deleted_freeze_is_caught(tmp_path):
    v = sealed(tmp_path)
    v.freeze(A)
    (gen(v) / "FREEZE.json").unlink()
    assert any("no FREEZE.json" in q for q in v.verify())
    with pytest.raises(VaultError):
        v.record_tune(A)  # the ledger still says frozen: tuning stays closed


# 5. a torn ledger tail does not swallow the next event, and repair acknowledges it
def test_torn_tail_does_not_eat_next_event(tmp_path):
    v = sealed(tmp_path)
    with open(v.ledger, "a") as f:
        f.write('{"event": "tu')
    v.freeze(A)
    assert [e["event"] for e in v.events()] == ["seal", "torn-line", "freeze"]
    assert any("torn" in q for q in v.verify())
    v.repair()
    assert v.verify() == []


# 6. any grader exception is an errored vote, not a lost run
def test_grader_exceptions_become_errored(tmp_path):
    class Slow:
        name = "slow"

        def grade(self, item, rubric):
            if item.id == "b":
                raise subprocess.TimeoutExpired("claude", 1)
            if item.id == "c":
                raise RuntimeError("api error")
            return True

    items = [Item("a", {}), Item("b", {}), Item("c", {})]
    k = build_key(items, [Slow(), SimulatedGrader("s", lambda i: True, 0, 0)], "r")
    assert k.labels == {"a": True} and sorted(k.errored) == ["b", "c"]


def test_keyboard_interrupt_still_stops(tmp_path):
    class Stop:
        name = "stop"

        def grade(self, item, rubric):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        build_key([Item("a", {})], [Stop(), SimulatedGrader("s", lambda i: True, 0, 0)], "r")


# 7. the claude -p grader runs with no tools, no MCP, no settings, no saved session, in an empty directory
def test_claude_code_grader_is_locked_down():
    seen = {}

    def runner(argv, cwd):
        seen["argv"], seen["cwd"] = argv, cwd
        seen["listing"] = os.listdir(cwd)
        return '{"flag": false}'

    ClaudeCodeGrader("cc", runner=runner).grade(Item("i", {"x": 1}), "R")
    a = seen["argv"]
    assert a[a.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert a[a.index("--setting-sources") + 1] == ""
    assert seen["listing"] == [] and seen["cwd"] != os.getcwd()


# 8. each grader gets its own copy of the payload
def test_grader_cannot_change_what_the_next_grader_sees():
    seen = []

    class Meddler:
        name = "a"

        def grade(self, item, rubric):
            item.payload["a_said"] = True
            return True

    class Watcher:
        name = "b"

        def grade(self, item, rubric):
            seen.append(dict(item.payload))
            return True

    items = [Item("x", {"k": {"nested": 1}})]
    build_key(items, [Meddler(), Watcher()], "r")
    assert seen == [{"k": {"nested": 1}}] and items[0].payload == {"k": {"nested": 1}}


# 9. read-only commands on a missing directory refuse
def test_verify_on_missing_dir_refuses(tmp_path, capsys):
    d = tmp_path / "typo"
    assert main(["verify", "--dir", str(d)]) == 2
    assert not d.exists()
    for cmd in (["status", "--dir", str(d)], ["repair", "--dir", str(d)]):
        assert main(cmd) == 2


# 10. stray gen-* entries don't crash, and lookalikes aren't generations
def test_stray_gen_dirs_are_ignored(tmp_path):
    v = sealed(tmp_path)
    for name in ("gen-old", "gen-001-bak"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "heldout.json").write_text("{}")
    assert v.current().n == 1 and v.verify() == []


# 11. non-bool labels are refused, not dropped
def test_split_refuses_non_bool_labels():
    with pytest.raises(ValueError, match="true/false"):
        split({**{f"p{n}": 1 for n in range(5)}, **{f"n{n}": False for n in range(5)}}, 0.4, 0)


# 12. open refuses, without spending the set, when the judge didn't call every held-out item
def test_open_refuses_incomplete_judged_without_spending(tmp_path, capsys):
    (tmp_path / "key.json").write_text(json.dumps(LABELS))
    (tmp_path / "cfg.json").write_text(json.dumps(A))
    (tmp_path / "judged.json").write_text("{}")
    d = str(tmp_path / "v")
    assert main(["seal", "--dir", d, "--labels", str(tmp_path / "key.json")]) == 0
    assert main(["freeze", "--dir", d, "--config", str(tmp_path / "cfg.json")]) == 0
    assert main(["open", "--dir", d, "--config", str(tmp_path / "cfg.json"), "--judged", str(tmp_path / "judged.json")]) == 2
    assert Vault(d).current().state == "frozen"
    assert "held-out" in capsys.readouterr().err


# 14. bad input files give a clean refusal, not a traceback
def test_bad_files_refuse_cleanly(tmp_path, capsys):
    (tmp_path / "bad.json").write_text("{not json")
    d = str(tmp_path / "v")
    assert main(["seal", "--dir", d, "--labels", str(tmp_path / "bad.json")]) == 2
    assert main(["seal", "--dir", d, "--labels", str(tmp_path / "missing.json")]) == 2
    (tmp_path / "key.json").write_text(json.dumps(LABELS))
    assert main(["seal", "--dir", d, "--labels", str(tmp_path / "key.json"), "--heldout-frac", "1.5"]) == 2
    err = capsys.readouterr().err
    assert "Traceback" not in err and err.count("refused") >= 3
