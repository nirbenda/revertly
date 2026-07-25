"""Tests for revertly.watch — Watcher interface, FakeWatcher, PollingWatcher.

Run:  python3 -m unittest tests.test_watch -v

PollingWatcher tests use generous timing tolerances and small poll intervals
to stay responsive without being flaky. The watcher is always stopped in
tearDown to avoid leaking threads across tests.
"""
import os
import tempfile
import threading
import time
import unittest

from revertly.model import Event, EventKind, FsOp
from revertly.watch import Watcher, FakeWatcher, PollingWatcher


class TestFakeWatcher(unittest.TestCase):
    def test_is_a_watcher(self):
        self.assertIsInstance(FakeWatcher(), Watcher)

    def test_start_records_root_and_callback(self):
        w = FakeWatcher()
        seen = []
        w.start("/some/root", seen.append)
        self.assertTrue(w.started)
        self.assertEqual(w.root, "/some/root")

    def test_emit_delivers_event_to_callback(self):
        w = FakeWatcher()
        seen = []
        w.start("/r", seen.append)
        e = Event(kind=EventKind.FS, op=FsOp.CREATE, path="/r/a.txt")
        w.emit(e)
        self.assertEqual(seen, [e])

    def test_stop_flips_state(self):
        w = FakeWatcher()
        w.start("/r", lambda e: None)
        w.stop()
        self.assertFalse(w.started)
        self.assertTrue(w.stopped)


class _Collector:
    """Thread-safe event sink for PollingWatcher tests."""
    def __init__(self):
        self._lock = threading.Lock()
        self.events = []

    def __call__(self, e):
        with self._lock:
            self.events.append(e)

    def snapshot(self):
        with self._lock:
            return list(self.events)

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in self.snapshot():
                if predicate(e):
                    return e
            time.sleep(0.02)
        return None


class TestPollingWatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.collector = _Collector()
        self.watcher = None

    def tearDown(self):
        if self.watcher is not None:
            try:
                self.watcher.stop()
            except Exception:
                pass
        self.tmp.cleanup()

    def _start(self, should_ignore=None):
        self.watcher = PollingWatcher(interval=0.05, should_ignore=should_ignore)
        self.watcher.start(self.root, self.collector)
        return self.watcher

    def test_is_a_watcher(self):
        self.assertIsInstance(PollingWatcher(), Watcher)

    def test_create_write_delete_lifecycle(self):
        self._start()
        p = os.path.join(self.root, "hello.txt")

        with open(p, "w") as f:
            f.write("one")
        ev = self.collector.wait_for(
            lambda e: e.path == p and e.op == FsOp.CREATE)
        self.assertIsNotNone(ev, "expected a CREATE event")
        self.assertEqual(ev.kind, EventKind.FS)

        # Give the mtime a chance to differ, then modify.
        time.sleep(0.05)
        with open(p, "w") as f:
            f.write("two longer content")
        ev = self.collector.wait_for(
            lambda e: e.path == p and e.op == FsOp.WRITE)
        self.assertIsNotNone(ev, "expected a WRITE event")

        os.remove(p)
        ev = self.collector.wait_for(
            lambda e: e.path == p and e.op == FsOp.DELETE)
        self.assertIsNotNone(ev, "expected a DELETE event")

    def test_should_ignore_skips_path(self):
        ignored = os.path.join(self.root, "skip.txt")
        watched = os.path.join(self.root, "keep.txt")
        self._start(should_ignore=lambda pth: os.path.basename(pth) == "skip.txt")

        with open(ignored, "w") as f:
            f.write("nope")
        with open(watched, "w") as f:
            f.write("yes")

        ev = self.collector.wait_for(
            lambda e: e.path == watched and e.op == FsOp.CREATE)
        self.assertIsNotNone(ev, "expected the non-ignored file's CREATE")

        # The ignored path must never appear in any event.
        self.assertFalse(
            any(e.path == ignored for e in self.collector.snapshot()),
            "ignored path should not produce events")

    def test_stop_joins_thread(self):
        self._start()
        self.watcher.stop()
        # After stop, the background thread should not be alive.
        thread = getattr(self.watcher, "_thread", None)
        if thread is not None:
            self.assertFalse(thread.is_alive())

    def test_stop_emits_changes_since_last_poll(self):
        # Regression: a session shorter than one poll interval must still
        # journal its changes — stop() performs a final catch-up sweep.
        pre = os.path.join(self.root, "pre.txt")
        with open(pre, "w") as f:
            f.write("existed before start")
        self.watcher = PollingWatcher(interval=30.0)  # poll will never fire
        self.watcher.start(self.root, self.collector)

        os.remove(pre)
        created = os.path.join(self.root, "made.txt")
        with open(created, "w") as f:
            f.write("born and never polled")
        self.watcher.stop()

        events = self.collector.snapshot()
        self.assertTrue(
            any(e.path == pre and e.op == FsOp.DELETE for e in events),
            "stop() must emit the un-polled DELETE")
        self.assertTrue(
            any(e.path == created and e.op == FsOp.CREATE for e in events),
            "stop() must emit the un-polled CREATE")

    def test_events_carry_timestamp(self):
        self._start()
        p = os.path.join(self.root, "t.txt")
        with open(p, "w") as f:
            f.write("x")
        ev = self.collector.wait_for(lambda e: e.path == p)
        self.assertIsNotNone(ev)
        self.assertGreater(ev.t, 0.0)


if __name__ == "__main__":
    unittest.main()
