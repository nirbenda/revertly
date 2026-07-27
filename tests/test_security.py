"""Security hardening tests: tamper-detection, immutability, incident log,
config-weakening detection."""
import os
import sys
import tempfile
import unittest

_DARWIN_ONLY = unittest.skipUnless(
    sys.platform == "darwin",
    "user-immutable flags (chflags UF_IMMUTABLE) are macOS-only in Phase 1")

from revertly import paths
from revertly.config import Config
from revertly.journal import Journal
from revertly.model import Event, EventKind, FsOp
from revertly.session import Session
from revertly.snapshot import FakeSnapshotter
from revertly.clone import FakeCloner
from revertly.watch import FakeWatcher


class SecBase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._proj = tempfile.TemporaryDirectory()
        os.environ["REVERTLY_HOME"] = self._home.name
        os.environ["REVERTLY_NO_NOTIFY"] = "1"
        with open(os.path.join(self._proj.name, "a.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        # clear any immutable flags so temp cleanup succeeds
        paths.rmtree_force(self._home.name)
        for k in ("REVERTLY_HOME", "REVERTLY_NO_NOTIFY", "REVERTLY_NO_HARDEN"):
            os.environ.pop(k, None)
        self._proj.cleanup()

    def _run_session(self, emit=None):
        s = Session(cwd=self._proj.name, argv=["claude", "go"], cfg=Config(),
                    snapshotter=FakeSnapshotter(), cloner=FakeCloner(),
                    watcher=FakeWatcher())
        s.arm()
        if emit:
            for e in emit:
                s._watcher.emit(e)
        s.seal(0)
        return s


class TestImmutability(SecBase):
    @_DARWIN_ONLY
    def test_journal_immutable_after_seal(self):
        s = self._run_session()
        jp = paths.journal_path(s.id)
        self.assertTrue(paths.is_immutable(jp),
                        "sealed journal should be user-immutable")

    def test_no_harden_env_disables_immutability(self):
        # cross-platform: with hardening off the journal is never immutable
        # (and on Linux it's never immutable regardless — chflags is a no-op).
        os.environ["REVERTLY_NO_HARDEN"] = "1"
        s = self._run_session()
        self.assertFalse(paths.is_immutable(paths.journal_path(s.id)))

    @_DARWIN_ONLY
    def test_immutable_blocks_naive_overwrite(self):
        s = self._run_session()
        jp = paths.journal_path(s.id)
        with self.assertRaises(OSError):
            with open(jp, "w") as f:      # truncate should be denied by uchg
                f.write("x")

    @_DARWIN_ONLY
    def test_rmtree_force_removes_immutable(self):
        s = self._run_session()
        sdir = paths.session_dir(s.id)
        self.assertTrue(paths.is_immutable(paths.journal_path(s.id)))
        paths.rmtree_force(sdir)
        self.assertFalse(os.path.exists(sdir))


class TestTamperDetection(SecBase):
    def test_verify_detects_edited_journal(self):
        os.environ["REVERTLY_NO_HARDEN"] = "1"   # so we can edit it to simulate attack
        s = self._run_session(emit=[
            Event(kind=EventKind.FS, op=FsOp.WRITE,
                  path=os.path.join(self._proj.name, "a.txt"))])
        jp = paths.journal_path(s.id)
        self.assertTrue(Journal.verify(jp)[0])
        # tamper: change a field value in a record WITHOUT recomputing its hash,
        # keeping the line valid JSON — the hash check must catch it.
        import json
        with open(jp) as f:
            lines = f.read().splitlines()
        self.assertGreaterEqual(len(lines), 2)
        rec = json.loads(lines[0])
        rec["path"] = "/evil/injected/path"          # forged content, stale hash
        lines[0] = json.dumps(rec)
        with open(jp, "w") as f:
            f.write("\n".join(lines) + "\n")
        ok2, seq = Journal.verify(jp)
        self.assertFalse(ok2, "verify must detect the forged record")
        self.assertEqual(seq, 0)

    def test_verify_detects_garbled_line(self):
        os.environ["REVERTLY_NO_HARDEN"] = "1"
        s = self._run_session()
        jp = paths.journal_path(s.id)
        with open(jp, "a") as f:
            f.write("}{ not json at all\n")           # corruption, not a crash
        ok, _ = Journal.verify(jp)
        self.assertFalse(ok)


class TestIncidentLog(SecBase):
    def test_tripwire_writes_incident(self):
        cred = os.path.expanduser("~/.ssh/id_rsa")
        self._run_session(emit=[Event(kind=EventKind.FS, op=FsOp.READ, path=cred)])
        ilog = paths.incidents_log()
        self.assertTrue(os.path.exists(ilog))
        content = open(ilog).read()
        self.assertIn("TRIPWIRE", content)
        self.assertIn(".ssh", content)

    def test_self_tamper_writes_incident(self):
        tamper = os.path.expanduser("~/.zshrc")
        self._run_session(emit=[Event(kind=EventKind.FS, op=FsOp.WRITE, path=tamper)])
        content = open(paths.incidents_log()).read()
        self.assertIn("SELF-TAMPER", content)


class TestConfigWeakening(unittest.TestCase):
    def test_broad_exclude_flagged(self):
        c = Config(); c.exclude = ["~/**"]
        self.assertTrue(c.risky_excludes())

    def test_star_exclude_flagged(self):
        c = Config(); c.exclude = ["**"]
        self.assertTrue(c.risky_excludes())

    def test_normal_exclude_ok(self):
        c = Config()  # defaults
        self.assertFalse(c.risky_excludes())

    def test_emptied_tripwires_detected(self):
        c = Config(); c.tripwire_paths = []
        self.assertTrue(c.tripwires_weakened())

    def test_self_tamper_always_active_even_if_tripwires_empty(self):
        c = Config(); c.tripwire_paths = []
        # SELF_TAMPER globs are still present in the combined set
        self.assertTrue(any(".revertly" in g or "zshrc" in g
                            for g in c.self_tamper_globs()))


if __name__ == "__main__":
    unittest.main()
