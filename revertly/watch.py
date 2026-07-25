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
            thread.join(timeout=max(self.interval * 4, 2.0))
            if thread.is_alive():
                # join timed out mid-scan; running the sweep now would race
                # the live thread on self._prev and could emit duplicates or
                # write after the journal seals. Skip it — the poll thread's
                # own next diff covers the window.
                self._thread = None
                return
        self._thread = None
        # Final catch-up sweep: a session shorter than one poll interval (or
        # changes landing between the last poll and stop) must still be
        # journaled — the session seals the journal right after stopping us.
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
        current = self._scan()
        prev = self._prev

        for path, meta in current.items():
            old = prev.get(path)
            if old is None:
                self._emit(FsOp.CREATE, path)
            elif old != meta:
                self._emit(FsOp.WRITE, path)

        for path in prev:
            if path not in current:
                self._emit(FsOp.DELETE, path)

        self._prev = current

    def _emit(self, op: FsOp, path: str) -> None:
        if self._on_event is None:
            return
        self._on_event(Event(kind=EventKind.FS, op=op, path=path, t=time.time()))
