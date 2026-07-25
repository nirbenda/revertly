"""Append-only, hash-chained JSONL journal for a revertly session.

Each session writes one `journal.jsonl` whose lines are Event records. Every
record carries a monotonic `seq`, a `prev_hash` linking it to the record
before it, and a `hash` over its own canonical serialization (excluding the
`hash` field). This forms a tamper-evident chain: editing, reordering, or
relinking any record breaks verification.

Truncation is *not* tampering — a truncated file that is a clean prefix of the
original chain still verifies as intact. Only modified/reordered/relinked
records fail `verify()`.

stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

from revertly.model import Event, EventKind


def _canonical(record: dict) -> str:
    """Deterministic serialization of a record for hashing.

    Excludes the `hash` field itself; includes seq and prev_hash. sort_keys
    plus tight separators make the byte string reproducible across runs.
    """
    payload = {k: v for k, v in record.items() if k != "hash"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _record_hash(record: dict) -> str:
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


class Journal:
    """An append-only hash-chained event log at `path`."""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._last_seq: Optional[int] = None
        self._last_hash: Optional[str] = None
        # arm() starts one watcher per root (project + sensitive dirs), each a
        # thread calling append(). Without this lock the read-modify-write of
        # _last_seq/_last_hash races and corrupts the chain -> verify() would
        # report the session as TAMPERED. The lock makes append() atomic.
        self._lock = threading.Lock()
        self._init_from_tail()

    def _init_from_tail(self) -> None:
        """Seed last seq/hash from the existing file so we continue the chain.

        Reads the final non-blank line only for the seq/hash we must chain from;
        creates an empty file if none exists.
        """
        if not os.path.exists(self.path):
            open(self.path, "a", encoding="utf-8").close()
            return
        last = None
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return
        rec = json.loads(last)
        self._last_seq = rec.get("seq")
        self._last_hash = rec.get("hash")

    def append(self, e: Event) -> Event:
        with self._lock:
            e.seq = 0 if self._last_seq is None else self._last_seq + 1
            e.prev_hash = self._last_hash
            e.hash = None
            record = e.to_json_dict()
            e.hash = _record_hash(record)
            record["hash"] = e.hash
            line = json.dumps(record, sort_keys=True, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._last_seq = e.seq
            self._last_hash = e.hash
            return e

    def heartbeat(self) -> Event:
        return self.append(Event(kind=EventKind.HEARTBEAT))

    @staticmethod
    def read(path) -> list:
        """Best-effort reader: a corrupt/partial line (process killed
        mid-append) is skipped, never fatal — verify() is the strict surface
        that reports tampering/corruption precisely."""
        if not os.path.exists(path):
            return []
        events = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    events.append(Event.from_json_dict(json.loads(line)))
                except (ValueError, KeyError):
                    continue
        return events

    @staticmethod
    def verify(path):
        """Recompute the chain. Returns (True, None) if intact for the whole
        file, else (False, seq_of_first_bad_record).

        A truncated-but-clean prefix verifies as intact. A modified, reordered,
        or relinked record fails at the first offending record's seq.
        """
        if not os.path.exists(path):
            return (True, None)
        expected_seq = 0
        prev_hash = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    # a corrupted/garbled line is tampering, not a crash
                    return (False, expected_seq)
                seq = rec.get("seq")
                # sequence must be monotonic 0,1,2,... (catches reordering)
                if seq != expected_seq:
                    return (False, seq if seq is not None else expected_seq)
                # linkage must match prior record's hash (catches relink)
                if rec.get("prev_hash") != prev_hash:
                    return (False, seq)
                # stored hash must equal recomputed hash (catches edits)
                stored = rec.get("hash")
                if stored != _record_hash(rec):
                    return (False, seq)
                prev_hash = stored
                expected_seq += 1
        return (True, None)

    @staticmethod
    def verify_sealed(journal_path, seal_path):
        """Chain verify PLUS truncation detection against the immutable seal
        anchor written at seal(). Returns (ok, reason):
          * chain broken            -> (False, "chain breaks at seq N")
          * journal shorter than    -> (False, "truncated: N of M records")
            the sealed final seq        (the clean-prefix cut verify() alone
                                         would accept)
          * final hash mismatch     -> (False, "final record altered")
          * no seal (unsealed/live) -> chain result only
        """
        ok, bad = Journal.verify(journal_path)
        if not ok:
            return (False, f"chain breaks at seq {bad}")
        if not os.path.exists(seal_path):
            return (True, None)   # never sealed (still running / old session)
        try:
            with open(seal_path) as f:
                sealed = json.load(f)
        except (OSError, ValueError):
            return (True, None)
        last_seq, last_hash = None, None
        for e in Journal.read(journal_path):
            last_seq, last_hash = e.seq, e.hash
        want_seq = sealed.get("seq")
        if want_seq is not None and (last_seq is None or last_seq < want_seq):
            return (False,
                    f"truncated: {(last_seq or -1) + 1} of {want_seq + 1} records")
        if sealed.get("hash") and last_hash != sealed.get("hash"):
            return (False, "final record altered or removed")
        return (True, None)
