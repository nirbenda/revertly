"""revertly.watch — dependency-free filesystem watchers.

Two implementations of the Watcher interface:

  * PollingWatcher — the real, portable backend. Diffs periodic snapshots of
    a directory tree (path -> (mtime, size)) and emits FS Events for created,
    written, and deleted paths. No third-party deps (no FSEvents/inotify).
  * FakeWatcher — a test double that lets a test push Events by hand.

Callers/sessions are responsible for high-level excludes; the watcher accepts
an optional `should_ignore(path) -> bool` to skip individual paths cheaply.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Dict, Optional, Tuple

from revertly.model import Event, EventKind, FsOp

OnEvent = Callable[[Event], None]
Snapshot = Dict[str, Tuple[float, int]]


class Watcher:
    """Abstract watcher interface."""

    def start(self, root: str, on_event: OnEvent) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class FakeWatcher(Watcher):
    """Test double: records lifecycle state and delivers manually-emitted events."""

    def __init__(self):
        self.started = False
        self.stopped = False
        self.root: Optional[str] = None
        self._on_event: Optional[OnEvent] = None

    def start(self, root: str, on_event: OnEvent) -> None:
        self.started = True
        self.stopped = False
        self.root = root
        self._on_event = on_event

    def emit(self, event: Event) -> None:
        if self._on_event is not None:
            self._on_event(event)

    def stop(self) -> None:
        self.started = False
        self.stopped = True


class PollingWatcher(Watcher):
    """Polls a directory tree and emits create/write/delete FS events."""

    def __init__(self, interval: float = 0.5,
                 should_ignore: Optional[Callable[[str], bool]] = None):
        self.interval = interval
        self._should_ignore = should_ignore
        self._on_event: Optional[OnEvent] = None
        self._root: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._prev: Snapshot = {}
        # Serializes _diff_and_emit so the poll thread and stop()'s final
        # sweep can never run concurrently on _prev (no double-emit, no lost
        # generation) even if join() times out on a slow scan.
        self._diff_lock = threading.Lock()

    def start(self, root: str, on_event: OnEvent) -> None:
        self._root = root
        self._on_event = on_event
        self._stop.clear()
        # Baseline snapshot: pre-existing files are not reported as CREATE.
        self._prev = self._scan()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # Wait for the poll thread to actually exit. A slow scan on a big
            # tree can exceed one interval, so retry the join a few times
            # rather than abandoning the thread (which would lose the final
            # window's events, including tripwire alerts). _run() checks _stop
            # right after each diff, so this terminates promptly once the
            # in-flight scan finishes.
            deadline = max(self.interval * 20, 10.0)
            waited = 0.0
            step = max(self.interval, 0.2)
            while thread.is_alive() and waited < deadline:
                thread.join(timeout=step)
                waited += step
        self._thread = None
        # Final catch-up sweep: a session shorter than one poll interval (or
        # changes landing between the last poll and stop) must still be
        # journaled — the session seals the journal right after stopping us.
        # _diff_lock makes this safe even in the rare case the thread is still
        # alive: the two calls serialize, so neither double-emits.
        try:
            self._diff_and_emit()
        except OSError:
            pass

    # ---- internals ----

    def _ignored(self, path: str) -> bool:
        return self._should_ignore is not None and self._should_ignore(path)

    def _scan(self) -> Snapshot:
        snap: Snapshot = {}
        root = self._root
        if not root or not os.path.exists(root):
            return snap
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune ignored directories so we never descend into them.
            if self._should_ignore is not None:
                dirnames[:] = [d for d in dirnames
                               if not self._ignored(os.path.join(dirpath, d))]
            for name in filenames:
                p = os.path.join(dirpath, name)
                if self._ignored(p):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    # File vanished mid-scan; skip it.
                    continue
                snap[p] = (st.st_mtime, st.st_size)
        return snap

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                self._diff_and_emit()
            except OSError:
                # Be robust to transient FS errors during a scan.
                continue

    def _diff_and_emit(self) -> None:
        with self._diff_lock:
            self._diff_and_emit_locked()

    def _diff_and_emit_locked(self) -> None:
        current = self._scan()
        prev = self._prev

        created = [p for p in current if p not in prev]
        deleted = [p for p in prev if p not in current]
        renames = self._pair_renames(created, deleted, prev, current)

        for new_path, old_path in renames.items():
            self._emit(FsOp.RENAME, new_path, path_from=old_path)
        for path in created:
            if path not in renames:
                self._emit(FsOp.CREATE, path)
        for path, meta in current.items():
            old = prev.get(path)
            if old is not None and old != meta:
                self._emit(FsOp.WRITE, path)
        renamed_from = set(renames.values())
        for path in deleted:
            if path not in renamed_from:
                self._emit(FsOp.DELETE, path)

        self._prev = current

    @staticmethod
    def _pair_renames(created, deleted, prev: Snapshot,
                      current: Snapshot) -> Dict[str, str]:
        """Infer renames: a move keeps the inode, so (mtime, size) survive
        exactly. Pair a created path with a deleted one ONLY when the
        signature match is one-to-one — ambiguity falls back to
        create+delete, never a guessed rename. Returns {new: old}.
        """
        del_by_sig: Dict[Tuple[float, int], list] = {}
        for p in deleted:
            del_by_sig.setdefault(prev[p], []).append(p)
        cre_by_sig: Dict[Tuple[float, int], list] = {}
        for p in created:
            cre_by_sig.setdefault(current[p], []).append(p)
        renames: Dict[str, str] = {}
        for sig, news in cre_by_sig.items():
            olds = del_by_sig.get(sig, [])
            if len(news) == 1 and len(olds) == 1:
                renames[news[0]] = olds[0]
        return renames

    def _emit(self, op: FsOp, path: str, path_from: Optional[str] = None) -> None:
        if self._on_event is None:
            return
        self._on_event(Event(kind=EventKind.FS, op=op, path=path,
                             path_from=path_from, t=time.time()))
