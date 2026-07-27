"""revertly.search — find events across ALL sessions ("where is my file?").

One matcher, shared by the CLI (`revertly find`, `log --path`) and the UI
(`GET /api/find`), so both surfaces answer identically.

Pattern semantics (case-insensitive):
  * plain text            -> substring match on the event path
  * contains * ? or [     -> fnmatch glob against the full path AND the
                             basename (so `*.env` finds `/x/y/.env` and
                             `secrets.env` anywhere)

Python 3.9 stdlib only.
"""
from __future__ import annotations

import fnmatch
import json
import os
from typing import Iterable, List, Optional

from revertly import paths

_GLOB_CHARS = ("*", "?", "[")

# fs ops that mutate disk; tripwire/self_tamper events always searchable.
MUTATING_OPS = ("write", "create", "delete", "rename")


def is_glob(pattern: str) -> bool:
    return any(c in pattern for c in _GLOB_CHARS)


def path_matches(path: Optional[str], pattern: str) -> bool:
    """Case-insensitive substring or glob match on an event path."""
    if not path or not pattern:
        return False
    p = path.lower()
    pat = pattern.lower()
    if is_glob(pat):
        return (fnmatch.fnmatch(p, pat)
                or fnmatch.fnmatch(os.path.basename(p), pat)
                # a bare glob like `src/*.py` should also match by suffix
                or fnmatch.fnmatch(p, "*" + pat))
    return pat in p


def read_events_raw(session_id: str) -> List[dict]:
    """Journal lines as dicts; tolerant of a corrupt/partial trailing line."""
    out: List[dict] = []
    jp = paths.journal_path(session_id)
    if not os.path.isfile(jp):
        return out
    try:
        with open(jp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def burst_deleted_paths(session_id: str, t_start: float) -> List[str]:
    """Paths the session deleted at or after `t_start` — the file set a
    runaway-deletion burst removed. Order-preserving and de-duplicated; the
    reverter restores each from the pre-image clone. (Reconstructed from the
    journal at undo time so it captures the *whole* burst, including deletions
    that landed after the alarm first tripped.)"""
    seen, out = set(), []
    for ev in read_events_raw(session_id):
        if ev.get("op") != "delete":
            continue
        if (ev.get("t") or 0.0) < t_start:
            continue
        p = ev.get("path")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def read_meta_raw(session_id: str) -> dict:
    mp = paths.meta_path(session_id)
    try:
        with open(mp, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def find_events(pattern: str, *, op: Optional[str] = None,
                since: Optional[float] = None,
                session_ids: Optional[Iterable[str]] = None,
                limit: int = 500) -> List[dict]:
    """Search every session's journal for path-matching events.

    Returns hit dicts, newest session first:
      {session_id, session_name, cwd, kind, op, path, t}
    Only mutating fs events and tripwire/self_tamper events are considered
    (heartbeats and reads-without-paths never match). Stops after `limit`
    hits so a broad pattern over a huge store stays bounded.
    """
    hits: List[dict] = []
    sids = list(session_ids) if session_ids is not None else paths.list_session_ids()
    for sid in sorted(sids, reverse=True):
        if len(hits) >= limit:
            break
        meta = read_meta_raw(sid)
        for ev in read_events_raw(sid):
            if len(hits) >= limit:
                break
            kind = ev.get("kind")
            if kind == "fs":
                if ev.get("op") not in MUTATING_OPS:
                    continue
            elif kind not in ("tripwire", "self_tamper"):
                continue
            if op and ev.get("op") != op:
                continue
            if since is not None and (ev.get("t") or 0) < since:
                continue
            # a rename is findable by EITHER end of the move
            if not (path_matches(ev.get("path"), pattern)
                    or path_matches(ev.get("path_from"), pattern)):
                continue
            hits.append({
                "session_id": sid,
                "session_name": meta.get("name", ""),
                "cwd": meta.get("cwd", ""),
                "kind": kind,
                "op": ev.get("op"),
                "path": ev.get("path"),
                "path_from": ev.get("path_from"),
                "t": ev.get("t"),
            })
    return hits
