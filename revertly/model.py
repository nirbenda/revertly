"""revertly domain model — the FROZEN contract shared by all modules.

Every worker builds against these types. Do not change field names or
semantics without updating TECH-DESIGN.md and every dependent module.

Pure data + (de)serialization only. No I/O, no side effects, no imports
beyond the stdlib typing/dataclasses/enum. This keeps the contract testable
in isolation and impossible to break by accident.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ─────────────────────────── enums ───────────────────────────

class EventKind(str, Enum):
    FS = "fs"            # a filesystem change observed by the watcher
    TOOL = "tool"        # an agent tool call (from Claude Code hooks)
    TRIPWIRE = "tripwire"  # a sensitive-path hit
    HEARTBEAT = "heartbeat"  # watcher liveness beacon
    SELF_TAMPER = "self_tamper"  # attempt to touch revertly's own state


class FsOp(str, Enum):
    CREATE = "create"
    WRITE = "write"
    DELETE = "delete"
    RENAME = "rename"
    READ = "read"       # only ever from the tool/hook layer (FSEvents can't see reads)


class ChangeType(str, Enum):
    """How a path differs between a pre-image and current state."""
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ALERT = "alert"


# ─────────────────────────── events ───────────────────────────

@dataclass
class Event:
    """One line in a session journal (journal.jsonl).

    Serialized as a single JSON object per line. `seq` and `prev_hash`/`hash`
    are populated by the Journal writer (hash chain); producers leave them None.
    """
    kind: EventKind
    t: float = field(default_factory=time.time)   # epoch seconds
    op: Optional[FsOp] = None
    path: Optional[str] = None
    tool: Optional[str] = None                     # e.g. "Edit", "Bash"
    target: Optional[str] = None                   # tool's target path/arg
    version: Optional[str] = None                  # e.g. "v3" for fs writes
    checkpoint: Optional[int] = None               # tool-call ordinal
    severity: Severity = Severity.INFO
    detail: Optional[str] = None
    shared: bool = False                           # touched by >1 concurrent session
    # hash-chain fields (filled by Journal.append):
    seq: Optional[int] = None
    prev_hash: Optional[str] = None
    hash: Optional[str] = None

    def to_json_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind.value
        d["op"] = self.op.value if self.op else None
        d["severity"] = self.severity.value
        return {k: v for k, v in d.items() if v is not None}

    @staticmethod
    def from_json_dict(d: dict) -> "Event":
        return Event(
            kind=EventKind(d["kind"]),
            t=d.get("t", 0.0),
            op=FsOp(d["op"]) if d.get("op") else None,
            path=d.get("path"),
            tool=d.get("tool"),
            target=d.get("target"),
            version=d.get("version"),
            checkpoint=d.get("checkpoint"),
            severity=Severity(d.get("severity", "info")),
            detail=d.get("detail"),
            shared=d.get("shared", False),
            seq=d.get("seq"),
            prev_hash=d.get("prev_hash"),
            hash=d.get("hash"),
        )


# ─────────────────────────── session ───────────────────────────

@dataclass
class SessionMeta:
    """meta.json for one session directory."""
    id: str                          # e.g. "2026-07-25T10-30-00_a1b2"
    name: str                        # human name derived from prompt/argv
    cwd: str                         # project dir at launch
    argv: list                       # argv passed to the wrapped command
    started: float
    ended: Optional[float] = None
    exit_code: Optional[int] = None
    snapshot: Optional[str] = None   # APFS snapshot name (None if unavailable)
    clone_path: Optional[str] = None # path to the CoW pre-image of cwd
    armed: bool = False              # did the safety net fully arm?
    is_revert: bool = False          # this session was itself a revert
    reverts_session: Optional[str] = None  # id of the session this reverted

    def to_json_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @staticmethod
    def from_json_dict(d: dict) -> "SessionMeta":
        return SessionMeta(**{k: d.get(k) for k in SessionMeta.__dataclass_fields__ if k in d})


# ─────────────────────────── versions & revert ───────────────────────────

@dataclass
class Version:
    """One captured content-state of a file within a session."""
    path: str            # logical path (relative to session cwd)
    label: str           # "v0", "v1", ...
    blob_path: str       # where the captured bytes live in the session store
    t: float
    checkpoint: Optional[int] = None


@dataclass
class Change:
    """A single path's difference between a session pre-image and current."""
    path: str            # absolute path on disk
    change_type: ChangeType
    pre_blob: Optional[str] = None   # path to pre-image bytes (None if CREATED)


@dataclass
class Conflict:
    """A change that cannot be cleanly reverted because current state diverged."""
    path: str
    reason: str          # human explanation ("modified after session ended")


@dataclass
class RevertPlan:
    """Preview of what a revert will do. Produced before any mutation."""
    session_id: str
    restores: list = field(default_factory=list)   # list[Change] modified/deleted -> restore
    deletes: list = field(default_factory=list)    # list[Change] created -> delete
    conflicts: list = field(default_factory=list)  # list[Conflict]

    @property
    def is_clean(self) -> bool:
        return not self.conflicts

    def summary(self) -> str:
        return (f"{len(self.restores)} to restore, {len(self.deletes)} to delete, "
                f"{len(self.conflicts)} conflict(s)")


@dataclass
class TripwireHit:
    path: str
    op: FsOp
    pattern: str         # the tripwire glob that matched
    severity: Severity = Severity.ALERT
    self_tamper: bool = False
