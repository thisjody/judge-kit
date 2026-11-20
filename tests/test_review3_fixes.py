"""Tests written from the third blind review (of d50af83..1303ce5), each failing before its fix."""

import json
import shutil

import pytest

from judge_kit.cli import main
from judge_kit.splits import split
from judge_kit.vault import SPENT, Vault, VaultError

LABELS = {f"i{n:03d}": n % 3 == 0 for n in range(60)}
FRESH = {f"j{n:03d}": n % 2 == 0 for n in range(40)}
A = {"m": "A"}


def old_format(root, frozen):
    v = Vault(root)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    if frozen:
        v.freeze(A)
    for name in ("heldout.json", "tune.json"):
        p = root / "gen-001" / name
        body = json.loads(p.read_text())
        body.pop("content_hash", None)
        body.pop("max_tune_rounds", None)
        p.write_text(json.dumps(body))
    return v


# 1. (revised after the fourth review) an older-format vault is refused with a way out that needs no ledger
#    trust: start a new vault directory. A forged "retire" line in the ledger has no effect.
@pytest.mark.parametrize("frozen", [False, True])
def test_old_format_vault_points_to_a_new_directory(tmp_path, frozen):
    v = old_format(tmp_path, frozen)
    with pytest.raises(VaultError, match="new vault directory"):
        v.open(A) if frozen else v.record_tune(A)
    with pytest.raises(VaultError, match="new vault directory"):
        v.seal(split(FRESH, 0.4, 2), FRESH)
    assert not any(e["event"] == "retire" for e in v.events())


def test_forged_retire_line_has_no_effect(tmp_path):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(A)
    with open(v.ledger, "a") as f:
        f.write('{"event": "retire", "gen": 1, "t": 0}\n')
    assert v.current().state == "frozen"
    with pytest.raises(VaultError, match="not spent"):
        v.seal(split(FRESH, 0.4, 2), FRESH)
    assert any("unknown ledger event" in p for p in v.verify())


# 2. a lost generation: the refusal says what actually unblocks it
def test_lost_generation_message(tmp_path):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(A); v.open(A)
    shutil.rmtree(tmp_path / "gen-001")
    with pytest.raises(VaultError, match="restore"):
        v.seal(split(FRESH, 0.4, 2), FRESH)


# 3. padded tune file names are not rounds
def test_padded_tune_file_ignored(tmp_path, capsys):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    (tmp_path / "gen-001" / "tune-01.json").write_text("{}")
    assert main(["repair", "--dir", str(tmp_path)]) == 0
    assert v.current().tune_rounds == 0


# 4. a regular file given as the vault dir is a clean refusal
def test_file_as_vault_dir(tmp_path, capsys):
    (tmp_path / "f").write_text("x")
    (tmp_path / "key.json").write_text(json.dumps(LABELS))
    assert main(["seal", "--dir", str(tmp_path / "f"), "--labels", str(tmp_path / "key.json")]) == 2
    assert "Traceback" not in capsys.readouterr().err


# 5. verify can accept recovered notes for CI, but never other problems
def test_verify_allow_recovered(tmp_path, capsys):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(A)
    v.ledger.write_text("\n".join(l for l in v.ledger.read_text().splitlines() if '"freeze"' not in l) + "\n")
    v.repair()
    assert main(["verify", "--dir", str(tmp_path)]) == 1
    assert main(["verify", "--dir", str(tmp_path), "--allow-recovered"]) == 0
    (tmp_path / "gen-001" / "FREEZE.json").unlink()
    assert main(["verify", "--dir", str(tmp_path), "--allow-recovered"]) == 1


def test_allow_recovered_never_passes_a_lost_ledger(tmp_path):
    v = Vault(tmp_path)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    v.freeze(A)
    v.open(A)
    v.ledger.unlink()
    v.repair()
    assert main(["verify", "--dir", str(tmp_path), "--allow-recovered"]) == 1
