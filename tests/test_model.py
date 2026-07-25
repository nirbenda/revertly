"""Contract tests for revertly.model — the reference TDD style for the team.

Run:  python3 -m unittest tests.test_model -v
"""
import unittest

from revertly.model import (
    Event, EventKind, FsOp, Severity, SessionMeta, RevertPlan, Change,
    ChangeType, Conflict, TripwireHit,
)


class TestEventSerialization(unittest.TestCase):
    def test_roundtrip_fs_event(self):
        e = Event(kind=EventKind.FS, op=FsOp.WRITE, path="/x/y.txt",
                  version="v3", checkpoint=12, t=123.0)
        d = e.to_json_dict()
        self.assertEqual(d["kind"], "fs")
        self.assertEqual(d["op"], "write")
        back = Event.from_json_dict(d)
        self.assertEqual(back.kind, EventKind.FS)
        self.assertEqual(back.op, FsOp.WRITE)
        self.assertEqual(back.path, "/x/y.txt")
        self.assertEqual(back.checkpoint, 12)

    def test_none_fields_omitted(self):
        e = Event(kind=EventKind.HEARTBEAT, t=1.0)
        d = e.to_json_dict()
        self.assertNotIn("op", d)
        self.assertNotIn("path", d)
        self.assertEqual(d["kind"], "heartbeat")

    def test_severity_default_info(self):
        e = Event(kind=EventKind.FS, t=1.0)
        self.assertEqual(e.severity, Severity.INFO)


class TestSessionMeta(unittest.TestCase):
    def test_roundtrip(self):
        m = SessionMeta(id="s1", name="fix", cwd="/p", argv=["claude", "go"],
                        started=1.0, armed=True)
        d = m.to_json_dict()
        back = SessionMeta.from_json_dict(d)
        self.assertEqual(back.id, "s1")
        self.assertTrue(back.armed)
        self.assertIsNone(back.ended)


class TestRevertPlan(unittest.TestCase):
    def test_is_clean(self):
        p = RevertPlan(session_id="s1")
        self.assertTrue(p.is_clean)
        p.conflicts.append(Conflict(path="/x", reason="diverged"))
        self.assertFalse(p.is_clean)

    def test_summary_counts(self):
        p = RevertPlan(session_id="s1")
        p.restores.append(Change(path="/a", change_type=ChangeType.MODIFIED))
        p.deletes.append(Change(path="/b", change_type=ChangeType.CREATED))
        self.assertIn("1 to restore", p.summary())
        self.assertIn("1 to delete", p.summary())


class TestTripwireHit(unittest.TestCase):
    def test_defaults(self):
        h = TripwireHit(path="~/.ssh/id_rsa", op=FsOp.READ, pattern="~/.ssh/**")
        self.assertEqual(h.severity, Severity.ALERT)
        self.assertFalse(h.self_tamper)


if __name__ == "__main__":
    unittest.main()
