"""Tests for revertly.snapshot — Snapshotter interface + Fake/Tmutil backends.

Run:  python3 -m unittest tests.test_snapshot -v

NOTE: these tests never create a real system snapshot. Only the Fake, the
can_snapshot/PATH logic, and stdout parsing are exercised.
"""
import shutil
import unittest

from revertly.snapshot import (
    Snapshotter, FakeSnapshotter, TmutilSnapshotter, parse_snapshot_name,
)


class TestParseSnapshotName(unittest.TestCase):
    def test_parses_standard_tmutil_line(self):
        out = "Created local snapshot with date: 2026-07-25-103000"
        self.assertEqual(parse_snapshot_name(out), "2026-07-25-103000")

    def test_parses_with_surrounding_whitespace_and_newlines(self):
        out = "\nCreated local snapshot with date: 2026-07-25-103000\n\n"
        self.assertEqual(parse_snapshot_name(out), "2026-07-25-103000")

    def test_returns_none_on_unrecognized_output(self):
        self.assertIsNone(parse_snapshot_name("something went wrong"))

    def test_returns_none_on_empty(self):
        self.assertIsNone(parse_snapshot_name(""))


class TestFakeSnapshotter(unittest.TestCase):
    def test_is_a_snapshotter(self):
        self.assertIsInstance(FakeSnapshotter(), Snapshotter)

    def test_can_snapshot_default_true(self):
        self.assertTrue(FakeSnapshotter().can_snapshot())

    def test_can_snapshot_configurable(self):
        self.assertFalse(FakeSnapshotter(can=False).can_snapshot())

    def test_create_returns_name_and_records(self):
        fs = FakeSnapshotter()
        name = fs.create()
        self.assertIsNotNone(name)
        self.assertIn(name, fs.created)
        self.assertEqual(len(fs.created), 1)

    def test_create_is_deterministic_and_unique(self):
        fs = FakeSnapshotter()
        n1 = fs.create()
        n2 = fs.create()
        self.assertNotEqual(n1, n2)
        self.assertEqual(fs.created, [n1, n2])

    def test_delete_removes_from_recorded(self):
        fs = FakeSnapshotter()
        name = fs.create()
        fs.delete(name)
        self.assertIn(name, fs.deleted)
        self.assertNotIn(name, fs.created)

    def test_delete_unknown_raises(self):
        fs = FakeSnapshotter()
        with self.assertRaises(Exception):
            fs.delete("does-not-exist")


class TestTmutilSnapshotter(unittest.TestCase):
    def test_is_a_snapshotter(self):
        self.assertIsInstance(TmutilSnapshotter(), Snapshotter)

    def test_construction_never_crashes_without_tmutil(self):
        orig = shutil.which
        shutil.which = lambda name: None
        try:
            t = TmutilSnapshotter()
            self.assertFalse(t.can_snapshot())
        finally:
            shutil.which = orig

    def test_can_snapshot_reflects_which_present(self):
        orig = shutil.which
        shutil.which = lambda name: "/usr/bin/tmutil" if name == "tmutil" else None
        try:
            self.assertTrue(TmutilSnapshotter().can_snapshot())
        finally:
            shutil.which = orig

    def test_can_snapshot_reflects_which_absent(self):
        orig = shutil.which
        shutil.which = lambda name: None
        try:
            self.assertFalse(TmutilSnapshotter().can_snapshot())
        finally:
            shutil.which = orig


if __name__ == "__main__":
    unittest.main()
