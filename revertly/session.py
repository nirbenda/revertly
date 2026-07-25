"""Session lifecycle: arm() the safety net, then seal() it.

Ordering invariant (TECH-DESIGN #1): arm() completes snapshot -> clone ->
journal-open -> watcher-start BEFORE the wrapped command is ever exec'd.
Fail-closed invariant (#2): if arming fails, honor cfg.on_arm_failure.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from . import paths
from .config import Config
from .journal import Journal
from .model import Event, EventKind, FsOp, Severity, SessionMeta
from .tripwire import TripwireEngine


class ArmError(RuntimeError):
    pass


class Session:
    def __init__(self, cwd, argv, cfg: Optional[Config] = None, *,
                 snapshotter=None, cloner=None, watcher=None, name: Optional[str] = None):
        self.cwd = os.path.abspath(cwd)
        self.argv = list(argv)
        self.cfg = cfg or Config()
        self.name = name or _derive_name(self.argv)
        self.id = paths.new_session_id(self.name)
        self._snapshotter = snapshotter
        self._cloner = cloner
        self._watcher = watcher          # explicit single watcher (tests) or None
        self._watchers = []              # active watchers (1 project + N tripwire roots)
        self._journal: Optional[Journal] = None
        self._tripwire = TripwireEngine(self.cfg)
        self.meta: Optional[SessionMeta] = None

    # ─────────────────────── arm ───────────────────────

    def arm(self) -> SessionMeta:
        paths.ensure_store()
        sdir = paths.ensure_dir(paths.session_dir(self.id))
        paths.ensure_dir(paths.versions_dir(self.id))

        snapshotter = self._snapshotter or _default_snapshotter()
        cloner = self._cloner or _default_cloner()

        snap_name = None
        clone_ok = False
        try:
            if snapshotter.can_snapshot():
                snap_name = snapshotter.create()
            cloner.clone_tree(self.cwd, paths.clone_dir(self.id))
            clone_ok = True
        except Exception as exc:  # arming failed
            self._handle_arm_failure(exc)
            # if we get here, policy was 'proceed': continue unarmed

        self._journal = Journal(paths.journal_path(self.id))
        self._journal.heartbeat()

        if self._watcher is not None:
            # explicit injection (tests): honor the single watcher as-is
            self._watcher.start(self._resolve_scope(), self._on_event)
            self._watchers = [self._watcher]
        else:
            # project dir (recursive) + a bounded set of sensitive tripwire roots
            for root in self._watch_roots():
                w = _make_watcher(self.cfg)
                w.start(root, self._on_event)
                self._watchers.append(w)

        self.meta = SessionMeta(
            id=self.id, name=self.name, cwd=self.cwd, argv=self.argv,
            started=time.time(), snapshot=snap_name,
            clone_path=paths.clone_dir(self.id), armed=clone_ok,
        )
        self._save_meta()
        return self.meta

    def _resolve_scope(self) -> str:
        scope = self.cfg.watch_scope
        if scope in (".", "", None):
            return self.cwd
        return os.path.expanduser(scope)

    def _watch_roots(self) -> list:
        """Project dir plus small, concrete sensitive roots that already exist.

        Whole-$HOME polling is infeasible; instead we watch the project (the
        95% case) and stat-poll the finite set of sensitive dirs/files that an
        injection targets. Large system trees (e.g. /etc) are intentionally not
        polled in Phase 1 — that's the real-FSEvents/fanotify backend's job.
        """
        roots = [self._resolve_scope()]
        for base in ("~/.ssh", "~/.aws", "~/.config/gh", "~/.gnupg",
                     "~/Library/LaunchAgents", "~/Library/LaunchDaemons"):
            p = os.path.expanduser(base)
            if os.path.isdir(p) and p not in roots:
                roots.append(p)
        return roots

    def _handle_arm_failure(self, exc: Exception):
        policy = self.cfg.on_arm_failure
        if policy == "proceed":
            return
        # 'abort' and 'ask' both refuse in a non-interactive context.
        raise ArmError(f"failed to arm safety net ({exc}); policy={policy}") from exc

    # ─────────────────────── event handling ───────────────────────

    def _on_event(self, e: Event):
        """Watcher callback. Enrich fs events with tripwire classification."""
        if e.path and self.cfg.is_excluded(e.path):
            return
        if e.path and e.op:
            hit = self._tripwire.check(e.path, e.op)
            if hit:
                e.kind = EventKind.SELF_TAMPER if hit.self_tamper else EventKind.TRIPWIRE
                e.severity = Severity.ALERT
                e.detail = f"matched {hit.pattern}"
                self._notify(e, hit)
        if self._journal:
            self._journal.append(e)

    def _notify(self, e: Event, hit):
        """Surface a tripwire immediately: stderr line + cross-session incident
        log + a real macOS notification. The point is that an attack (or a
        hallucinated dotfile edit) *announces itself* the instant it happens."""
        tag = "SELF-TAMPER" if hit.self_tamper else "TRIPWIRE"
        op = e.op.value if e.op else "?"
        msg = f"revertly ⚠ {tag}: {op} {e.path} ({hit.pattern})"
        try:
            import sys
            print(msg, file=sys.stderr)
        except Exception:
            pass
        self._append_incident(tag, op, e.path, hit.pattern)
        self._desktop_notify(tag, f"{op} {e.path}")

    def _append_incident(self, tag, op, path, pattern):
        try:
            line = (f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{self.id}\t{tag}\t"
                    f"{op}\t{path}\t{pattern}\n")
            with open(paths.incidents_log(), "a") as f:
                f.write(line)
        except Exception:
            pass

    def _desktop_notify(self, title, body):
        # best-effort; never block or raise. macOS only.
        if os.environ.get("REVERTLY_NO_NOTIFY"):
            return
        try:
            import shutil as _sh
            import subprocess
            if _sh.which("osascript"):
                safe = body.replace('"', "'")[:200]
                subprocess.Popen(
                    ["osascript", "-e",
                     f'display notification "{safe}" with title "revertly: {title}"'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    # ─────────────────────── seal ───────────────────────

    def seal(self, exit_code: Optional[int] = None) -> SessionMeta:
        for w in self._watchers:
            try:
                w.stop()
            except Exception:
                pass
        if self._journal:
            self._journal.heartbeat()
        if self.meta:
            self.meta.ended = time.time()
            self.meta.exit_code = exit_code
            self._save_meta()
        # Seal for real: make the evidence tamper-RAISING. A same-UID attacker
        # must now `chflags nouchg` before editing/removing — an extra, visible
        # step, and the hash chain still catches content edits either way.
        paths.make_immutable(paths.journal_path(self.id))
        paths.make_immutable(paths.meta_path(self.id))
        return self.meta

    def summary_line(self) -> str:
        events = Journal.read(paths.journal_path(self.id))
        fs = [e for e in events if e.kind == EventKind.FS]
        trips = [e for e in events if e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER)]
        outside = [e for e in fs if e.path and not e.path.startswith(self.cwd)]
        parts = [f"{len(fs)} files touched"]
        if outside:
            parts.append(f"{len(outside)} outside project ⚠")
        if trips:
            parts.append(f"{len(trips)} tripwire(s) ⚠")
        return (f"revertly: {', '.join(parts)} — 'revertly last' to inspect, "
                f"'revertly revert {self.id}' to undo")

    # ─────────────────────── persistence ───────────────────────

    def _save_meta(self):
        if not self.meta:
            return
        with open(paths.meta_path(self.id), "w") as f:
            json.dump(self.meta.to_json_dict(), f, indent=2)


def _derive_name(argv) -> str:
    # first non-flag argument after the command, truncated + slugged
    for a in argv[1:]:
        if not a.startswith("-"):
            slug = "-".join(a.lower().split())[:40]
            return "".join(c for c in slug if c.isalnum() or c in "-_") or "session"
    return "session"


# ─────────────────────── default backends ───────────────────────

def _default_snapshotter():
    # REVERTLY_NO_SNAPSHOT=1 skips the (privileged, side-effecting) APFS snapshot —
    # useful for CI, tests, and users who rely on the clone layer alone.
    if os.environ.get("REVERTLY_NO_SNAPSHOT"):
        from .snapshot import FakeSnapshotter
        return FakeSnapshotter(can=False)  # can_snapshot() -> False, no snapshot taken
    from .snapshot import TmutilSnapshotter
    return TmutilSnapshotter()


def _default_cloner():
    from .clone import ClonefileCloner
    return ClonefileCloner()


def _make_watcher(cfg: Config):
    from .watch import PollingWatcher
    def ignore(path):
        return cfg.is_excluded(path) or path.startswith(paths.revertly_home())
    return PollingWatcher(interval=cfg.poll_interval, should_ignore=ignore)
