import json

from judge_kit.cli import main


def test_demo_runs_and_verifies(tmp_path, capsys):
    assert main(["demo", "--dir", str(tmp_path / "v"), "--n", "200"]) == 0
    out = capsys.readouterr().out
    assert "not ground truth" in out
    assert "a second open is refused" in out
    assert "a 4th round is refused" in out
    assert "verify: consistent" in out


def test_file_workflow(tmp_path, capsys):
    labels = {f"i{n}": n % 2 == 0 for n in range(40)}
    (tmp_path / "key.json").write_text(json.dumps(labels))
    (tmp_path / "cfg.json").write_text(json.dumps({"prompt": "x"}))
    (tmp_path / "judged.json").write_text(json.dumps({k: (not v) if k == "i0" else v for k, v in labels.items()}))
    d = str(tmp_path / "v")
    assert main(["seal", "--dir", d, "--labels", str(tmp_path / "key.json")]) == 0
    assert main(["open", "--dir", d, "--config", str(tmp_path / "cfg.json"), "--judged", str(tmp_path / "judged.json")]) == 2
    assert main(["tune", "--dir", d, "--config", str(tmp_path / "cfg.json")]) == 0
    assert main(["freeze", "--dir", d, "--config", str(tmp_path / "cfg.json")]) == 0
    assert main(["open", "--dir", d, "--config", str(tmp_path / "cfg.json"), "--judged", str(tmp_path / "judged.json")]) == 0
    out = capsys.readouterr().out
    assert "kappa" in out
    assert main(["open", "--dir", d, "--config", str(tmp_path / "cfg.json"), "--judged", str(tmp_path / "judged.json")]) == 2
    assert main(["verify", "--dir", d]) == 0
    assert main(["status", "--dir", d]) == 0
    assert '"state": "spent"' in capsys.readouterr().out
