"""The tests are shown to go red: each planted defect below must make the suite fail.

Run from the repo root: `python scripts/red_check.py`. One line per defect; exit 1 if any stays green.
Some guards are layered on purpose (the SPENT check and the O_EXCL open marker both stop a second
open), so a defect that removes only one layer is not a defect; those breaks remove every layer.
"""

import os
import pathlib
import shutil
import subprocess
import sys

DEFECTS = [
    ("consensus by majority, not unanimity", [
        ("src/judge_kit/consensus.py", "        if all(calls):", "        if sum(calls) >= 2:")]),
    ("open without a freeze", [
        ("src/judge_kit/vault.py", "            if g.state == SEALED:\n                raise VaultError(f\"generation {g.n} is not frozen",
         "            if False:\n                raise VaultError(f\"generation {g.n} is not frozen")]),
    ("open with a config that was not frozen", [
        ("src/judge_kit/vault.py", "            if ch != g.config_hash or any(h is not None and h != ch for h in logged):", "            if False:")]),
    ("a set can be opened twice (both once-guards removed)", [
        ("src/judge_kit/vault.py", "            if g.state == SPENT:", "            if False:"),
        ("src/judge_kit/vault.py", "mfd = os.open(d / \"OPENED\", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)", "mfd = os.open(d / \"OPENED\", os.O_WRONLY | os.O_CREAT, 0o644)")]),
    ("SPENT not derived from the open marker (a crashed open would not spend the set)", [
        ("src/judge_kit/vault.py", "        if (d / \"OPENED\").exists() or self._logged(n, \"open\"):\n            state = SPENT", "        if self._logged(n, \"open\"):\n            state = SPENT")]),
    ("no tuning cap", [
        ("src/judge_kit/vault.py", "            if g.tune_rounds >= g.max_tune_rounds:", "            if False:")]),
    ("tuning allowed after the freeze", [
        ("src/judge_kit/vault.py", "            if g.state != SEALED:\n                raise VaultError(f\"generation {g.n} is {g.state}: tuning",
         "            if False:\n                raise VaultError(f\"generation {g.n} is {g.state}: tuning")]),
    ("held-out ids reused in a replacement set", [
        ("src/judge_kit/vault.py", "            if reused:", "            if False:")]),
    ("freeze is not exactly-once (link replaced by rename)", [
        ("src/judge_kit/vault.py", "        os.link(tmp, path)  # fails if path exists: exactly one writer wins", "        os.replace(tmp, path); return")]),
    ("verify ignores a forged freeze", [
        ("src/judge_kit/vault.py", "                if config_hash(fr[\"config\"]) != fr[\"config_hash\"]:", "                if False:")]),
    ("a grader reply with a non-boolean flag is accepted", [
        ("src/judge_kit/graders.py", "    if not isinstance(flag, bool):", "    if flag is None:")]),
    ("verify ignores a FREEZE.json that differs from the ledger's freeze", [
        ("src/judge_kit/vault.py", "                elif freezes[0].get(\"config_hash\") != fr[\"config_hash\"]:", "                elif False:")]),
    ("deleting FREEZE.json goes unnoticed", [
        ("src/judge_kit/vault.py", "            elif freezes:\n                problems.append(f\"gen {n}: the ledger says frozen", "            elif False:\n                problems.append(f\"gen {n}: the ledger says frozen")]),
    ("held-out labels not covered by a hash", [
        ("src/judge_kit/vault.py", "            if content_hash(held[\"labels\"]) != held.get(\"content_hash\") or sorted(held[\"labels\"]) != sorted(held[\"ids\"]):", "            if False:")]),
    ("deleted tune rounds are not counted", [
        ("src/judge_kit/vault.py", "        rounds = max(max(files, default=0), len(files), len(self._logged(n, \"tune\")))", "        rounds = len(files)")]),
    ("a torn ledger tail swallows the next event", [
        ("src/judge_kit/vault.py", "                line = \"\\n\" + line  # a torn last line must not swallow this event", "                pass")]),
    ("a grader exception aborts the key build", [
        ("src/judge_kit/consensus.py", "            except Exception:  # a timeout or an API error is an errored vote, not a lost run", "            except ValueError:")]),
    ("graders share one mutable payload", [
        ("src/judge_kit/consensus.py", "payload=copy.deepcopy(item.payload))", "payload=item.payload)")]),
    ("the claude -p grader runs with tools and project settings", [
        ("src/judge_kit/graders.py", "    LOCKDOWN = [\"--tools\", \"\", \"--strict-mcp-config\", \"--setting-sources\", \"\", \"--no-session-persistence\"]", "    LOCKDOWN = []")]),
    ("open on an incomplete judged file spends the set", [
        ("src/judge_kit/cli.py", "            if uncalled:  # refuse BEFORE opening", "            if False:  # refuse BEFORE opening")]),
    ("repair does not log a tune round a crash left unlogged", [
        ("src/judge_kit/vault.py", "                    if r not in logged_rounds:", "                    if False:")]),
    ("verify stays silent about steps rebuilt from files", [
        ("src/judge_kit/vault.py", "            if e.get(\"recovered\"):", "            if False:")]),
    ("verify ignores a generation whose files were deleted", [
        ("src/judge_kit/vault.py", "        for n in sorted(self._ledger_gens() - set(self._gens())):", "        for n in []:")]),
    ("the grader prompt leaks the item id", [
        ("src/judge_kit/graders.py", "f\"POLICY:\\n{rubric}\\n\\nTOOL CALL:\\n{json.dumps(dict(item.payload), sort_keys=True)}",
         "f\"POLICY:\\n{rubric}\\n\\nTOOL CALL {item.id}:\\n{json.dumps(dict(item.payload), sort_keys=True)}")]),
    ("false alarms counted as hits", [
        ("src/judge_kit/agreement.py", "        elif judged and not keyed:\n            fp += 1", "        elif judged and not keyed:\n            tp += 1")]),
    ("Wilson interval uses the normal approximation", [
        ("src/judge_kit/agreement.py", "    centre = (p + z * z / (2 * n)) / denom", "    centre = p; denom = 1")]),
    ("tune and held-out sets overlap", [
        ("src/judge_kit/splits.py", "        tune.extend(stratum[k:])", "        tune.extend(stratum[max(k - 1, 0):])")]),
]


def suite_passes() -> bool:
    for p in pathlib.Path("src").rglob("__pycache__"):
        shutil.rmtree(p)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "JK_CHAOS_TRIALS": "3"}
    r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], env=env, capture_output=True)
    return r.returncode == 0


def main() -> int:
    if not suite_passes():
        print("the unbroken tree is not green; fix that first")
        return 1
    bad = 0
    for why, edits in DEFECTS:
        saved = {}
        try:
            for f, old, new in edits:
                p = pathlib.Path(f)
                s = saved.setdefault(f, p.read_text()) if f not in saved else p.read_text()
                if s.count(old) != 1:
                    raise SystemExit(f"ANCHOR MISSING ({why}) in {f}: {old!r}")
                p.write_text(s.replace(old, new))
            green = suite_passes()
        finally:
            for f, s in saved.items():
                pathlib.Path(f).write_text(s)
        print(("STAYED GREEN: " if green else "red: ") + why)
        bad += green
    ok = suite_passes()
    print("restored tree: " + ("green" if ok else "NOT GREEN"))
    print(f"{len(DEFECTS) - bad}/{len(DEFECTS)} planted defects turned the suite red")
    return 0 if (bad == 0 and ok) else 1


if __name__ == "__main__":
    sys.exit(main())
