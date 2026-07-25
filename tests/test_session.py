"""Integration tests for the session lifecycle (arm/seal + event enrichment)."""
import os
import tempfile
import unittest

from revertly.clone import FakeCloner
from revertly.config import Config
from revertly.journal import Journal
from revertly.model import Event, EventKind, FsOp
from revertly.session import Session, ArmError
from revertly.snapshot import FakeSnapshotter
from revertly.watch import FakeWatcher
from revertly import paths


class SessionBase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory()
        self._proj = tempfile.TemporaryDirectory()
        os.environ["REVERTLY_HOME"] = self._home.name
        os.environ["REVERTLY_NO_HARDEN"] = "1"   # keep temp dirs deletable
        os.environ["REVERTLY_NO_NOTIFY"] = "1"   # no desktop popups in tests
        # seed a project file so the clone has content
        with open(os.path.join(self._proj.name, "a.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        for k in ("REVERTLY_HOME", "REVERTLY_NO_HARDEN", "REVERTLY_NO_NOTIFY"):
            os.environ.pop(k, None)
        self._home.cleanup()
        self._proj.cleanup()

    def _mk(self, cfg=None):
        return Session(
            cwd=self._proj.name, argv=["claude", "do a thing"],
            cfg=cfg or Config(),
            snapshotter=FakeSnapshotter(), cloner=FakeCloner(), watcher=FakeWatcher(),
        )


class TestArmSeal(SessionBase):
    def test_arm_captures_snapshot_clone_journal_watcher(self):
        s = self._mk()
        meta = s.arm()
        self.assertTrue(meta.armed)
        self.assertTrue(s._snapshotter.created)               # snapshot taken
        self.assertTrue(os.path.exists(paths.clone_dir(s.id)))  # clone made
        self.assertTrue(os.path.exists(os.path.join(paths.clone_dir(s.id), "a.txt")))
        self.assertTrue(os.path.exists(paths.journal_path(s.id)))  # journal opened
        self.assertTrue(s._watcher.started)                    # watcher armed
        self.assertTrue(os.path.exists(paths.meta_path(s.id)))

    def test_seal_records_end_and_stops_watcher(self):
        s = self._mk()
        s.arm()
        meta = s.seal(exit_code=0)
        self.assertIsNotNone(meta.ended)
        self.assertEqual(meta.exit_code, 0)
        self.assertTrue(s._watcher.stopped)

    def test_arm_failure_abort_policy_raises(self):
        cfg = Config(); cfg.on_arm_failure = "abort"
        cloner = FakeCloner()
        def boom(src, dst):
            raise OSError("disk full")
        cloner.clone_tree = boom
        s = Session(cwd=self._proj.name, argv=["claude"], cfg=cfg,
                    snapshotter=FakeSnapshotter(), cloner=cloner, watcher=FakeWatcher())
        with self.assertRaises(ArmError) as ctx:
            s.arm()
        self.assertEqual(ctx.exception.policy, "abort")


class TestCloneExcludes(SessionBase):
    def test_excluded_trees_not_cloned(self):
        # .git, .claude, node_modules must NOT land in the pre-image (H2).
        for rel in (".git/refs/main", ".claude/settings.json",
                    "node_modules/pkg/i.js", "src/app.py"):
            p = os.path.join(self._proj.name, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write("x")
        s = self._mk()
        s.arm()
        clone = paths.clone_dir(s.id)
        self.assertTrue(os.path.exists(os.path.join(clone, "src", "app.py")))
        self.assertFalse(os.path.exists(os.path.join(clone, ".git")))
        self.assertFalse(os.path.exists(os.path.join(clone, ".claude")))
        self.assertFalse(os.path.exists(os.path.join(clone, "node_modules")))


class TestStoreHardening(SessionBase):
    def test_store_root_is_0700(self):
        del os.environ["REVERTLY_NO_HARDEN"]      # exercise real hardening
        try:
            paths.ensure_store()
            import stat as _stat
            mode = _stat.S_IMODE(os.stat(paths.revertly_home()).st_mode)
            self.assertEqual(mode, 0o700,
                             "store root must be 0700 so other users can't "
                             "traverse into clones holding secrets")
        finally:
            os.environ["REVERTLY_NO_HARDEN"] = "1"

    def test_append_incident_records_line(self):
        paths.append_incident("RM", "deleted session xyz")
        with open(paths.incidents_log()) as f:
            body = f.read()
        self.assertIn("RM", body)
        self.assertIn("deleted session xyz", body)


class TestShimArmFailure(SessionBase):
    def test_abort_policy_does_not_run_command(self):
        # C1: on_arm_failure=abort must REFUSE to run the wrapped command,
        # not run it unprotected.
        import io
        from contextlib import redirect_stderr
        from unittest import mock
        from revertly import shim
        # write a config that selects abort
        with open(paths.config_path(), "w") as f:
            f.write("on_arm_failure = \"abort\"\n")
        # force arm to fail by making the cloner explode via a bad cwd
        with mock.patch("revertly.session.Session.arm",
                        side_effect=ArmError("boom", policy="abort")), \
             mock.patch("subprocess.call") as call:
            with redirect_stderr(io.StringIO()):
                rc = shim.run_wrapped(["--", "echo", "hi"])
        self.assertNotEqual(rc, 0)
        call.assert_not_called()   # the command must NOT have run


class TestEventEnrichment(SessionBase):
    def _emit_and_read(self, path, op):
        s = self._mk()
        s.arm()
        s._watcher.emit(Event(kind=EventKind.FS, op=op, path=path))
        return Journal.read(paths.journal_path(s.id))

    def test_self_tamper_classified(self):
        tamper = os.path.expanduser("~/.zshrc")
        events = self._emit_and_read(tamper, FsOp.WRITE)
        self.assertTrue(any(e.kind == EventKind.SELF_TAMPER for e in events))

    def test_credential_tripwire_classified(self):
        cred = os.path.expanduser("~/.ssh/id_rsa")
        events = self._emit_and_read(cred, FsOp.READ)
        self.assertTrue(any(e.kind == EventKind.TRIPWIRE for e in events))

    def test_ordinary_write_is_plain_fs(self):
        p = os.path.join(self._proj.name, "a.txt")
        events = self._emit_and_read(p, FsOp.WRITE)
        fs = [e for e in events if e.kind == EventKind.FS and e.path == p]
        self.assertTrue(fs)

    def test_summary_line_mentions_counts(self):
        s = self._mk(); s.arm()
        s._watcher.emit(Event(kind=EventKind.FS, op=FsOp.WRITE,
                              path=os.path.join(self._proj.name, "a.txt")))
        s.seal(0)
        line = s.summary_line()
        self.assertIn("files touched", line)
        self.assertIn(s.id, line)


if __name__ == "__main__":
    unittest.main()
