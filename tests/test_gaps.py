"""Tests added after the first mutation run: each one pins behaviour a surviving mutant changed."""

import json
import os
import stat
import sys
import types

import pytest

from judge_kit.agreement import agreement, kappa, percentile, wilson
from judge_kit.consensus import build_key, key_from_votes
from judge_kit.graders import ClaudeCodeGrader, ClaudeGrader, GraderError, Item, SimulatedGrader, parse_flag, shuffled
from judge_kit.splits import split
from judge_kit.vault import Vault, VaultError, config_hash

LABELS = {f"i{n:03d}": n % 3 == 0 for n in range(60)}
CFG = {"model": "m", "prompt": "p"}


# ---- messages say what went wrong ----------------------------------------------------------------

def test_error_messages():
    with pytest.raises(ValueError, match="at least two graders"):
        key_from_votes({}, ["g"])
    with pytest.raises(ValueError, match="has no vote from"):
        key_from_votes({"a": {"g1": True}}, ["g1", "g2"])
    g, h = SimulatedGrader("g", lambda i: True, 0, 0), SimulatedGrader("h", lambda i: True, 0, 0)
    with pytest.raises(ValueError, match="ids must be unique"):
        build_key([Item("x", {}), Item("x", {})], [g, h], "r")
    with pytest.raises(ValueError, match="names must be unique"):
        build_key([Item("x", {})], [g, g], "r")
    with pytest.raises(ValueError, match="between 0 and 1"):
        split({"a": True}, 0.0, 0)
    with pytest.raises(ValueError, match=r"0 <= k <= n"):
        wilson(-1, 3)
    with pytest.raises(ValueError, match=r"in \[0, 1\]"):
        SimulatedGrader("x", lambda i: True, 0.1, 1.5)


def test_wilson_argument_checks():
    for k, n in [(-1, 3), (4, 3), (0, -1)]:
        with pytest.raises(ValueError):
            wilson(k, n)
    assert wilson(3, 3)[1] == 1.0


# ---- consensus records -----------------------------------------------------------------------------

def test_key_records_votes_and_graders_and_keeps_going_after_errors():
    votes = {"a": {"g1": None, "g2": True}, "b": {"g1": True, "g2": True}, "c": {"g1": False, "g2": True}}
    k = key_from_votes(votes, ["g1", "g2"])
    assert k.errored == ["a"] and k.labels == {"b": True} and k.dropped == ["c"]
    assert k.votes == {"b": {"g1": True, "g2": True}, "c": {"g1": False, "g2": True}}
    assert k.graders == ["g1", "g2"]


def test_build_key_passes_rubric_and_shuffles_per_grader():
    seen = {}

    class Rec:
        def __init__(self, name):
            self.name = name
            seen[name] = []

        def grade(self, item, rubric):
            assert rubric == "THE RUBRIC"
            seen[self.name].append(item.id)
            return True

    items = [Item(f"i{n}", {}) for n in range(30)]
    build_key(items, [Rec("a"), Rec("b")], "THE RUBRIC", seed=4)
    assert seen["a"] == [i.id for i in shuffled(items, 4)]
    assert seen["b"] == [i.id for i in shuffled(items, 5)]
    assert seen["a"] != seen["b"]
    assert shuffled(items, 1) == shuffled(items, 1) and shuffled(items, 1) != shuffled(items, 2)


def test_build_key_default_seed_is_zero():
    order = []

    class Rec:
        name = "a"

        def grade(self, item, rubric):
            order.append(item.id)
            return True

    class Rec2(Rec):
        name = "b"

    items = [Item(f"i{n}", {}) for n in range(20)]
    build_key(items, [Rec(), Rec2()], "r")
    assert order[:20] == [i.id for i in shuffled(items, 0)]


# ---- simulated graders -------------------------------------------------------------------------------

def test_simulated_grader_edges():
    items = [Item(f"i{n}", {}) for n in range(3000)]
    SimulatedGrader("ok", lambda i: True, 1.0, 1.0)  # 1.0 is allowed
    always = SimulatedGrader("fa", lambda i: False, false_alarm=1.0, miss=0.0)
    assert all(always.grade(i, "") for i in items[:50])
    g = SimulatedGrader("h", lambda i: False, 0.0, 0.0, hard=lambda i: i.id.endswith("0"), hard_extra=0.5)
    flagged = [i.id for i in items if g.grade(i, "")]
    assert flagged and all(f.endswith("0") for f in flagged)  # extra errors only on hard items
    assert 0.4 < len(flagged) / 300 < 0.6
    g2 = SimulatedGrader("h2", lambda i: True, 0.0, 0.3, hard=lambda i: True, hard_extra=0.3)
    rate = sum(not g2.grade(i, "") for i in items) / len(items)
    assert 0.55 < rate < 0.65  # miss + extra
    assert SimulatedGrader("d", lambda i: True, 0.1, 0.1).seed == 0
    a = SimulatedGrader("s", lambda i: False, 0.5, 0.0, seed=1)
    b = SimulatedGrader("s", lambda i: False, 0.5, 0.0, seed=2)
    assert [a.grade(i, "") for i in items[:200]] != [b.grade(i, "") for i in items[:200]]
    u = [a._u(i) for i in items[:500]]
    assert all(0.0 <= x < 1.0 for x in u) and 0.4 < sum(u) / len(u) < 0.6


# ---- the Claude adapters -----------------------------------------------------------------------------

def test_claude_grader_sends_model_and_tokens(monkeypatch):
    calls = []

    class Msgs:
        def create(self, **kw):
            calls.append(kw)
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="thinking"), types.SimpleNamespace(type="text", text='{"flag": true}')])

    fake_sdk = types.SimpleNamespace(Anthropic=lambda: types.SimpleNamespace(messages=Msgs()))
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    g = ClaudeGrader("c", "model-x", max_tokens=77)  # builds its client from the SDK
    assert g.name == "c" and g.model == "model-x" and g.max_tokens == 77
    assert g.grade(Item("i", {"a": 1}), "RUB") is True
    assert calls[0]["model"] == "model-x" and calls[0]["max_tokens"] == 77
    assert "RUB" in calls[0]["messages"][0]["content"]
    assert ClaudeGrader("d", "m", client=object()).max_tokens == 200


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    exe = bin_ / "claude"
    exe.write_text("#!/bin/sh\n" + 'case "$*" in *FAILME*) echo boom >&2; exit 3;; *SLOW*) sleep 5;; esac\n'
                   + 'printf \'%s\\n\' "$@" > "$(dirname "$0")/argv.txt"\necho \'{"flag": false, "why": "ok"}\'\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    return bin_


def test_claude_code_grader_real_subprocess(fake_claude):
    g = ClaudeCodeGrader("cc", model="m1", timeout_s=3)
    assert g.name == "cc" and g.timeout_s == 3
    assert g.grade(Item("i", {"x": "ok"}), "RUBRIC-TEXT") is False
    argv = (fake_claude / "argv.txt").read_text()
    assert argv.startswith("-p\n") and "RUBRIC-TEXT" in argv and argv.rstrip().endswith("--model\nm1")
    with pytest.raises(GraderError, match="exited 3.*boom"):
        g.grade(Item("i", {"x": "FAILME"}), "r")
    import subprocess

    with pytest.raises(subprocess.TimeoutExpired):
        ClaudeCodeGrader("slow", timeout_s=1).grade(Item("i", {"x": "SLOW"}), "r")
    assert ClaudeCodeGrader("d").timeout_s == 120


@pytest.mark.parametrize("reply,flag", [
    ('```json\n{"flag": true}\n```', True),
    ('```\n{"flag": false}\n```', False),
    ('{"flag": true, "why": "a } brace"}', True),
    ('{"why": {"nested": 1}, "flag": false}', False),
    ('Answer: {"flag": true}.', True),
])
def test_parse_flag_shapes(reply, flag):
    assert parse_flag(reply) is flag


@pytest.mark.parametrize("reply,msg", [("no braces", "no JSON object"), ("} {", "no JSON object"), ("{nope}", "unparseable"),
                                       ('["flag", true]'.replace("[", "{").replace("]", "}"), "unparseable"), ('{"flag": 1}', "no boolean")])
def test_parse_flag_messages(reply, msg):
    with pytest.raises(GraderError, match=msg):
        parse_flag(reply)


def test_parse_flag_nonboolean_inside():
    with pytest.raises(GraderError, match="no boolean"):
        parse_flag('{"flag": [true]}')


# ---- agreement -----------------------------------------------------------------------------------------

def test_counts_above_one():
    key = {f"p{n}": True for n in range(5)} | {f"q{n}": False for n in range(7)}
    judged = {**{f"p{n}": n < 3 for n in range(5)}, **{f"q{n}": n < 2 for n in range(7)}}
    r = agreement(judged, key, boot=0)
    assert (r.tp, r.fn, r.fp, r.tn) == (3, 2, 2, 5)
    assert (r.miss.k, r.miss.n) == (2, 5) and (r.precision.k, r.precision.n) == (3, 5)
    import math

    assert len(r.kappa_ci) == 2 and all(math.isnan(x) for x in r.kappa_ci)  # no bootstrap, no interval


def test_kappa_single_class_agreement():
    assert kappa(5, 0, 0, 0) == 1.0 and kappa(0, 0, 0, 5) == 1.0


def test_percentile_and_bootstrap_bounds():
    xs = [float(i) for i in range(101)]
    assert percentile(xs, 0.025) == 2.0 and percentile(xs, 0.975) == 98.0
    assert percentile([1.0, 2.0], 0.025) == 1.0 and percentile([1.0, 2.0], 0.975) == 2.0
    with pytest.raises(ValueError, match="no values"):
        percentile([], 0.5)
    key = {f"i{n}": n % 2 == 0 for n in range(60)}
    judged = {k: (not v) if k in {"i1", "i2", "i3", "i10", "i11"} else v for k, v in key.items()}
    a, b = agreement(judged, key, boot=400, seed=3), agreement(judged, key, boot=400, seed=3)
    assert a.kappa_ci == b.kappa_ci and a.kappa_ci[0] < a.kappa < a.kappa_ci[1]
    c = agreement(judged, key, boot=400, seed=4)
    assert c.kappa_ci != a.kappa_ci
    one = agreement(judged, key, boot=1, seed=3)
    assert one.kappa_ci[0] == one.kappa_ci[1]  # one resample still gives an interval
    d = agreement(judged, key)  # default seed 0 and 2000 resamples
    assert d.kappa_ci == agreement(judged, key, boot=2000, seed=0).kappa_ci


# ---- splits --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pos,neg,frac,held_pos,held_neg", [
    (2, 2, 0.1, 1, 1), (3, 3, 0.1, 1, 1), (3, 3, 0.9, 2, 2), (10, 10, 0.4, 4, 4), (1, 5, 0.5, 0, 2),  # a one-item stratum stays in tune
])
def test_split_bounds(pos, neg, frac, held_pos, held_neg):
    labels = {f"p{n}": True for n in range(pos)} | {f"n{n}": False for n in range(neg)}
    s = split(labels, frac, seed=0)
    assert sum(labels[i] for i in s.heldout) == held_pos
    assert sum(not labels[i] for i in s.heldout) == held_neg
    assert s.seed == 0 and split(labels, frac, seed=9).seed == 9


# ---- vault records ----------------------------------------------------------------------------------

def _sealed(tmp_path):
    v = Vault(tmp_path / "deep" / "er")  # parents are created
    v.seal(split(LABELS, 0.4, 1), LABELS)
    return v


def test_config_hash_is_canonical():
    assert config_hash({"b": 1, "a": [1, 2]}) == config_hash({"a": [1, 2], "b": 1})
    import hashlib

    assert config_hash({"a": 1, "b": "x"}) == hashlib.sha256(b'{"a":1,"b":"x"}').hexdigest()


def test_ledger_fields(tmp_path):
    v = _sealed(tmp_path)
    sp = split(LABELS, 0.4, 1)
    v.record_tune(CFG, "n1")
    v.record_tune({**CFG, "prompt": "q"}, "n2")
    v.freeze(CFG)
    v.open(CFG)
    ev = v.events()
    seal, t1, t2, fr, op = ev
    assert seal["manifest_hash"] == sp.manifest_hash() and seal["heldout"] == len(sp.heldout) and seal["tune"] == len(sp.tune)
    assert (t1["round"], t2["round"]) == (1, 2) and t1["gen"] == 1 and t1["config_hash"] == config_hash(CFG)
    assert t2["config_hash"] == config_hash({**CFG, "prompt": "q"})
    assert fr["config_hash"] == config_hash(CFG) and fr["manifest_hash"] == sp.manifest_hash()
    assert op["config_hash"] == config_hash(CFG)
    raw = (v.root / "ledger.jsonl").read_text().splitlines()
    assert all(list(json.loads(l)) == sorted(json.loads(l)) for l in raw)  # keys written sorted
    d = v.root / "gen-001"
    assert json.loads((d / "tune-1.json").read_text()) == {"config": CFG, "note": "n1"}
    assert json.loads((d / "tune-2.json").read_text())["note"] == "n2"
    fz = json.loads((d / "FREEZE.json").read_text())
    assert list(fz) == sorted(fz) and fz["config"] == CFG
    marker = json.loads((d / "OPENED").read_text())
    assert marker["config_hash"] == config_hash(CFG) and marker["pid"] == os.getpid()


def test_reuse_check_spans_all_generations(tmp_path):
    v = Vault(tmp_path)
    sets = [{f"g{g}-{n}": n % 2 == 0 for n in range(20)} for g in range(3)]
    first = split(sets[0], 0.4, 1)
    v.seal(first, sets[0])
    v.freeze(CFG); v.open(CFG)
    v.seal(split(sets[1], 0.4, 1), sets[1])
    v.freeze(CFG); v.open(CFG)
    mixed = {**sets[2], **{i: sets[0][i] for i in first.heldout}}
    with pytest.raises(VaultError, match="earlier held-out set"):
        v.seal(split(mixed, 0.9, 1), mixed)


def test_seal_messages(tmp_path):
    from judge_kit.splits import Split

    v = Vault(tmp_path)
    with pytest.raises(VaultError, match="overlap"):
        v.seal(Split(tune=("a",), heldout=("a",), seed=0), {"a": True})
    with pytest.raises(VaultError, match="empty"):
        v.seal(Split(tune=("a",), heldout=(), seed=0), {"a": True})


def test_open_checks_manifest(tmp_path):
    v = _sealed(tmp_path)
    v.freeze(CFG)
    p = v.root / "gen-001" / "heldout.json"
    body = json.loads(p.read_text())
    body["ids"] = body["ids"][:-1]
    p.write_text(json.dumps(body))
    with pytest.raises(VaultError, match="does not match its manifest hash"):
        v.open(CFG)


def test_repair_marks_recovered_and_cleans_tmp(tmp_path):
    v = _sealed(tmp_path)
    v.freeze(CFG)
    (v.root / "gen-001" / "OPENED").write_text("{}")
    (v.root / "gen-001" / ".FREEZE.json.1.1.tmp").write_text("x")
    v.repair()
    assert v.events()[-1] == {**v.events()[-1], "event": "open", "recovered": True}
    assert not list((v.root / "gen-001").glob(".*.tmp"))
    assert v.repair() == []


def _problems(v):
    return "\n".join(v.verify())


def test_verify_every_problem(tmp_path):
    v = _sealed(tmp_path)
    v.freeze(CFG)
    v.open(CFG)
    led = v.root / "ledger.jsonl"
    lines = led.read_text().splitlines()
    led.write_text("\n".join(lines) + "\n{torn\n")
    assert "torn line" in _problems(v)
    led.write_text("\n".join(l for l in lines if '"seal"' not in l) + "\n")
    assert "gen 1: seal unlogged" in _problems(v)
    led.write_text("\n".join(l for l in lines if '"freeze"' not in l) + "\n")
    assert "gen 1: freeze unlogged" in _problems(v)
    led.write_text("\n".join(lines + [lines[-1]]) + "\n")
    assert "gen 1: opened 2 times" in _problems(v)
    led.write_text("\n".join(lines + [lines[1]]) + "\n")
    assert "gen 1: frozen 2 times" in _problems(v)
    led.write_text("\n".join(lines) + "\n")
    assert v.verify() == []
    d = v.root / "gen-001"
    (d / "OPENED").unlink()
    assert "ledger says opened but no OPENED marker" in _problems(v)
    (d / "OPENED").write_text("{}")
    fz = json.loads((d / "FREEZE.json").read_text())
    (d / "FREEZE.json").unlink()
    assert "gen 1: opened without a freeze" in _problems(v)
    fz["manifest_hash"] = "0" * 64
    (d / "FREEZE.json").write_text(json.dumps(fz))
    assert "freeze names a different held-out set" in _problems(v)


def test_verify_ignores_other_generations(tmp_path):
    v = _sealed(tmp_path)
    v.freeze(CFG)
    v.open(CFG)
    fresh = {f"j{n}": n % 2 == 0 for n in range(20)}
    v.seal(split(fresh, 0.4, 2), fresh)
    v.freeze(CFG)
    v.open(CFG)
    assert v.verify() == []  # one open and one freeze per generation, two generations


def test_events_torn_line_is_reported(tmp_path):
    v = _sealed(tmp_path)
    with open(v.root / "ledger.jsonl", "a") as f:
        f.write("not json\n\n")
    assert v.events()[-1] == {"event": "torn-line"}


def test_files_are_not_executable(tmp_path):
    v = _sealed(tmp_path)
    v.record_tune(CFG)
    v.freeze(CFG)
    v.open(CFG)
    um = os.umask(0); os.umask(um)
    for p in [*v.root.glob("gen-001/*"), v.root / "ledger.jsonl", v.root / ".lock"]:
        assert stat.S_IMODE(p.stat().st_mode) == 0o644 & ~um, p


def test_split_is_pinned():
    # A split must be reproducible across versions of the kit, not only within one run.
    labels = {f"i{n:02d}": n % 3 == 0 for n in range(12)}
    s = split(labels, 0.5, seed=42)
    assert s.heldout == ('i03', 'i04', 'i06', 'i07', 'i08', 'i11')


def test_simulated_grader_hard_without_extra_adds_nothing():
    items = [Item(f"i{n}", {}) for n in range(500)]
    g = SimulatedGrader("h", lambda i: False, 0.0, 0.0, hard=lambda i: True)
    assert not any(g.grade(i, "") for i in items)
