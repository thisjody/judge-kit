"""Stateful property test of the core invariant.

Hypothesis drives random sequences of vault operations, including reopening the vault from disk
and repair, against a simple model. Invariants after every step:
  - held-out labels are returned only by an open that follows a freeze of the same config;
  - each generation is opened at most once and frozen at most once;
  - no tuning after the freeze, and never more than the cap;
  - a replacement set shares no held-out ids with any earlier set;
  - verify() reports nothing.
"""

import tempfile

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from judge_kit.splits import split
from judge_kit.vault import FROZEN, SEALED, SPENT, Vault, VaultError

CONFIGS = [{"p": "a"}, {"p": "b"}, {"p": "c", "t": 0}]
CAP = 2


class VaultMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.dir = tempfile.mkdtemp(prefix="jk-sm-")
        self.v = Vault(self.dir, max_tune_rounds=CAP)
        self.gen = 0
        self.state = None
        self.frozen = None
        self.tunes = 0
        self.opens = {}
        self.used_held = set()
        self.batch = 0

    def _labels(self):
        self.batch += 1
        return {f"b{self.batch}-{n}": n % 3 == 0 for n in range(12)}

    @rule(seed=st.integers(0, 5))
    def seal(self, seed):
        labels = self._labels()
        sp = split(labels, 0.4, seed)
        try:
            g = self.v.seal(sp, labels)
        except VaultError:
            assert self.state in (SEALED, FROZEN)
            return
        assert self.state in (None, SPENT)
        assert not (set(sp.heldout) & self.used_held)
        self.used_held |= set(sp.heldout)
        self.gen, self.state, self.frozen, self.tunes = g, SEALED, None, 0

    @rule(c=st.sampled_from(CONFIGS))
    def reseal_old_ids(self, c):
        # try to reuse an earlier held-out id in a new set: always refused when anything is spent
        if not self.used_held or self.state != SPENT:
            return
        old = sorted(self.used_held)[0]
        labels = {old: True, "fresh-x": False, "fresh-y": True, "fresh-z": False}
        sp = split(labels, 0.5, 0)
        if old in sp.heldout:
            try:
                self.v.seal(sp, labels)
                raise AssertionError("reused held-out id was accepted")
            except VaultError:
                pass

    @rule(c=st.sampled_from(CONFIGS))
    def tune(self, c):
        try:
            self.v.record_tune(c)
        except VaultError:
            assert self.state != SEALED or self.tunes >= CAP or self.state is None
            return
        assert self.state == SEALED and self.tunes < CAP
        self.tunes += 1

    @rule(c=st.sampled_from(CONFIGS))
    def freeze(self, c):
        try:
            self.v.freeze(c)
        except VaultError:
            assert self.state != SEALED
            return
        assert self.state == SEALED
        self.state, self.frozen = FROZEN, c

    @rule(c=st.sampled_from(CONFIGS))
    def open(self, c):
        try:
            held = self.v.open(c)
        except VaultError:
            assert not (self.state == FROZEN and c == self.frozen)
            return
        assert self.state == FROZEN and c == self.frozen
        assert self.opens.get(self.gen, 0) == 0
        assert held and all(isinstance(x, bool) for x in held.values())
        self.opens[self.gen] = 1
        self.state = SPENT

    @rule()
    def reopen_from_disk(self):
        self.v = Vault(self.dir, max_tune_rounds=CAP)

    @rule()
    def repair(self):
        assert self.v.repair() == []  # no crashes here, so nothing to repair

    @invariant()
    def consistent(self):
        assert self.v.verify() == []
        if self.state is not None:
            g = self.v.current()
            assert g.n == self.gen and g.state == self.state and g.tune_rounds == self.tunes


VaultMachine.TestCase.settings = settings(max_examples=int(__import__("os").environ.get("JK_SM_EXAMPLES", "300")), stateful_step_count=30, deadline=None)
TestVaultMachine = VaultMachine.TestCase
