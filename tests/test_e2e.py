"""End-to-end smoke: arm → mutate project → seal → journal intact →
revert → restored → revert-the-revert → re-applied.

Uses Fakes for snapshot/clone/watch so it's hermetic and fast, but exercises
the REAL session + clone + revert + journal wiring together.
"""
import os
import tempfile
import unittest

from revertly.clone import FakeCloner
from revertly.config import Config
from revertly.journal import Journal
from revertly.model import Event, EventKind, FsOp
from revertly.revert import Reverter
from revertly.session import Session
from revertly.snapshot import FakeSnapshotter
from revertly.watch import FakeWatcher
from revertly import paths


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._proj = tempfile.TemporaryDirectory()
        os.environ["REVERTLY_HOME"] = self._home.name
        os.environ["REVERTLY_NO_HARDEN"] = "1"
        os.environ["REVERTLY_NO_NOTIFY"] = "1"
        self.proj = self._proj.name
        self._write("keep.txt", "unchanged\n")
        self._write("edit.txt", "original\n")
        self._write("gone.txt", "delete me\n")

    def tearDown(self):
        for k in ("REVERTLY_HOME", "REVERTLY_NO_HARDEN", "REVERTLY_NO_NOTIFY"):
            os.environ.pop(k, None)
        self._home.cleanup()
        self._proj.cleanup()

    def _write(self, rel, content):
        with open(os.path.join(self.proj, rel), "w") as f:
            f.write(content)

    def _read(self, rel):
        with open(os.path.join(self.proj, rel)) as f:
            return f.read()

    def test_full_lifecycle(self):
        # 1. arm — captures pre-image via clone
        s = Session(cwd=self.proj, argv=["claude", "refactor"], cfg=Config(),
                    snapshotter=FakeSnapshotter(), cloner=FakeCloner(),
                    watcher=FakeWatcher())
        s.arm()

        # 2. agent mutates the project (and we mirror the events into the journal)
        self._write("edit.txt", "MODIFIED BY AGENT\n")
        os.remove(os.path.join(self.proj, "gone.txt"))
        self._write("new.txt", "created by agent\n")
        for op, rel in [(FsOp.WRITE, "edit.txt"), (FsOp.DELETE, "gone.txt"),
                        (FsOp.CREATE, "new.txt")]:
            s._watcher.emit(Event(kind=EventKind.FS, op=op,
                                  path=os.path.join(self.proj, rel)))

        # 3. seal + journal integrity holds
        s.seal(exit_code=0)
        ok, bad = Journal.verify(paths.journal_path(s.id))
        self.assertTrue(ok, f"journal tampered at {bad}")
        self.assertIn("files touched", s.summary_line())

        # 4. revert the whole session
        r = Reverter(paths.session_dir(s.id))
        plan = r.plan()
        self.assertTrue(any(c.path.endswith("edit.txt") for c in plan.restores))
        self.assertTrue(any(c.path.endswith("gone.txt") for c in plan.restores))
        self.assertTrue(any(c.path.endswith("new.txt") for c in plan.deletes))
        revert_id = r.apply(plan)

        # 5. project restored to pre-session state
        self.assertEqual(self._read("edit.txt"), "original\n")
        self.assertTrue(os.path.exists(os.path.join(self.proj, "gone.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "new.txt")))
        self.assertEqual(self._read("keep.txt"), "unchanged\n")

        # 6. revert-the-revert — agent's work comes back (non-destructive)
        r2 = Reverter(paths.session_dir(revert_id))
        r2.apply(r2.plan())
        self.assertEqual(self._read("edit.txt"), "MODIFIED BY AGENT\n")
        self.assertTrue(os.path.exists(os.path.join(self.proj, "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.proj, "gone.txt")))


if __name__ == "__main__":
    unittest.main()
