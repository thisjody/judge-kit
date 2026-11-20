"""Chaos: kill -9 in the middle of freeze and open, and race concurrent opens.

Each trial runs the step in a child process and SIGKILLs it after a random delay. Afterwards the
vault must reconcile one-for-one: repair() leaves verify() clean, the generation is in exactly one
state, the freeze is whole or absent, and a set that may have been opened is counted as spent.
"""

import json
import multiprocessing as mp
import os
import random
import signal
import time

import pytest

from judge_kit.splits import split
from judge_kit.vault import FROZEN, SEALED, SPENT, Vault, VaultError

LABELS = {f"i{n:04d}": n % 3 == 0 for n in range(2000)}
CFG = {"model": "m", "prompt": "p" * 2000}
TRIALS = int(os.environ.get("JK_CHAOS_TRIALS", "40"))


def _freeze(root):
    Vault(root).freeze(CFG)


def _open(root):
    Vault(root).open(CFG)


def _kill_after(target, root, delay):
    p = mp.get_context("fork").Process(target=target, args=(root,))
    p.start()
    time.sleep(delay)
    if p.is_alive():
        os.kill(p.pid, signal.SIGKILL)
    p.join()
    return p.exitcode


def _check(root):
    v = Vault(root)
    v.repair()
    assert v.verify() == [], v.verify()
    assert not list((v.root / "gen-001").glob(".*.tmp"))
    return v.current().state


@pytest.mark.parametrize("trial", range(TRIALS))
def test_kill_mid_freeze(tmp_path, trial):
    rng = random.Random(trial)
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    _kill_after(_freeze, str(tmp_path), rng.uniform(0, 0.01))
    state = _check(tmp_path)
    assert state in (SEALED, FROZEN)
    fr = tmp_path / "gen-001" / "FREEZE.json"
    if fr.exists():
        json.loads(fr.read_text())  # whole, never torn
        Vault(tmp_path).open(CFG)
    else:
        Vault(tmp_path).freeze(CFG)  # a killed freeze can be redone
    assert _check(tmp_path) in (FROZEN, SPENT)


@pytest.mark.parametrize("trial", range(TRIALS))
def test_kill_mid_open(tmp_path, trial):
    rng = random.Random(1000 + trial)
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(CFG)
    _kill_after(_open, str(tmp_path), rng.uniform(0, 0.01))
    state = _check(tmp_path)
    assert state in (FROZEN, SPENT)
    if state == FROZEN:
        Vault(tmp_path).open(CFG)
    with pytest.raises(VaultError, match="spent"):
        Vault(tmp_path).open(CFG)
    ev = [e for e in Vault(tmp_path).events() if e["event"] == "open"]
    assert len(ev) == 1


def _racer(root, q):
    try:
        Vault(root).open(CFG)
        q.put("ok")
    except VaultError:
        q.put("refused")


@pytest.mark.parametrize("n", [2, 8, 16])
def test_concurrent_opens_exactly_one_wins(tmp_path, n):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(CFG)
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    ps = [ctx.Process(target=_racer, args=(str(tmp_path), q)) for _ in range(n)]
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    results = [q.get() for _ in range(n)]
    assert results.count("ok") == 1 and results.count("refused") == n - 1
    assert _check(tmp_path) == SPENT


# ---- every crash window, deterministically -------------------------------------------------------

def _in_child(fn, root, point):
    def run():
        os.environ["JUDGE_KIT_CRASH_AT"] = point
        fn(root)
    p = mp.get_context("fork").Process(target=run)
    p.start()
    p.join()
    return p.exitcode


@pytest.mark.parametrize("point", [
    "before-link:FREEZE.json", "after-link:FREEZE.json", "freeze-before-log",
])
def test_freeze_crash_windows(tmp_path, point):
    Vault(tmp_path).seal(split(LABELS, 0.4, 1), LABELS)
    assert _in_child(_freeze, str(tmp_path), point) == -signal.SIGKILL
    state = _check(tmp_path)
    assert state == (SEALED if point.startswith("before") else FROZEN)
    if state == SEALED:
        Vault(tmp_path).freeze(CFG)
    Vault(tmp_path).open(CFG)
    assert _check(tmp_path) == SPENT


@pytest.mark.parametrize("point", ["open-before-log", "open-after-log"])
def test_open_crash_windows(tmp_path, point):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(CFG)
    assert _in_child(_open, str(tmp_path), point) == -signal.SIGKILL
    assert _check(tmp_path) == SPENT  # the marker landed, so the set counts as seen
    with pytest.raises(VaultError, match="spent"):
        Vault(tmp_path).open(CFG)
    assert len([e for e in Vault(tmp_path).events() if e["event"] == "open"]) == 1


def _seal(root):
    Vault(root).seal(split(LABELS, 0.4, 1), LABELS)


@pytest.mark.parametrize("point", [
    "before-link:tune.json", "after-link:tune.json", "before-link:heldout.json", "after-link:heldout.json", "seal-before-log",
])
def test_seal_crash_windows(tmp_path, point):
    assert _in_child(_seal, str(tmp_path), point) == -signal.SIGKILL
    v = Vault(tmp_path)
    v.repair()
    committed = (tmp_path / "gen-001" / "heldout.json").exists()
    assert committed == (point in ("after-link:heldout.json", "seal-before-log"))
    if not committed:
        assert v.seal(split(LABELS, 0.4, 1), LABELS) == 1  # a torn seal is simply redone
    assert v.verify() == [] and v.current().state == SEALED
