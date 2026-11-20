"""The vault: seal a held-out set, freeze the judge, open the set once, replace a spent set.

The rules it enforces:
  1. The held-out labels are not handed out before the judge is frozen.
  2. A freeze records the judge's config hash and the held-out set's hashes, and happens once per set.
  3. A set opens once. The open must name the frozen config. After that the set is SPENT.
  4. Tuning is capped (the cap is fixed when the set is sealed) and allowed only before the freeze.
  5. A spent set can't be reused. Further tuning needs a fresh set: `seal` a new generation.

Each step writes a file, created atomically and exactly once (write to a temp file, fsync, then
link or O_EXCL), and appends a line to an append-only ledger. A step counts as having happened if
EITHER record shows it, so deleting a file can't undo a freeze, an open or a tuning round.

A crash part-way through `open` still spends the set. That is deliberate: the set may have been
read, and a held-out set that may have been seen is no longer held out.

What this does not do: it can't stop someone reading heldout.json with `cat`, and someone who edits
the files AND the ledger consistently can forge a history. It is a discipline tool, not a lock
against its owner. What it does is make the honest path the easy one, and `verify` reports any
place where the files and the ledger disagree.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from judge_kit.splits import Split, manifest_hash

SEALED, FROZEN, SPENT = "sealed", "frozen", "spent"
_GEN_RE = re.compile(r"^gen-(\d{3,})$")


class VaultError(RuntimeError):
    pass


def _crash_point(name: str) -> None:
    """Chaos-test hook: SIGKILL this process at a named point when JUDGE_KIT_CRASH_AT names it."""
    if os.environ.get("JUDGE_KIT_CRASH_AT") == name:
        os.kill(os.getpid(), 9)


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def content_hash(labels: Mapping[str, bool]) -> str:
    """Hash of a labelled set: its ids AND their labels."""
    return hashlib.sha256(json.dumps(sorted(labels.items()), separators=(",", ":")).encode()).hexdigest()


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
    max_tune_rounds: int


class Vault:
    def __init__(self, root: str | os.PathLike, max_tune_rounds: int = 3, create: bool = True):
        """`max_tune_rounds` applies to sets sealed through this handle; a sealed set keeps its own cap.
        With create=False a missing directory is an error rather than a new, empty vault."""
        self.root = Path(root)
        self.max_tune_rounds = max_tune_rounds
        self.ledger = self.root / "ledger.jsonl"
        if create:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise VaultError(f"cannot use {self.root} as a vault directory ({e.strerror})") from None
            if not self.root.is_dir():
                raise VaultError(f"{self.root} is not a directory")
        elif not self.root.is_dir():
            raise VaultError(f"no vault at {self.root}")
        elif not self.ledger.exists() and not self._gens():
            raise VaultError(f"{self.root} is not a vault (no ledger, no sealed generations)")

    # -- internals -------------------------------------------------------------------------

    def _gen_dir(self, n: int) -> Path:
        return self.root / f"gen-{n:03d}"

    def _gens(self) -> list[int]:
        out = []
        for p in self.root.iterdir():
            m = _GEN_RE.match(p.name)
            if m and p.name == f"gen-{int(m.group(1)):03d}" and (p / "heldout.json").exists():
                out.append(int(m.group(1)))
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
        fd = os.open(self.ledger, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = "\n" + line  # a torn last line must not swallow this event
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _read_json(self, p: Path) -> dict:
        return json.loads(p.read_text())

    def _logged(self, n: int, event: str) -> list[dict]:
        return [e for e in self.events() if e.get("gen") == n and e.get("event") == event]

    def _tune_files(self, n: int) -> list[int]:
        out = []
        for p in self._gen_dir(n).glob("tune-*.json"):
            m = re.match(r"^tune-(\d+)\.json$", p.name)
            if m and p.name == f"tune-{int(m.group(1))}.json":
                out.append(int(m.group(1)))
        return sorted(out)

    def generation(self, n: int) -> Generation:
        d = self._gen_dir(n)
        held = self._read_json(d / "heldout.json")
        if (d / "OPENED").exists() or self._logged(n, "open"):
            state = SPENT
        elif (d / "FREEZE.json").exists() or self._logged(n, "freeze"):
            state = FROZEN
        else:
            state = SEALED
        ch = None
        if (d / "FREEZE.json").exists():
            ch = self._read_json(d / "FREEZE.json")["config_hash"]
        elif self._logged(n, "freeze"):
            ch = self._logged(n, "freeze")[0].get("config_hash")
        files = self._tune_files(n)
        rounds = max(max(files, default=0), len(files), len(self._logged(n, "tune")))
        cap = int(held.get("max_tune_rounds", self.max_tune_rounds))
        return Generation(n=n, state=state, manifest_hash=held["manifest_hash"], config_hash=ch, tune_rounds=rounds, max_tune_rounds=cap)

    def _ledger_gens(self) -> set[int]:
        return {e["gen"] for e in self.events() if e.get("event") == "seal" and isinstance(e.get("gen"), int)}

    def _require_format(self, n: int) -> None:
        held = self._read_json(self._gen_dir(n) / "heldout.json")
        if "content_hash" not in held or "max_tune_rounds" not in held:
            raise VaultError(f"generation {n} was sealed by an older version of judge-kit, which this version can't "
                             "use; start a new vault directory")

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
        bad = [i for i in (*sp.heldout, *sp.tune) if not isinstance(labels[i], bool)]
        if bad:
            raise VaultError(f"labels must be true/false, e.g. {bad[:3]}")
        fd = self._lock()
        try:
            gens = self._gens()
            lost = sorted(self._ledger_gens() - set(gens))
            if lost:
                raise VaultError(f"generation(s) {lost} are in the ledger but their files are missing; restore them from a "
                                 "backup (without them the vault can't check that a new held-out set reuses none of their ids)")
            if gens and self.generation(gens[-1]).state != SPENT:
                self._require_format(gens[-1])
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
            held_labels = {i: bool(labels[i]) for i in sp.heldout}
            tune_labels = {i: bool(labels[i]) for i in sp.tune}
            hh, th = content_hash(held_labels), content_hash(tune_labels)
            _write_once(d / "tune.json", json.dumps({"ids": list(sp.tune), "labels": tune_labels, "content_hash": th}).encode())
            body = {"ids": list(sp.heldout), "labels": held_labels, "manifest_hash": mh, "content_hash": hh,
                    "seed": sp.seed, "max_tune_rounds": self.max_tune_rounds}
            # heldout.json is the commit point: until it exists, the generation does not exist.
            _write_once(d / "heldout.json", json.dumps(body).encode())
            _crash_point("seal-before-log")
            self._log("seal", n, manifest_hash=mh, content_hash=hh, tune_hash=th, heldout=len(sp.heldout),
                      tune=len(sp.tune), max_tune_rounds=self.max_tune_rounds)
            return n
        finally:
            self._unlock(fd)

    def heldout_ids(self) -> list[str]:
        """Which items are held out in the current generation (ids only, never labels)."""
        g = self.current()
        return list(self._read_json(self._gen_dir(g.n) / "heldout.json")["ids"])

    def tune_set(self) -> dict[str, bool]:
        """The tune labels of the current generation. Always available; the held-out labels are not."""
        g = self.current()
        return dict(self._read_json(self._gen_dir(g.n) / "tune.json")["labels"])

    def record_tune(self, config: Mapping[str, Any], note: str = "") -> int:
        """Record one tuning round. Allowed only before the freeze and up to the sealed cap."""
        fd = self._lock()
        try:
            g = self.current()
            self._require_format(g.n)
            if g.state != SEALED:
                raise VaultError(f"generation {g.n} is {g.state}: tuning is closed; seal a fresh held-out set to retune")
            if g.tune_rounds >= g.max_tune_rounds:
                raise VaultError(f"tuning cap reached ({g.max_tune_rounds} rounds); freeze the best config")
            r = g.tune_rounds + 1
            try:
                _write_once(self._gen_dir(g.n) / f"tune-{r}.json", json.dumps({"config": dict(config), "note": note}).encode())
            except FileExistsError:
                raise VaultError(f"tune round {r} already has a file; run verify") from None
            self._log("tune", g.n, round=r, config_hash=config_hash(config))
            return r
        finally:
            self._unlock(fd)

    def freeze(self, config: Mapping[str, Any]) -> str:
        """Write down the judge before the held-out set is opened. Once per generation."""
        fd = self._lock()
        try:
            g = self.current()
            self._require_format(g.n)
            if g.state != SEALED:
                raise VaultError(f"generation {g.n} is already {g.state}")
            ch = config_hash(config)
            held = self._read_json(self._gen_dir(g.n) / "heldout.json")
            body = {"config": dict(config), "config_hash": ch, "manifest_hash": g.manifest_hash,
                    "content_hash": held.get("content_hash"), "t": time.time()}
            try:
                _write_once(self._gen_dir(g.n) / "FREEZE.json", json.dumps(body, sort_keys=True).encode())
            except FileExistsError:
                raise VaultError(f"generation {g.n} is already frozen") from None
            _crash_point("freeze-before-log")
            self._log("freeze", g.n, config_hash=ch, manifest_hash=g.manifest_hash, content_hash=held.get("content_hash"))
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
            self._require_format(g.n)
            d = self._gen_dir(g.n)
            if not (d / "FREEZE.json").exists():
                raise VaultError(f"generation {g.n}: the ledger says frozen but FREEZE.json is missing; run verify")
            ch = config_hash(config)
            logged = [e.get("config_hash") for e in self._logged(g.n, "freeze")]
            if ch != g.config_hash or any(h is not None and h != ch for h in logged):
                raise VaultError("this is not the frozen config: open must use the judge that was frozen")
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
            if manifest_hash(held["ids"]) != held["manifest_hash"] or content_hash(held["labels"]) != held.get("content_hash"):
                raise VaultError("held-out file does not match its hashes")
            return dict(held["labels"])
        finally:
            self._unlock(fd)

    # -- audit -----------------------------------------------------------------------------

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

    def repair(self) -> list[str]:
        """After a crash: log any step whose file exists but whose ledger line is missing, and
        acknowledge torn ledger lines.

        The appended lines carry recovered=true. It never creates or removes a step's file, so it
        can't un-spend a set or invent a freeze.
        """
        fd = self._lock()
        try:
            for tmp in self.root.glob("gen-*/.*.tmp"):
                tmp.unlink(missing_ok=True)
            ev = self.events()
            logged = {(e.get("event"), e.get("gen")) for e in ev}
            fixed = []
            for n in self._gens():
                d = self._gen_dir(n)
                if (d / "heldout.json").exists() and ("seal", n) not in logged:
                    h = self._read_json(d / "heldout.json")
                    t = self._read_json(d / "tune.json") if (d / "tune.json").exists() else {}
                    self._log("seal", n, recovered=True, manifest_hash=h["manifest_hash"], content_hash=h.get("content_hash"),
                              tune_hash=t.get("content_hash"), max_tune_rounds=h.get("max_tune_rounds"))
                    fixed.append(f"gen {n}: logged seal")
                logged_rounds = {e.get("round") for e in ev if e.get("gen") == n and e.get("event") == "tune"}
                for r in self._tune_files(n):
                    if r not in logged_rounds:
                        cfg = self._read_json(d / f"tune-{r}.json").get("config", {})
                        self._log("tune", n, round=r, config_hash=config_hash(cfg), recovered=True)
                        fixed.append(f"gen {n}: logged tune round {r}")
                if (d / "FREEZE.json").exists() and ("freeze", n) not in logged:
                    self._log("freeze", n, recovered=True, config_hash=self._read_json(d / "FREEZE.json")["config_hash"])
                    fixed.append(f"gen {n}: logged freeze")
                if (d / "OPENED").exists() and ("open", n) not in logged:
                    try:
                        och = json.loads((d / "OPENED").read_text() or "{}").get("config_hash")
                    except json.JSONDecodeError:
                        och = None
                    self._log("open", n, recovered=True, config_hash=och)
                    fixed.append(f"gen {n}: logged open")
            torn = sum(1 for e in ev if e.get("event") == "torn-line")
            acked = sum(e.get("torn_lines", 0) for e in ev if e.get("event") == "repair")
            if torn > acked:
                self._log("repair", 0, torn_lines=torn - acked)
                fixed.append(f"acknowledged {torn - acked} torn ledger line(s)")
            return fixed
        finally:
            self._unlock(fd)

    def verify(self) -> list[str]:
        """Check the files against their hashes and against the ledger. Returns problems; [] means consistent.

        A step whose file exists but whose ledger line is missing (a crash between the two) is
        reported as 'unlogged'; `repair` logs it.
        """
        problems: list[str] = []
        ev = self.events()
        torn = sum(1 for e in ev if e.get("event") == "torn-line")
        acked = sum(e.get("torn_lines", 0) for e in ev if e.get("event") == "repair")
        if torn > acked:
            problems.append(f"ledger has {torn - acked} torn line(s); run repair to acknowledge them")
        for n in sorted(self._ledger_gens() - set(self._gens())):
            problems.append(f"gen {n}: in the ledger but its files are missing")
        known = {"seal", "tune", "freeze", "open", "repair", "torn-line"}
        for e in ev:
            if e.get("event") not in known:
                problems.append(f"unknown ledger event {e.get('event')!r} (gen {e.get('gen')}); the vault never writes it")
        for e in ev:
            if e.get("recovered"):
                problems.append(f"gen {e.get('gen')}: {e.get('event')} recovered from its file after a crash; "
                                "it could not be checked against the ledger")
        earlier: set[str] = set()
        for n in self._gens():
            d = self._gen_dir(n)
            seals, freezes, opens, tunes = (self._logged(n, k) for k in ("seal", "freeze", "open", "tune"))
            held = self._read_json(d / "heldout.json")
            if "content_hash" not in held or "max_tune_rounds" not in held:
                problems.append(f"gen {n}: sealed by an older version of judge-kit; its labels and cap are not hashed")
            if manifest_hash(held["ids"]) != held["manifest_hash"]:
                problems.append(f"gen {n}: held-out ids do not match their manifest hash")
            if content_hash(held["labels"]) != held.get("content_hash") or sorted(held["labels"]) != sorted(held["ids"]):
                problems.append(f"gen {n}: held-out labels do not match their content hash")
            tune = self._read_json(d / "tune.json") if (d / "tune.json").exists() else None
            if tune is None:
                problems.append(f"gen {n}: tune.json is missing")
            elif content_hash(tune["labels"]) != tune.get("content_hash"):
                problems.append(f"gen {n}: tune set does not match its content hash")
            if tune is not None and set(tune["labels"]) & set(held["ids"]):
                problems.append(f"gen {n}: tune and held-out sets overlap")
            if set(held["ids"]) & earlier:
                problems.append(f"gen {n}: held-out ids reused from an earlier generation")
            earlier |= set(held["ids"])
            if not seals:
                problems.append(f"gen {n}: seal unlogged")
            else:
                s = seals[0]
                if s.get("manifest_hash") != held["manifest_hash"] or s.get("content_hash") != held.get("content_hash"):
                    problems.append(f"gen {n}: held-out set differs from the one the ledger recorded at seal")
                if tune is not None and s.get("tune_hash") != tune.get("content_hash"):
                    problems.append(f"gen {n}: tune set differs from the one the ledger recorded at seal")
                if s.get("max_tune_rounds") != held.get("max_tune_rounds"):
                    problems.append(f"gen {n}: tuning cap differs from the one the ledger recorded at seal")
            files = self._tune_files(n)
            if files != list(range(1, len(files) + 1)) or len(files) != len(tunes):
                problems.append(f"gen {n}: {len(files)} tune files but {len(tunes)} tune events in the ledger")
            # The ledger's freeze line is the earlier record, so it names the frozen config when it exists.
            frozen_hash = freezes[0].get("config_hash") if freezes else None
            if (d / "FREEZE.json").exists():
                fr = self._read_json(d / "FREEZE.json")
                frozen_hash = frozen_hash or fr["config_hash"]
                if fr["manifest_hash"] != held["manifest_hash"] or fr.get("content_hash") != held.get("content_hash"):
                    problems.append(f"gen {n}: freeze names a different held-out set")
                if config_hash(fr["config"]) != fr["config_hash"]:
                    problems.append(f"gen {n}: frozen config does not match its hash")
                if not freezes:
                    problems.append(f"gen {n}: freeze unlogged")
                elif freezes[0].get("config_hash") != fr["config_hash"]:
                    problems.append(f"gen {n}: FREEZE.json differs from the freeze the ledger recorded")
            elif freezes:
                problems.append(f"gen {n}: the ledger says frozen but there is no FREEZE.json")
            if (d / "OPENED").exists():
                if not (d / "FREEZE.json").exists() and not freezes:
                    problems.append(f"gen {n}: opened without a freeze")
                if not opens:
                    problems.append(f"gen {n}: open unlogged (crash during open; set counted as spent)")
                try:
                    och = json.loads((d / "OPENED").read_text() or "{}").get("config_hash")
                except json.JSONDecodeError:
                    och = None
                if och is not None and frozen_hash is not None and och != frozen_hash:
                    problems.append(f"gen {n}: opened with a config that was not the frozen one")
            elif opens:
                problems.append(f"gen {n}: ledger says opened but no OPENED marker")
            for e in opens:
                if e.get("config_hash") is not None and frozen_hash is not None and e["config_hash"] != frozen_hash:
                    problems.append(f"gen {n}: the ledger's open names a config that was not the frozen one")
            if len(opens) > 1:
                problems.append(f"gen {n}: opened {len(opens)} times")
            if len(freezes) > 1:
                problems.append(f"gen {n}: frozen {len(freezes)} times")
        return problems
