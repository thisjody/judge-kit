"""Tests written from the second blind review (of the fix diff 8130d9a..d50af83), each failing before its fix."""

import json
import multiprocessing as mp
import os
import shutil
import signal

import pytest

from judge_kit.cli import main
from judge_kit.splits import Split, split
from judge_kit.vault import Vault, VaultError, config_hash

LABELS = {f"i{n:03d}": n % 3 == 0 for n in range(60)}
A, B = {"m": "A"}, {"m": "B"}


def sealed(root):
    v = Vault(root)
    v.seal(split(LABELS, 0.4, 1), LABELS)
    return v


# F1: a freeze recovered from the file after the ledger lost it is never silently clean
def test_recovered_freeze_is_always_reported(tmp_path):
    v = sealed(tmp_path)
    v.freeze(A)
    raw = v.ledger.read_bytes()
    v.ledger.write_bytes(raw[:-15])  # a torn tail, as a crash leaves
    fz = json.loads((tmp_path / "gen-001" / "FREEZE.json").read_text())
    fz["config"], fz["config_hash"] = B, config_hash(B)
    (tmp_path / "gen-001" / "FREEZE.json").write_text(json.dumps(fz))
    v.repair()
    notes = v.verify()
    assert any("recovered" in p and "freeze" in p for p in notes)


def test_verify_strict_flag_on_cli(tmp_path, capsys):
    v = sealed(tmp_path)
    v.freeze(A)
    v.ledger.write_bytes(v.ledger.read_bytes()[:-15])
    v.repair()
    assert main(["verify", "--dir", str(tmp_path)]) == 1
    assert "recovered" in capsys.readouterr().out


# F2: generations the ledger knows about but whose files are gone are reported, and block a reseal
def test_deleted_generation_is_caught(tmp_path):
    v = sealed(tmp_path)
    v.freeze(A)
    v.open(A)
    shutil.rmtree(tmp_path / "gen-001")
    assert any("gen 1" in p and "files are missing" in p for p in v.verify())
    with pytest.raises(VaultError, match="run verify"):
        v.seal(split(LABELS, 0.4, 1), LABELS)


def test_deleted_heldout_file_is_caught(tmp_path):
    v = sealed(tmp_path)
    (tmp_path / "gen-001" / "heldout.json").unlink()
    assert any("files are missing" in p for p in v.verify())


# F3: a crash between a tune file and its ledger line is repairable
def _tune(root):
    Vault(root).record_tune(A)


def test_tune_crash_is_repairable(tmp_path):
    sealed(tmp_path)

    def run():
        os.environ["JUDGE_KIT_CRASH_AT"] = "after-link:tune-1.json"
        _tune(str(tmp_path))

    p = mp.get_context("fork").Process(target=run)
    p.start()
    p.join()
    assert p.exitcode == -signal.SIGKILL
    v = Vault(tmp_path)
    assert v.verify()  # one tune file, no ledger line
    assert any("tune" in f for f in v.repair())
    assert [x for x in v.verify() if "recovered" not in x] == []
    assert v.current().tune_rounds == 1


# F4: a vault sealed by an older version is refused before anything is spent
def test_old_format_vault_refused_before_spending(tmp_path):
    v = sealed(tmp_path)
    v.freeze(A)
    for name in ("heldout.json", "tune.json"):
        p = tmp_path / "gen-001" / name
        body = json.loads(p.read_text())
        body.pop("content_hash", None)
        body.pop("max_tune_rounds", None)
        p.write_text(json.dumps(body))
    with pytest.raises(VaultError, match="older version"):
        v.open(A)
    assert not (tmp_path / "gen-001" / "OPENED").exists()


# F5: an existing directory that is not a vault is refused by read-only commands
def test_non_vault_dir_refused(tmp_path, capsys):
    (tmp_path / "x").mkdir()
    for cmd in ("verify", "status", "repair"):
        assert main([cmd, "--dir", str(tmp_path / "x")]) == 2
    assert not (tmp_path / "x" / ".lock").exists()


# F6: padded generation names are ignored, not crashed on
def test_padded_gen_name_ignored(tmp_path):
    v = sealed(tmp_path)
    (tmp_path / "gen-0007").mkdir()
    (tmp_path / "gen-0007" / "heldout.json").write_text("{}")
    assert v.verify() == [] and v.current().n == 1


# F7: a directory or unreadable path as an input file is a clean refusal
def test_directory_as_input_refuses(tmp_path, capsys):
    (tmp_path / "adir").mkdir()
    assert main(["seal", "--dir", str(tmp_path / "v"), "--labels", str(tmp_path / "adir")]) == 2
    assert "Traceback" not in capsys.readouterr().err


# nit: seal refuses non-bool labels itself
def test_seal_refuses_non_bool_labels(tmp_path):
    with pytest.raises(VaultError, match="true/false"):
        Vault(tmp_path).seal(Split(tune=("a",), heldout=("b",), seed=0), {"a": True, "b": "false"})
