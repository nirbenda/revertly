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

    # An unsealed session (ended is None) is either running NOW or a crash.
    # We can't see the PID, so we use a grace window: unsealed AND started
    # within LIVE_GRACE = "probably running, protect it"; unsealed but older =
    # a zombie that can be reclaimed. A real agent run rarely exceeds a day.
    def is_live(self, now: float) -> bool:
        return self.ended is None and (now - self.started) < LIVE_GRACE

    def age_ref(self) -> float:
        # age is measured from when the RECORDING finished (ended), not when it
        # started — a session that ran for 40 days and just ended is not "40
        # days of old history". Falls back to started for unsealed sessions.
        return self.ended if self.ended is not None else self.started


LIVE_GRACE = 86400.0   # 24h: unsealed-and-recent = treated as live


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
    prunable = [s for s in sessions if not s.is_live(now)]
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
            if s.age_ref() < cutoff:   # age from when the recording ENDED
                mark(s, f"older than {keep_days:g}d")
    if before is not None:
        for s in prunable:
            if s.started < before:     # timeline position -> started
                mark(s, "before cutoff")

    if max_disk_bytes is not None:
        # Disk cap can only reclaim PRUNABLE bytes. If the protected floor
        # (live + kept-flagged) already exceeds the cap, chasing it would
        # delete all history for nothing — so target max(cap, protected floor)
        # and only remove what's actually over that.
        protected_floor = sum(s.size for s in sessions
                              if s.is_live(now) or (s.flagged and not include_flagged))
        target = max(max_disk_bytes, protected_floor)
        remaining = sum(s.size for s in sessions) - sum(p.size for p in chosen.values())
        for s in sorted(prunable, key=lambda s: s.age_ref()):  # oldest first
            if remaining <= target:
                break
            if s.id not in chosen:
                mark(s, "over disk cap")
                remaining -= s.size

    # preserve newest-first order for display
    order = {s.id: i for i, s in enumerate(sessions)}
    return sorted(chosen.values(), key=lambda p: order.get(p.id, 1 << 30))


def apply(items: List[PruneItem], *, log: bool = True) -> int:
    """Delete the planned sessions. Returns the count ACTUALLY removed (a tree
    that couldn't be fully deleted is not counted). Incident-logged."""
    removed = 0
    for it in items:
        if log:
            tag = " (FLAGGED evidence)" if it.flagged else ""
            paths.append_incident(
                "PRUNE", f"cleared session {it.id} [{it.reason}]{tag}")
        paths.rmtree_force(paths.session_dir(it.id))
        if not os.path.isdir(paths.session_dir(it.id)):
            removed += 1
        elif log:
            paths.append_incident("PRUNE-FAIL",
                                  f"could not fully remove {it.id}")
    return removed


def enforce_policy(cfg, exclude=None, cow=True) -> int:
    """Automatic retention pass (run at seal): apply the config's day + disk
    limits, non-flagged only, quietly. `exclude` protects a session id (the one
    just sealed) from being pruned by its own seal. Returns count pruned.

    `cow=False` means the pre-images are full byte copies (no copy-on-write on
    this filesystem), so history costs real disk — we then keep the SHORTER of
    retention_days and fallback_retention_days to stop full copies piling up."""
    keep_days = getattr(cfg, "retention_days", None)
    if not cow:
        fb = getattr(cfg, "fallback_retention_days", None)
        if fb:
            keep_days = min(keep_days, fb) if keep_days else fb
    max_gb = getattr(cfg, "max_disk_gb", None)
    max_bytes = int(max_gb * 1e9) if max_gb else None
    if not keep_days and not max_bytes:
        return 0
    sessions = [s for s in collect() if s.id != exclude]
    items = plan(sessions, keep_days=keep_days, max_disk_bytes=max_bytes)
    return apply(items)
