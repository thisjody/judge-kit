import json
import math

import pytest

from judge_kit.agreement import agreement, kappa, wilson
from judge_kit.consensus import build_key, key_from_votes
from judge_kit.graders import ClaudeGrader, GraderError, Item, SimulatedGrader, parse_flag
from judge_kit.splits import manifest_hash, split


# ---- consensus --------------------------------------------------------------------------------

def test_unanimous_only():
    votes = {
        "a": {"g1": True, "g2": True, "g3": True},
        "b": {"g1": False, "g2": False, "g3": False},
        "c": {"g1": True, "g2": False, "g3": True},
        "d": {"g1": None, "g2": True, "g3": True},
    }
    k = key_from_votes(votes, ["g1", "g2", "g3"])
    assert k.labels == {"a": True, "b": False}
    assert k.dropped == ["c"]
    assert k.errored == ["d"]
    assert k.total == 4
    assert k.coverage == 0.5
    assert "not ground truth" in k.summary()


def test_key_needs_two_graders_and_all_votes():
    with pytest.raises(ValueError):
        key_from_votes({"a": {"g1": True}}, ["g1"])
    with pytest.raises(ValueError):
        key_from_votes({"a": {"g1": True}}, ["g1", "g2"])


def test_build_key_isolated_and_reproducible():
    items = [Item(id=f"i{n}", payload={"n": n}) for n in range(50)]
    truth = lambda it: it.payload["n"] % 3 == 0
    gs = [SimulatedGrader(f"g{n}", truth, 0.1, 0.1, seed=1) for n in range(3)]
    k1 = build_key(items, gs, "rubric", seed=5)
    k2 = build_key(list(reversed(items)), gs, "rubric", seed=99)
    assert k1.labels == k2.labels  # order-independent: graders are pure functions of the item
    assert 0 < len(k1.labels) < 50


def test_build_key_rejects_duplicates():
    items = [Item(id="x", payload={}), Item(id="x", payload={})]
    g = SimulatedGrader("g", lambda i: True, 0, 0)
    with pytest.raises(ValueError):
        build_key(items, [g, SimulatedGrader("h", lambda i: True, 0, 0)], "r")
    with pytest.raises(ValueError):
        build_key([Item(id="y", payload={})], [g, g], "r")


def test_grader_error_becomes_errored_not_a_label():
    class Bad:
        name = "bad"

        def grade(self, item, rubric):
            raise GraderError("no")

    items = [Item(id="a", payload={})]
    k = build_key(items, [Bad(), SimulatedGrader("g", lambda i: True, 0, 0)], "r")
    assert k.labels == {} and k.errored == ["a"]


def test_simulated_grader_rates_and_blind_spot():
    items = [Item(id=f"i{n}", payload={}) for n in range(4000)]
    g = SimulatedGrader("g", lambda i: False, false_alarm=0.1, miss=0.0, seed=3)
    rate = sum(g.grade(i, "") for i in items) / len(items)
    assert 0.08 < rate < 0.12
    blind = SimulatedGrader("b", lambda i: True, 0.0, 0.0, blind=lambda i: True)
    assert not any(blind.grade(i, "") for i in items[:10])
    hard = SimulatedGrader("h", lambda i: True, 0.0, 0.0, hard=lambda i: True, hard_extra=1.0)
    assert not any(hard.grade(i, "") for i in items[:10])
    with pytest.raises(ValueError):
        SimulatedGrader("x", lambda i: True, 1.5, 0)


# ---- the Claude adapter, against a fake with the SDK's shape -------------------------------------

class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, *, model, max_tokens, messages):
        self.calls.append({"model": model, "max_tokens": max_tokens, "messages": messages})
        return _Msg(self.replies.pop(0))


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)


def test_claude_grader_parses_and_sends_rubric_and_item_only():
    c = FakeClient(['{"flag": true, "why": "outside"}', '```json\n{"flag": false, "why": "ok"}\n```'])
    g = ClaudeGrader("claude", "some-model", client=c)
    it = Item(id="secret-id", payload={"tool": "file.write", "args": {"path": "/etc/x"}})
    assert g.grade(it, "POLICY TEXT") is True
    assert g.grade(it, "POLICY TEXT") is False
    sent = c.messages.calls[0]["messages"][0]["content"]
    assert "POLICY TEXT" in sent and "/etc/x" in sent
    assert "secret-id" not in sent  # the grader never sees ids or labels


@pytest.mark.parametrize("reply", ["yes", "{flag: true}", '{"flag": "true"}', '{"why": "x"}', "[]", ""])
def test_parse_flag_refuses_ambiguous(reply):
    with pytest.raises(GraderError):
        parse_flag(reply)


def test_parse_flag_accepts_prose_around_json():
    assert parse_flag('Here you go: {"flag": false, "why": "fine"}') is False


# ---- splits --------------------------------------------------------------------------------------

def test_split_stratified_disjoint_seeded():
    labels = {f"i{n}": n % 4 == 0 for n in range(100)}
    s = split(labels, 0.4, seed=1)
    assert set(s.tune).isdisjoint(s.heldout)
    assert set(s.tune) | set(s.heldout) == set(labels)
    pos_held = sum(labels[i] for i in s.heldout)
    assert pos_held == 10 and len(s.heldout) == 40
    assert split(labels, 0.4, seed=1) == s
    assert split(labels, 0.4, seed=2) != s
    assert s.manifest_hash() == manifest_hash(reversed(s.heldout))


def test_split_small_keeps_both_sides():
    s = split({"a": True, "b": True, "c": False, "d": False}, 0.1, seed=0)
    assert len(s.heldout) == 2 and len(s.tune) == 2
    with pytest.raises(ValueError):
        split({"a": True}, 0.0, 0)
    with pytest.raises(ValueError):
        split({"a": True}, 1.0, 0)


# ---- agreement -----------------------------------------------------------------------------------

def test_wilson_known_values():
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and abs(hi - 0.2775) < 1e-3
    lo, hi = wilson(5, 10)
    assert abs(lo - 0.2366) < 1e-3 and abs(hi - 0.7634) < 1e-3
    assert wilson(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        wilson(3, 2)


def test_kappa_known_values():
    assert kappa(10, 0, 0, 10) == 1.0
    assert abs(kappa(20, 5, 10, 15) - 0.4) < 1e-9
    assert kappa(0, 0, 0, 10) == 1.0
    assert kappa(0, 10, 0, 0) == 0.0
    assert math.isnan(kappa(0, 0, 0, 0))


def test_agreement_counts_and_missing():
    key = {"a": True, "b": True, "c": False, "d": False, "e": False}
    judged = {"a": True, "b": False, "c": True, "d": False}
    r = agreement(judged, key, boot=200, seed=0)
    assert (r.tp, r.fn, r.fp, r.tn) == (1, 1, 1, 1)
    assert r.missing == ["e"]
    assert r.false_alarm.k == 1 and r.false_alarm.n == 2
    assert r.recall.value == 0.5
    assert r.kappa_ci[0] <= r.kappa <= r.kappa_ci[1]
    assert any("not with truth" in line for line in r.lines())


def test_agreement_perfect_has_tight_ci():
    key = {f"i{n}": n % 2 == 0 for n in range(200)}
    r = agreement(dict(key), key, boot=300)
    assert r.kappa == 1.0 and r.kappa_ci == (1.0, 1.0)
    assert r.false_alarm.hi < 0.05


def test_agreement_empty():
    r = agreement({}, {}, boot=10)
    assert r.n == 0 and str(r.recall).startswith("n/a")


def test_claude_code_grader_fresh_call_per_item():
    calls = []

    def runner(argv):
        calls.append(argv)
        return '{"flag": true, "why": "x"}' if "/etc" in argv[2] else '{"flag": false, "why": "ok"}'

    from judge_kit.graders import ClaudeCodeGrader

    g = ClaudeCodeGrader("cc", model="m", runner=runner)
    assert g.grade(Item(id="a", payload={"path": "/etc/x"}), "P") is True
    assert g.grade(Item(id="b", payload={"path": "/workspace/x"}), "P") is False
    assert len(calls) == 2 and calls[0][:2] == ["claude", "-p"] and calls[0][-2:] == ["--model", "m"]
    assert "a" not in calls[0][2].split('"path"')[0][-3:]
    bad = ClaudeCodeGrader("cc2", runner=lambda argv: "I think it's fine")
    with pytest.raises(GraderError):
        bad.grade(Item(id="c", payload={}), "P")
