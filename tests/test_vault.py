import json
import os

import pytest

from judge_kit.splits import split
from judge_kit.vault import FROZEN, SEALED, SPENT, Vault, VaultError, config_hash

LABELS = {f"i{n:03d}": n % 3 == 0 for n in range(60)}
CFG = {"model": "m", "prompt": "p", "temperature": 0}


def sealed(tmp_path, **kw):
    v = Vault(tmp_path, **kw)
    v.seal(split(LABELS, 0.4, seed=1), LABELS)
    return v


def test_happy_path(tmp_path):
    v = sealed(tmp_path)
    assert v.current().state == SEALED
    assert set(v.tune_set()) == set(split(LABELS, 0.4, 1).tune)
    v.record_tune(CFG, "r1")
    assert v.freeze(CFG) == config_hash(CFG)
    assert v.current().state == FROZEN
    held = v.open(CFG)
    assert set(held) == set(split(LABELS, 0.4, 1).heldout)
    assert all(held[i] == LABELS[i] for i in held)
    assert v.current().state == SPENT
    assert v.verify() == []
    assert [e["event"] for e in v.events()] == ["seal", "tune", "freeze", "open"]


def test_no_open_before_freeze(tmp_path):
    v = sealed(tmp_path)
    with pytest.raises(VaultError, match="not frozen"):
        v.open(CFG)


def test_open_once(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    v.open(CFG)
    with pytest.raises(VaultError, match="spent"):
        v.open(CFG)
    with pytest.raises(VaultError, match="spent"):
        Vault(tmp_path).open(CFG)  # a fresh handle sees the same state


def test_open_must_name_frozen_config(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    with pytest.raises(VaultError, match="not the frozen config"):
        v.open({**CFG, "temperature": 1})
    assert v.current().state == FROZEN  # a refused open does not spend the set


def test_freeze_once(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    with pytest.raises(VaultError, match="already frozen"):
        v.freeze({**CFG, "prompt": "q"})


def test_tune_cap_and_closed_after_freeze(tmp_path):
    v = sealed(tmp_path, max_tune_rounds=2)
    v.record_tune(CFG)
    v.record_tune(CFG)
    with pytest.raises(VaultError, match="cap"):
        v.record_tune(CFG)
    v2 = sealed(tmp_path / "b")
    v2.freeze(CFG)
    with pytest.raises(VaultError, match="tuning is closed"):
        v2.record_tune(CFG)


def test_replace_requires_spent_and_fresh_ids(tmp_path):
    v = sealed(tmp_path)
    with pytest.raises(VaultError, match="not spent"):
        v.seal(split(LABELS, 0.4, seed=2), LABELS)
    v.freeze(CFG)
    v.open(CFG)
    with pytest.raises(VaultError, match="earlier held-out set"):
        v.seal(split(LABELS, 0.4, seed=2), LABELS)
    fresh = {f"j{n}": n % 2 == 0 for n in range(30)}
    g = v.seal(split(fresh, 0.4, seed=3), fresh)
    assert g == 2 and v.current().state == SEALED
    v.record_tune(CFG)  # tuning reopens on the new generation
    assert v.verify() == []


def test_seal_validates(tmp_path):
    v = Vault(tmp_path)
    with pytest.raises(VaultError, match="no key label"):
        v.seal(split(LABELS, 0.4, 1), {"i000": True})
    with pytest.raises(VaultError, match="nothing sealed"):
        v.current()


def test_verify_catches_tampering(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    held = tmp_path / "gen-001" / "heldout.json"
    body = json.loads(held.read_text())
    body["ids"] = body["ids"][1:]
    held.write_text(json.dumps(body))
    assert any("manifest hash" in p for p in v.verify())


def test_verify_catches_forged_freeze(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    fr = tmp_path / "gen-001" / "FREEZE.json"
    body = json.loads(fr.read_text())
    body["config"]["prompt"] = "swapped after the fact"
    fr.write_text(json.dumps(body))
    assert any("does not match its hash" in p for p in v.verify())


def test_crash_after_open_marker_spends_and_repairs(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    (tmp_path / "gen-001" / "OPENED").write_text("{}")  # the marker landed, the ledger line didn't
    assert v.current().state == SPENT
    assert any("open unlogged" in p for p in v.verify())
    assert v.repair() == ["gen 1: logged open"]
    assert v.verify() == ["gen 1: open recovered from its file after a crash; it could not be checked against the ledger"]
    with pytest.raises(VaultError, match="spent"):
        v.open(CFG)


def test_torn_seal_is_retried(tmp_path):
    v = Vault(tmp_path)
    d = tmp_path / "gen-001"
    d.mkdir()
    (d / "tune.json").write_text("{}")
    (d / ".heldout.json.1.2.tmp").write_text("partial")
    g = v.seal(split(LABELS, 0.4, 1), LABELS)
    assert g == 1 and v.verify() == []


def test_ledger_is_append_only_jsonl(tmp_path):
    v = sealed(tmp_path)
    v.freeze(CFG)
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2 and all(json.loads(l)["gen"] == 1 for l in lines)
