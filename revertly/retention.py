"""revertly.retention — decide which sessions to prune, and do it safely.

The store accumulates one pre-image clone + journal per `claude` run; deleted
files are materialized as real bytes, so it grows. This module is the single,
testable place that answers "which sessions should go?" for every surface:

  * `revertly gc`         — enforce the retention policy (age + disk cap)
  * `revertly clear`      — the "I'm at a safe point, clear history" button
  * automatic enforcement — a light pass at session seal
  * the UI Storage tab    — same plan, previewed then applied

Invariants (safety first — deletion is the one irreversible op):
  * The LIVE session (not yet sealed: ended is None) is NEVER pruned.
  * Tripwire/self-tamper FLAGGED sessions are evidence: kept unless the caller
    explicitly opts in (include_flagged=True).
  * Disk-cap pruning removes the OLDEST non-flagged sessions first.
  * Every prune is reported (reason) so callers can preview and log it.

Pure planning (`plan`) is separated from I/O (`apply`) so it unit-tests
without touching a real store.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional

from revertly import paths


@dataclass
class SessionInfo:
    id: str
    started: float
    ended: Optional[float]
    size: int
    flagged: bool
    is_revert: bool

    @property
    def is_live(self) -> bool:
        # unsealed session (crashed or currently running) — never auto-prune
        return self.ended is None


@dataclass
class PruneItem:
    id: str
    size: int
    flagged: bool
    reason: str


def _read_meta(sid: str) -> dict:
    try:
        with open(paths.meta_path(sid), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _is_flagged(sid: str, meta: dict) -> bool:
    # prefer the flag recorded at seal; fall back to a journal scan for older
    # sessions that predate the field.
    if "flagged" in meta:
        return bool(meta["flagged"])
    try:
        from revertly.search import read_events_raw
        return any(e.get("kind") in ("tripwire", "self_tamper")
                   for e in read_events_raw(sid))
    except Exception:
        return False


def collect(session_ids: Optional[List[str]] = None) -> List[SessionInfo]:
    """Build SessionInfo for each session (newest first)."""
    ids = session_ids if session_ids is not None else paths.list_session_ids()
    out = []
    for sid in ids:
        meta = _read_meta(sid)
        out.append(SessionInfo(
            id=sid,
            started=float(meta.get("started") or 0.0),
            ended=meta.get("ended"),
            size=paths.session_size(sid),
            flagged=_is_flagged(sid, meta),
            is_revert=bool(meta.get("is_revert")),
        ))
    out.sort(key=lambda s: s.started, reverse=True)
    return out


def plan(sessions: List[SessionInfo], *,
         keep_days: Optional[float] = None,
         max_disk_bytes: Optional[int] = None,
         before: Optional[float] = None,
         include_flagged: bool = False,
         clear_all: bool = False,
         now: Optional[float] = None) -> List[PruneItem]:
    """Return the sessions to prune, given a policy. Pure — no I/O.

    Any of keep_days / before / clear_all select sessions by cutoff; max_disk
    then adds the oldest remaining non-flagged sessions until the store fits.
    The live (unsealed) session and — unless include_flagged — flagged
    sessions are always protected.
    """
    now = time.time() if now is None else now
    prunable = [s for s in sessions if not s.is_live]
    if not include_flagged:
        prunable = [s for s in prunable if not s.flagged]

    chosen: dict = {}   # id -> PruneItem

    def mark(s: SessionInfo, reason: str):
        chosen.setdefault(s.id, PruneItem(s.id, s.size, s.flagged, reason))

    if clear_all:
        for s in prunable:
            mark(s, "clear-all")
    if keep_days is not None:
        cutoff = now - keep_days * 86400
        for s in prunable:
            if s.started < cutoff:
                mark(s, f"older than {keep_days:g}d")
    if before is not None:
        for s in prunable:
            if s.started < before:
                mark(s, "before cutoff")

    if max_disk_bytes is not None:
        total = sum(s.size for s in sessions)
        # prune oldest non-flagged first until under cap
        remaining = total - sum(p.size for p in chosen.values())
        for s in sorted(prunable, key=lambda s: s.started):  # oldest first
            if remaining <= max_disk_bytes:
                break
            if s.id not in chosen:
                mark(s, "over disk cap")
                remaining -= s.size

    # preserve newest-first order for display
    order = {s.id: i for i, s in enumerate(sessions)}
    return sorted(chosen.values(), key=lambda p: order.get(p.id, 1 << 30))


def apply(items: List[PruneItem], *, log: bool = True) -> int:
    """Delete the planned sessions. Returns count removed. Incident-logged."""
    removed = 0
    for it in items:
        if log:
            tag = " (FLAGGED evidence)" if it.flagged else ""
            paths.append_incident(
                "PRUNE", f"cleared session {it.id} [{it.reason}]{tag}")
        paths.rmtree_force(paths.session_dir(it.id))
        removed += 1
    return removed


def enforce_policy(cfg) -> int:
    """Automatic retention pass (run at seal): apply the config's day + disk
    limits, non-flagged only, quietly. Returns count pruned."""
    keep_days = getattr(cfg, "retention_days", None)
    max_gb = getattr(cfg, "max_disk_gb", None)
    max_bytes = int(max_gb * 1e9) if max_gb else None
    if not keep_days and not max_bytes:
        return 0
    items = plan(collect(), keep_days=keep_days, max_disk_bytes=max_bytes)
    return apply(items)
