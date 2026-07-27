"""Runaway-deletion burst detection + one-shot `revertly undo`.

Covers the detector (sliding window, thresholds, regeneratable-dir skip,
cooldown), the burst record store, the config knobs, the Session wiring, and
an end-to-end restore via cmd_undo against a real clone/journal.
"""
import json
import os
import shutil
import tempfile
import time
import types
import unittest

from revertly import paths
from revertly.config import Config, load
from revertly.model import Event, EventKind, FsOp, SessionMeta
from revertly.session import Session, _BurstDetector, _is_regeneratable


class TestBurstDetector(unittest.TestCase):
    def _det(self, threshold=5, window=3.0, **kw):
        self.trips = []
        cfg = Config(delete_burst_threshold=threshold,
                     delete_burst_window=window, **kw)
        return _BurstDetector(cfg, lambda ts, te, n: self.trips.append((ts, te, n)))

    def test_trips_at_threshold(self):
        d = self._det(threshold=5, window=100)
        for i in range(4):
            d.observe_delete(f"/proj/src/f{i}.py", now=1000 + i)
        self.assertEqual(self.trips, [])          # 4 < 5: no trip yet
        d.observe_delete("/proj/src/f4.py", now=1004)
        self.assertEqual(len(self.trips), 1)      # 5th crosses the line
        ts, te, n = self.trips[0]
        self.assertEqual(n, 5)
        self.assertEqual(ts, 1000)

    def test_window_expiry_prevents_trip(self):
        d = self._det(threshold=3, window=2.0)
        d.observe_delete("/proj/a", now=100.0)
        d.observe_delete("/proj/b", now=101.5)
        d.observe_delete("/proj/c", now=103.0)   # a (100) now outside 2s window
        self.assertEqual(self.trips, [])          # only b,c in window -> 2 < 3

    def test_regeneratable_dirs_never_count(self):
        d = self._det(threshold=3, window=100)
        for i in range(20):
            d.observe_delete(f"/proj/node_modules_x/dist/chunk{i}.js", now=i)
        for i in range(20):
            d.observe_delete(f"/proj/build/out{i}.o", now=i)
        self.assertEqual(self.trips, [])          # all in build/dist -> ignored

    def test_cooldown_avoids_refiring_same_wipe(self):
        d = self._det(threshold=3, window=5.0)
        for i in range(10):
            d.observe_delete(f"/proj/f{i}", now=1000 + i * 0.1)
        # one ongoing wipe of 10 files should fire once, not many times
        self.assertEqual(len(self.trips), 1)

    def test_off_disables(self):
        self.trips = []
        cfg = Config(delete_burst="off", delete_burst_threshold=2)
        d = _BurstDetector(cfg, lambda *a: self.trips.append(a))
        for i in range(10):
            d.observe_delete(f"/proj/f{i}", now=i)
        self.assertEqual(self.trips, [])

    def test_is_regeneratable(self):
        self.assertTrue(_is_regeneratable("/proj/dist/a.js"))
        self.assertTrue(_is_regeneratable("/proj/x/build/y/z.o"))
        self.assertFalse(_is_regeneratable("/proj/src/app.py"))
        self.assertFalse(_is_regeneratable("/proj/README.md"))


class _HomeCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_home_")
        os.environ["REVERTLY_HOME"] = self.home
        paths.ensure_store()

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)
        os.environ.pop("REVERTLY_HOME", None)


class TestBurstRecords(_HomeCase):
    def test_record_list_latest_and_mark_undone(self):
        b1 = paths.record_burst("s1", 100.0, 103.0, 30)
        time.sleep(0.01)
        b2 = paths.record_burst("s2", 200.0, 202.0, 12)
        self.assertIsNotNone(b1)
        self.assertEqual(len(paths.list_bursts()), 2)
        self.assertEqual(paths.latest_open_burst()["id"], b2)  # newest first
        paths.mark_burst_undone(b2, "revert-xyz")
        self.assertEqual(paths.latest_open_burst()["id"], b1)  # b2 now resolved
        rec = next(r for r in paths.list_bursts() if r["id"] == b2)
        self.assertTrue(rec["undone"])
        self.assertEqual(rec["revert_id"], "revert-xyz")

    def test_latest_open_burst_none_when_empty(self):
        self.assertIsNone(paths.latest_open_burst())


class TestConfigBurstKnobs(_HomeCase):
    def test_parses_guard_burst_knobs(self):
        with open(paths.config_path(), "w") as f:
            f.write('[guard]\n'
                    'delete_burst = "off"\n'
                    'delete_burst_threshold = 40\n'
                    'delete_burst_window = 1.5\n')
        cfg = load(paths.config_path())
        self.assertEqual(cfg.delete_burst, "off")
        self.assertEqual(cfg.delete_burst_threshold, 40)
        self.assertEqual(cfg.delete_burst_window, 1.5)

    def test_garbage_numeric_keeps_default(self):
        with open(paths.config_path(), "w") as f:
            f.write('[guard]\ndelete_burst_threshold = "lots"\n')
        cfg = load(paths.config_path())
        self.assertEqual(cfg.delete_burst_threshold, 25)


class TestSessionWiring(_HomeCase):
    def test_delete_burst_records_and_logs_incident(self):
        proj = tempfile.mkdtemp(prefix="revertly_proj_")
        try:
            cfg = Config(delete_burst_threshold=5, delete_burst_window=100)
            s = Session(cwd=proj, argv=["claude"], cfg=cfg)
            for i in range(5):
                s._on_event(Event(kind=EventKind.FS, op=FsOp.DELETE,
                                  path=os.path.join(proj, f"src/f{i}.py")))
            burst = paths.latest_open_burst()
            self.assertIsNotNone(burst)
            self.assertEqual(burst["session_id"], s.id)
            self.assertGreaterEqual(burst["count"], 5)
            with open(paths.incidents_log()) as f:
                self.assertIn("DELETE_BURST", f.read())
        finally:
            shutil.rmtree(proj, ignore_errors=True)


class TestUndoCommand(_HomeCase):
    """End-to-end: a clone holds the pre-image, the project has the files
    deleted, a journal + burst record describe the burst, and `revertly undo`
    restores every deleted file."""

    def _build(self, sid, deleted):
        proj = tempfile.mkdtemp(prefix="revertly_proj_")
        clone = paths.clone_dir(sid)
        paths.ensure_dir(clone)
        # pre-image (clone) has all files; project has them removed
        for rel, content in deleted.items():
            cp = os.path.join(clone, rel)
            os.makedirs(os.path.dirname(cp) or clone, exist_ok=True)
            with open(cp, "w") as f:
                f.write(content)
        time.sleep(0.02)
        ended = time.time()
        meta = SessionMeta(id=sid, name="rogue", cwd=proj, argv=["claude"],
                           started=ended - 100, ended=ended,
                           clone_path=clone, armed=True)
        with open(paths.meta_path(sid), "w") as f:
            json.dump(meta.to_json_dict(), f)
        # journal of the deletions (what burst_deleted_paths reads)
        t0 = ended - 50
        with open(paths.journal_path(sid), "w") as f:
            for i, rel in enumerate(deleted):
                f.write(json.dumps({"kind": "fs", "op": "delete",
                                    "path": os.path.join(proj, rel),
                                    "t": t0 + i}) + "\n")
        paths.record_burst(sid, t0, t0 + len(deleted), len(deleted))
        return proj

    def _args(self, **kw):
        base = {"list": False, "yes": True, "dry_run": False, "force": False}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_undo_restores_all_deleted_files(self):
        from revertly.cli import cmd_undo
        sid = "2026-07-25T12-00-00_rogue"
        deleted = {"src/a.py": "AAA", "src/b.py": "BBB", "docs/c.md": "CCC"}
        proj = self._build(sid, deleted)
        try:
            for rel in deleted:                      # precondition: gone
                self.assertFalse(os.path.exists(os.path.join(proj, rel)))
            rc = cmd_undo(self._args())
            self.assertEqual(rc, 0)
            for rel, content in deleted.items():     # restored, byte-exact
                p = os.path.join(proj, rel)
                self.assertTrue(os.path.exists(p), rel)
                with open(p) as f:
                    self.assertEqual(f.read(), content)
            # burst is now marked undone -> nothing left to undo
            self.assertIsNone(paths.latest_open_burst())
        finally:
            shutil.rmtree(proj, ignore_errors=True)

    def test_undo_with_no_burst_is_a_noop(self):
        from revertly.cli import cmd_undo
        self.assertEqual(cmd_undo(self._args()), 0)   # nothing recorded

    def test_dry_run_changes_nothing(self):
        from revertly.cli import cmd_undo
        sid = "2026-07-25T13-00-00_rogue"
        proj = self._build(sid, {"src/a.py": "AAA"})
        try:
            cmd_undo(self._args(dry_run=True))
            self.assertFalse(os.path.exists(os.path.join(proj, "src/a.py")))
            self.assertIsNotNone(paths.latest_open_burst())  # still open
        finally:
            shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
