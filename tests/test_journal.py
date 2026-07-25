"""Tests for revertly.journal — append-only hash-chained JSONL journal.

Run:  python3 -m unittest tests.test_journal -v
"""
import json
import os
import tempfile
import unittest

from revertly.journal import Journal
from revertly.model import Event, EventKind, FsOp


class TestAppend(unittest.TestCase):
    def test_seq_and_prev_hash_chain(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            e0 = j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/a", t=1.0))
            e1 = j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/b", t=2.0))
            e2 = j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/c", t=3.0))
            self.assertEqual([e0.seq, e1.seq, e2.seq], [0, 1, 2])
            self.assertIsNone(e0.prev_hash)
            self.assertEqual(e1.prev_hash, e0.hash)
            self.assertEqual(e2.prev_hash, e1.hash)
            for e in (e0, e1, e2):
                self.assertIsNotNone(e.hash)
                self.assertEqual(len(e.hash), 64)  # sha256 hex

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nested", "deep", "journal.jsonl")
            j = Journal(p)
            j.append(Event(kind=EventKind.HEARTBEAT, t=1.0))
            self.assertTrue(os.path.exists(p))

    def test_append_returns_mutated_event(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            e = Event(kind=EventKind.FS, op=FsOp.WRITE, path="/a", t=1.0)
            ret = j.append(e)
            self.assertIs(ret, e)
            self.assertEqual(e.seq, 0)


class TestReadRoundtrip(unittest.TestCase):
    def test_read_preserves_kind_op_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/x/y.txt", t=1.0))
            j.append(Event(kind=EventKind.TOOL, tool="Edit", target="/x/y.txt", t=2.0))
            events = Journal.read(p)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].kind, EventKind.FS)
            self.assertEqual(events[0].op, FsOp.WRITE)
            self.assertEqual(events[0].path, "/x/y.txt")
            self.assertEqual(events[1].kind, EventKind.TOOL)
            self.assertEqual(events[1].tool, "Edit")
            self.assertEqual(events[1].target, "/x/y.txt")
            self.assertEqual([e.seq for e in events], [0, 1])

    def test_read_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "does-not-exist.jsonl")
            self.assertEqual(Journal.read(p), [])

    def test_read_skips_blank_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            j.append(Event(kind=EventKind.HEARTBEAT, t=1.0))
            with open(p, "a", encoding="utf-8") as f:
                f.write("\n")
                f.write("   \n")
            # (blank lines above must be ignored by read)
            events = Journal.read(p)
            self.assertEqual(len(events), 1)


class TestVerify(unittest.TestCase):
    def test_verify_ok_on_untampered(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            for i in range(5):
                j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/f%d" % i, t=float(i)))
            self.assertEqual(Journal.verify(p), (True, None))

    def test_verify_ok_on_empty_or_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            self.assertEqual(Journal.verify(p), (True, None))
            open(p, "w").close()
            self.assertEqual(Journal.verify(p), (True, None))

    def test_verify_detects_modified_middle_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            for i in range(5):
                j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/f%d" % i, t=float(i)))
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
            rec = json.loads(lines[2])
            rec["path"] = "/tampered"       # edit content, leave hash field intact
            lines[2] = json.dumps(rec)
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            ok, seq = Journal.verify(p)
            self.assertFalse(ok)
            self.assertEqual(seq, 2)

    def test_verify_detects_reordered_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            for i in range(5):
                j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/f%d" % i, t=float(i)))
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
            lines[1], lines[2] = lines[2], lines[1]   # swap a pair
            with open(p, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            ok, seq = Journal.verify(p)
            self.assertFalse(ok)
            self.assertIsNotNone(seq)

    def test_verify_truncation_is_valid_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            for i in range(5):
                j.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/f%d" % i, t=float(i)))
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
            with open(p, "w", encoding="utf-8") as f:   # keep clean prefix of 3
                f.write("\n".join(lines[:3]) + "\n")
            self.assertEqual(Journal.verify(p), (True, None))


class TestReopen(unittest.TestCase):
    def test_reopen_continues_chain(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j1 = Journal(p)
            a = j1.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/a", t=1.0))
            b = j1.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/b", t=2.0))
            # reopen a fresh instance on the same file
            j2 = Journal(p)
            c = j2.append(Event(kind=EventKind.FS, op=FsOp.WRITE, path="/c", t=3.0))
            self.assertEqual(c.seq, 2)
            self.assertEqual(c.prev_hash, b.hash)
            self.assertEqual([e.seq for e in Journal.read(p)], [0, 1, 2])
            self.assertEqual(Journal.verify(p), (True, None))

    def test_reopen_empty_file_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            Journal(p)  # creates empty file
            j = Journal(p)
            e = j.append(Event(kind=EventKind.HEARTBEAT, t=1.0))
            self.assertEqual(e.seq, 0)
            self.assertIsNone(e.prev_hash)


class TestCorruptTrailingLine(unittest.TestCase):
    def test_read_skips_partial_trailing_line(self):
        # a process killed mid-append leaves a truncated last line; read()
        # must return the clean prefix, never crash (verify() stays strict).
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            j.heartbeat()
            j.heartbeat()
            with open(p, "a") as f:
                f.write('{"kind": "fs", "op": "wri')  # torn write
            events = Journal.read(p)
            self.assertEqual(len(events), 2)


class TestHeartbeat(unittest.TestCase):
    def test_heartbeat_appends_and_reads(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "journal.jsonl")
            j = Journal(p)
            j.heartbeat()
            events = Journal.read(p)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, EventKind.HEARTBEAT)
            self.assertEqual(events[0].seq, 0)
            self.assertEqual(Journal.verify(p), (True, None))


if __name__ == "__main__":
    unittest.main()
