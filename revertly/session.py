"""Session lifecycle: arm() the safety net, then seal() it.

Ordering invariant (TECH-DESIGN #1): arm() completes snapshot -> clone ->
journal-open -> watcher-start BEFORE the wrapped command is ever exec'd.
Fail-closed invariant (#2): if arming fails, honor cfg.on_arm_failure.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from typing import Optional

from . import paths
from .config import Config
from .journal import Journal
from .model import Event, EventKind, FsOp, Severity, SessionMeta
from .tripwire import TripwireEngine


class ArmError(RuntimeError):
    def __init__(self, message: str, policy: str = "abort"):
        super().__init__(message)
        self.policy = policy


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
        self._warn_if_blinded()

        snapshotter = self._snapshotter or _default_snapshotter()
        cloner = self._cloner or _default_cloner()

        snap_name = None
        clone_ok = False
        try:
            if snapshotter.can_snapshot():
                snap_name = snapshotter.create()
            t0 = time.time()
            cloner.clone_tree(self.cwd, paths.clone_dir(self.id))
            self._prune_clone(paths.clone_dir(self.id))
            clone_ok = True
            dt = time.time() - t0
            if dt > 1.0:
                # arming blocks the wrapped command's start; a slow clone means
                # a big/non-CoW tree. Tell the user so it isn't a mystery hang,
                # and hint at excludes.
                import sys as _sys
                print(f"revertly: clone took {dt:.1f}s — large project. Add big "
                      f"regenerable dirs to [watch] exclude in "
                      f"{paths.config_path()} to speed this up.", file=_sys.stderr)
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
            # dedicated SELF_TAMPER sentinels: watch revertly's OWN state so
            # disabling/blinding/destroying it is no longer silent.
            for w in self._self_tamper_watchers():
                self._watchers.append(w)

        self.meta = SessionMeta(
            id=self.id, name=self.name, cwd=self.cwd, argv=self.argv,
            started=time.time(), snapshot=snap_name,
            clone_path=paths.clone_dir(self.id), armed=clone_ok,
        )
        self._save_meta()
        return self.meta

    def _prune_clone(self, clone_root: str) -> None:
        """Remove excluded paths (node_modules, .git, .claude, venvs, caches)
        from the freshly-made clone, keyed on the LOGICAL project path so the
        store isn't bloated with regenerable trees and the pre-image never
        includes anything the watcher also ignores (keeping revert consistent).
        On APFS the clone is CoW so this is cheap; it also bounds the non-APFS
        full-copy footprint."""
        for dirpath, dirnames, filenames in os.walk(clone_root, topdown=True):
            rel = os.path.relpath(dirpath, clone_root)
            base = self.cwd if rel == "." else os.path.join(self.cwd, rel)
            keep = []
            for d in dirnames:
                if self.cfg.is_excluded(os.path.join(base, d)):
                    shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                else:
                    keep.append(d)
            dirnames[:] = keep
            for fn in filenames:
                if self.cfg.is_excluded(os.path.join(base, fn)):
                    try:
                        os.remove(os.path.join(dirpath, fn))
                    except OSError:
                        pass

    def _warn_if_blinded(self) -> None:
        """A config that broadens exclude to everything or empties the tripwire
        set means the watcher is blind — announce it at arm time (was only
        surfaced by a manual `revertly doctor`)."""
        risky = self.cfg.risky_excludes()
        if risky:
            msg = f"config exclude is dangerously broad {risky} — watcher blinded"
            print(f"revertly ⚠ {msg}", file=__import__("sys").stderr)
            paths.append_incident("CONFIG", msg)
        if self.cfg.tripwires_weakened():
            msg = "sensitive-path tripwires are EMPTY in config"
            print(f"revertly ⚠ {msg}", file=__import__("sys").stderr)
            paths.append_incident("CONFIG", msg)

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

    def _self_tamper_watchers(self) -> list:
        """Start watchers on revertly's own sentinel state. These bypass the
        revertly-home exclude the project watcher uses, but are kept to a
        tiny, self-noise-free set (revertly never writes these DURING a
        session, so they never flag our own activity):

          * this session's clone/  — the revert source (delete = sabotage)
          * bin/                    — the shim (removal/replacement)
          * store root: only config.toml + paused (blind/disable sentinels)
          * $HOME: only the shell-rc files (persistence + PATH hijack)
        """
        started = []
        home = paths.revertly_home()
        userhome = os.path.expanduser("~")
        # full-watch roots revertly doesn't write during a session
        for root in (paths.clone_dir(self.id), paths.bin_dir()):
            if os.path.isdir(root):
                w = _make_plain_watcher(self.cfg)
                w.start(root, self._on_event)
                started.append(w)
        # allowlist watchers: scan a dir but only report specific sentinels
        store_allow = {paths.config_path(), os.path.join(home, "paused")}
        rc_allow = {os.path.join(userhome, n) for n in
                    (".zshrc", ".zprofile", ".zshenv",
                     ".bashrc", ".bash_profile", ".profile")}
        for root, allow in ((home, store_allow), (userhome, rc_allow)):
            if os.path.isdir(root):
                w = _make_allowlist_watcher(allow, self.cfg.poll_interval)
                w.start(root, self._on_event)
                started.append(w)
        return started

    def _handle_arm_failure(self, exc: Exception):
        policy = self.cfg.on_arm_failure
        if policy == "proceed":
            return
        # 'abort' and 'ask' both refuse in a non-interactive context. The
        # policy travels on the exception so the shim can honor it (abort =
        # do NOT run the wrapped command) instead of always proceeding.
        raise ArmError(f"failed to arm safety net ({exc}); policy={policy}",
                       policy=policy) from exc

    # ─────────────────────── event handling ───────────────────────

    def _on_event(self, e: Event):
        """Watcher callback. Enrich fs events with tripwire classification.

        Order matters: classify FIRST, then apply the exclude filter only to
        non-tripwire events. Otherwise a SELF_TAMPER/TRIPWIRE hit on a path
        that also matches an exclude glob (e.g. ~/.revertly/**) would be
        silently dropped — which is exactly how self-defense was dead before.
        """
        hit = self._tripwire.check(e.path, e.op) if (e.path and e.op) else None
        if hit is None and e.path and self.cfg.is_excluded(e.path):
            return  # ordinary excluded churn — drop
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
        # on_write = "log" -> journal + stderr + incident, but no desktop popup.
        # SELF_TAMPER always pops (it means someone is disabling revertly).
        if hit.self_tamper or self.cfg.tripwire_on_write != "log":
            self._desktop_notify(tag, f"{op} {e.path}")

    def _append_incident(self, tag, op, path, pattern):
        # one incident schema across the codebase: ts \t session \t tag \t detail
        # (paths.append_incident writes the same 4 columns with a '-' session).
        paths.append_incident(tag, f"{op} {path} ({pattern})",
                              session_id=self.id)

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
            # Immutable truncation anchor: record the final seq/hash so verify()
            # can detect a clean-prefix cut (which the chain alone accepts).
            try:
                with open(paths.seal_path(self.id), "w") as f:
                    json.dump({"seq": self._journal._last_seq,
                               "hash": self._journal._last_hash}, f)
            except OSError:
                pass
        if self.meta:
            self.meta.ended = time.time()
            self.meta.exit_code = exit_code
            # record evidence status so retention/status don't rescan journals
            events = Journal.read(paths.journal_path(self.id))
            self.meta.flagged = any(
                e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER)
                for e in events)
            self._save_meta()
        # Seal for real: make the evidence tamper-RAISING. A same-UID attacker
        # must now `chflags nouchg` before editing/removing — an extra, visible
        # step, and the hash chain still catches content edits either way.
        paths.make_immutable(paths.journal_path(self.id))
        paths.make_immutable(paths.meta_path(self.id))
        paths.make_immutable(paths.seal_path(self.id))
        # Automatic retention: enforce the config's day/disk limits now that a
        # session just landed. Exclude THIS session so a long run that just
        # sealed can't be pruned by its own seal. Best-effort — a failure here
        # is logged but never blocks or breaks sealing.
        try:
            from . import retention
            retention.enforce_policy(self.cfg, exclude=self.id)
        except Exception as exc:
            paths.append_incident("RETENTION-FAIL", f"enforce at seal: {exc}")
        return self.meta

    def summary_line(self) -> str:
        events = Journal.read(paths.journal_path(self.id))
        fs = [e for e in events if e.kind == EventKind.FS]
        trips = [e for e in events if e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER)]
        outside = [e for e in fs if e.path and not paths.is_under(e.path, self.cwd)]
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
    home = paths.revertly_home()

    def ignore(path):
        return cfg.is_excluded(path) or paths.is_under(path, home)
    return PollingWatcher(interval=cfg.poll_interval, should_ignore=ignore)


def _make_plain_watcher(cfg: Config):
    """A watcher with no ignore — used for sentinel roots (clone/, bin/) that
    revertly does not write during a session, so there is nothing to ignore."""
    from .watch import PollingWatcher
    return PollingWatcher(interval=cfg.poll_interval)


def _make_allowlist_watcher(allowed: set, interval: float = 0.5):
    """A watcher that prunes ALL subdirectories (no descent) and reports only
    the explicit sentinel files in `allowed`. Cheap: one listing of the root,
    stat of a handful of files. Used for the store root (config.toml, paused)
    and $HOME (shell-rc files) so those specific paths are watched without
    journaling everything around them."""
    from .watch import PollingWatcher
    allowed = {os.path.abspath(p) for p in allowed}

    def ignore(path):
        ap = os.path.abspath(path)
        if os.path.isdir(ap):
            return True                    # never descend
        return ap not in allowed           # only the sentinels are reported
    return PollingWatcher(interval=interval, should_ignore=ignore)
