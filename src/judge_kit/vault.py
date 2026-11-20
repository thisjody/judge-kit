"""The vault: seal a held-out set, freeze the judge, open the set once, replace a spent set.

The rules it enforces:
  1. The held-out labels are not handed out before the judge is frozen.
  2. A freeze records the judge's config hash and the held-out manifest hash, and happens once per set.
  3. A set opens once. The open must name the frozen config. After that the set is SPENT.
  4. Tuning is capped at `max_tune_rounds` and allowed only before the freeze.
  5. A spent set can't be reused. Further tuning needs a fresh set: `seal` a new generation.

The files on disk are the truth, and each step's file is created atomically and exactly once
(write to a temp file, fsync, then link or O_EXCL). The ledger is an append-only log of the same
events, for people to read and for `verify` to check.

A crash part-way through `open` still spends the set. That is deliberate: the set may have been
read, and a held-out set that may have been seen is no longer held out.

What this does not do: it can't stop someone reading heldout.json with `cat`. It is a discipline tool,
not a lock against its owner. What it does is make the honest path the easy one and leave a record
that `verify` checks.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from judge_kit.splits import Split, manifest_hash

SEALED, FROZEN, SPENT = "sealed", "frozen", "spent"


class VaultError(RuntimeError):
    pass


def _crash_point(name: str) -> None:
    """Chaos-test hook: SIGKILL this process at a named point when JUDGE_KIT_CRASH_AT names it."""
    if os.environ.get("JUDGE_KIT_CRASH_AT") == name:
        os.kill(os.getpid(), 9)


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_once(path: Path, data: bytes) -> None:
    """Create `path` with `data`, atomically, or raise FileExistsError if it already exists."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    _crash_point(f"before-link:{path.name}")
    try:
        os.link(tmp, path)  # fails if path exists: exactly one writer wins
    finally:
        _crash_point(f"after-link:{path.name}")
        os.unlink(tmp)
    _fsync_dir(path.parent)


@dataclass
class Generation:
    n: int
    state: str
    manifest_hash: str
    config_hash: str | None
    tune_rounds: int


class Vault:
    def __init__(self, root: str | os.PathLike, max_tune_rounds: int = 3):
        self.root = Path(root)
        self.max_tune_rounds = max_tune_rounds
        self.root.mkdir(parents=True, exist_ok=True)
        self.ledger = self.root / "ledger.jsonl"

    # -- internals -------------------------------------------------------------------------

    def _gen_dir(self, n: int) -> Path:
        return self.root / f"gen-{n:03d}"

    def _gens(self) -> list[int]:
        out = []
        for p in self.root.glob("gen-*"):
            if (p / "heldout.json").exists():
                out.append(int(p.name.split("-")[1]))
        return sorted(out)

    def _lock(self):
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _unlock(self, fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    def _log(self, event: str, gen: int, **fields: Any) -> None:
        line = json.dumps({"t": time.time(), "event": event, "gen": gen, **fields}, sort_keys=True) + "\n"
        fd = os.open(self.ledger, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_json(self, p: Path) -> dict:
        return json.loads(p.read_text())

    def _tune_rounds(self, n: int) -> int:
        d = self._gen_dir(n)
        return len(list(d.glob("tune-*.json")))

    def generation(self, n: int) -> Generation:
        d = self._gen_dir(n)
        held = self._read_json(d / "heldout.json")
        if (d / "OPENED").exists():
            state = SPENT
        elif (d / "FREEZE.json").exists():
            state = FROZEN
        else:
            state = SEALED
        ch = self._read_json(d / "FREEZE.json")["config_hash"] if (d / "FREEZE.json").exists() else None
        return Generation(n=n, state=state, manifest_hash=held["manifest_hash"], config_hash=ch, tune_rounds=self._tune_rounds(n))

    def current(self) -> Generation:
        gens = self._gens()
        if not gens:
            raise VaultError("nothing sealed yet: run seal first")
        return self.generation(gens[-1])

    # -- the steps -------------------------------------------------------------------------

    def seal(self, sp: Split, labels: Mapping[str, bool]) -> int:
        """Seal a held-out set as a new generation. The previous generation must be spent."""
        missing = [i for i in (*sp.heldout, *sp.tune) if i not in labels]
        if missing:
            raise VaultError(f"{len(missing)} split ids have no key label, e.g. {missing[:3]}")
        if set(sp.heldout) & set(sp.tune):
            raise VaultError("tune and held-out sets overlap")
        if not sp.heldout:
            raise VaultError("held-out set is empty")
        fd = self._lock()
        try:
            gens = self._gens()
            if gens and self.generation(gens[-1]).state != SPENT:
                raise VaultError(f"generation {gens[-1]} is not spent yet; open it before sealing a replacement")
            used = set()
            for g in gens:
                used |= set(self._read_json(self._gen_dir(g) / "heldout.json")["ids"])
            reused = sorted(used & set(sp.heldout))
            if reused:
                raise VaultError(f"{len(reused)} held-out ids were in an earlier held-out set, e.g. {reused[:3]}")
            n = (gens[-1] + 1) if gens else 1
            d = self._gen_dir(n)
            d.mkdir(exist_ok=True)
            for leftover in [d / "tune.json", *d.glob(".*.tmp")]:  # a seal that crashed before heldout.json
                leftover.unlink(missing_ok=True)
            mh = manifest_hash(sp.heldout)
            _write_once(d / "tune.json", json.dumps({"ids": list(sp.tune), "labels": {i: labels[i] for i in sp.tune}}).encode())
            body = {"ids": list(sp.heldout), "labels": {i: labels[i] for i in sp.heldout}, "manifest_hash": mh, "seed": sp.seed}
            # heldout.json is the commit point: until it exists, the generation does not exist.
            _write_once(d / "heldout.json", json.dumps(body).encode())
            _crash_point("seal-before-log")
            self._log("seal", n, manifest_hash=mh, heldout=len(sp.heldout), tune=len(sp.tune))
            return n
        finally:
            self._unlock(fd)

    def tune_set(self) -> dict[str, bool]:
        """The tune labels of the current generation. Always available; the held-out set is not."""
        g = self.current()
        return dict(self._read_json(self._gen_dir(g.n) / "tune.json")["labels"])

    def record_tune(self, config: Mapping[str, Any], note: str = "") -> int:
        """Record one tuning round. Allowed only before the freeze and up to the cap."""
        fd = self._lock()
        try:
            g = self.current()
            if g.state != SEALED:
                raise VaultError(f"generation {g.n} is {g.state}: tuning is closed; seal a fresh held-out set to retune")
            if g.tune_rounds >= self.max_tune_rounds:
                raise VaultError(f"tuning cap reached ({self.max_tune_rounds} rounds); freeze the best config")
            r = g.tune_rounds + 1
            _write_once(self._gen_dir(g.n) / f"tune-{r}.json", json.dumps({"config": dict(config), "note": note}).encode())
            self._log("tune", g.n, round=r, config_hash=config_hash(config))
            return r
        finally:
            self._unlock(fd)

    def freeze(self, config: Mapping[str, Any]) -> str:
        """Write down the judge before the held-out set is opened. Once per generation."""
        fd = self._lock()
        try:
            g = self.current()
            if g.state != SEALED:
                raise VaultError(f"generation {g.n} is already {g.state}")
            ch = config_hash(config)
            body = {"config": dict(config), "config_hash": ch, "manifest_hash": g.manifest_hash, "t": time.time()}
            try:
                _write_once(self._gen_dir(g.n) / "FREEZE.json", json.dumps(body, sort_keys=True).encode())
            except FileExistsError:
                raise VaultError(f"generation {g.n} is already frozen") from None
            _crash_point("freeze-before-log")
            self._log("freeze", g.n, config_hash=ch, manifest_hash=g.manifest_hash)
            return ch
        finally:
            self._unlock(fd)

    def open(self, config: Mapping[str, Any]) -> dict[str, bool]:
        """Open the held-out set, once, for the frozen config. Returns its labels; the set is then spent."""
        fd = self._lock()
        try:
            g = self.current()
            if g.state == SEALED:
                raise VaultError(f"generation {g.n} is not frozen: freeze the judge before opening")
            if g.state == SPENT:
                raise VaultError(f"generation {g.n} was already opened; the set is spent. Seal a fresh one")
            ch = config_hash(config)
            if ch != g.config_hash:
                raise VaultError("this is not the frozen config: open must use the judge that was frozen")
            d = self._gen_dir(g.n)
            try:
                mfd = os.open(d / "OPENED", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                raise VaultError(f"generation {g.n} was already opened; the set is spent") from None
            try:
                os.write(mfd, json.dumps({"t": time.time(), "config_hash": ch, "pid": os.getpid()}).encode())
                os.fsync(mfd)
            finally:
                os.close(mfd)
            _fsync_dir(d)
            _crash_point("open-before-log")
            self._log("open", g.n, config_hash=ch)
            _crash_point("open-after-log")
            held = self._read_json(d / "heldout.json")
            if manifest_hash(held["ids"]) != held["manifest_hash"]:
                raise VaultError("held-out file does not match its manifest hash")
            return dict(held["labels"])
        finally:
            self._unlock(fd)

    # -- audit -----------------------------------------------------------------------------

    def repair(self) -> list[str]:
        """After a crash: log any step whose file exists but whose ledger line is missing.

        The appended lines carry recovered=true. It never creates or removes a step's file, so it
        can't un-spend a set or invent a freeze.
        """
        fd = self._lock()
        try:
            for tmp in self.root.glob("gen-*/.*.tmp"):
                tmp.unlink(missing_ok=True)
            logged = {(e.get("event"), e.get("gen")) for e in self.events()}
            fixed = []
            for n in self._gens():
                d = self._gen_dir(n)
                steps = [("seal", d / "heldout.json"), ("freeze", d / "FREEZE.json"), ("open", d / "OPENED")]
                for event, f in steps:
                    if f.exists() and (event, n) not in logged:
                        self._log(event, n, recovered=True)
                        fixed.append(f"gen {n}: logged {event}")
            return fixed
        finally:
            self._unlock(fd)

    def events(self) -> list[dict]:
        if not self.ledger.exists():
            return []
        out = []
        for line in self.ledger.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"event": "torn-line"})
        return out

    def verify(self) -> list[str]:
        """Check files against hashes and the ledger. Returns problems; [] means consistent.

        A step whose file exists but whose ledger line is missing (a crash between the two) is
        reported as 'unlogged', not as corruption: the file is the truth.
        """
        problems: list[str] = []
        ev = self.events()
        if any(e.get("event") == "torn-line" for e in ev):
            problems.append("ledger has a torn line")
        logged = {(e.get("event"), e.get("gen")) for e in ev}
        for n in self._gens():
            d = self._gen_dir(n)
            held = self._read_json(d / "heldout.json")
            if manifest_hash(held["ids"]) != held["manifest_hash"]:
                problems.append(f"gen {n}: held-out ids do not match manifest hash")
            if ("seal", n) not in logged:
                problems.append(f"gen {n}: seal unlogged")
            if (d / "FREEZE.json").exists():
                fr = self._read_json(d / "FREEZE.json")
                if fr["manifest_hash"] != held["manifest_hash"]:
                    problems.append(f"gen {n}: freeze names a different held-out set")
                if config_hash(fr["config"]) != fr["config_hash"]:
                    problems.append(f"gen {n}: frozen config does not match its hash")
                if ("freeze", n) not in logged:
                    problems.append(f"gen {n}: freeze unlogged")
            if (d / "OPENED").exists():
                if not (d / "FREEZE.json").exists():
                    problems.append(f"gen {n}: opened without a freeze")
                if ("open", n) not in logged:
                    problems.append(f"gen {n}: open unlogged (crash during open; set counted as spent)")
            for e in ev:
                if e.get("gen") == n and e.get("event") == "open" and not (d / "OPENED").exists():
                    problems.append(f"gen {n}: ledger says opened but no OPENED marker")
            opens = sum(1 for e in ev if e.get("gen") == n and e.get("event") == "open")
            if opens > 1:
                problems.append(f"gen {n}: opened {opens} times")
            freezes = sum(1 for e in ev if e.get("gen") == n and e.get("event") == "freeze")
            if freezes > 1:
                problems.append(f"gen {n}: frozen {freezes} times")
        return problems
