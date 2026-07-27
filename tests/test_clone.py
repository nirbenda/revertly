"""Tests for revertly.clone — Cloner interface + Fake/Clonefile backends.

Run:  python3 -m unittest tests.test_clone -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

from revertly.clone import Cloner, FakeCloner, ClonefileCloner


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(data)


def _read(path):
    with open(path) as f:
        return f.read()


class TestFakeCloner(unittest.TestCase):
    def test_is_a_cloner(self):
        self.assertIsInstance(FakeCloner(), Cloner)

    def test_is_cow_configurable(self):
        self.assertTrue(FakeCloner(cow=True).is_cow())
        self.assertFalse(FakeCloner(cow=False).is_cow())

    def test_clone_file_really_copies_and_records(self):
        fc = FakeCloner()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.txt")
            dst = os.path.join(d, "b.txt")
            _write(src, "hello")
            fc.clone_file(src, dst)
            self.assertTrue(os.path.exists(dst))
            self.assertEqual(_read(dst), "hello")
            self.assertIn((src, dst), fc.file_calls)

    def test_clone_tree_really_copies_and_records(self):
        fc = FakeCloner()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            dst = os.path.join(d, "dst")
            _write(os.path.join(src, "x.txt"), "one")
            _write(os.path.join(src, "sub", "y.txt"), "two")
            fc.clone_tree(src, dst)
            self.assertEqual(_read(os.path.join(dst, "x.txt")), "one")
            self.assertEqual(_read(os.path.join(dst, "sub", "y.txt")), "two")
            self.assertIn((src, dst), fc.tree_calls)


class TestClonefileCloner(unittest.TestCase):
    def test_is_a_cloner(self):
        self.assertIsInstance(ClonefileCloner(), Cloner)

    def test_is_cow_reflects_platform(self):
        expected = sys.platform == "darwin"
        self.assertEqual(ClonefileCloner().is_cow(), expected)

    def test_clone_file_copies_bytes_correctly(self):
        c = ClonefileCloner()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "a.bin")
            dst = os.path.join(d, "b.bin")
            with open(src, "wb") as f:
                f.write(b"\x00\x01binary\xff data")
            c.clone_file(src, dst)
            self.assertTrue(os.path.exists(dst))
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), b"\x00\x01binary\xff data")

    def test_clone_tree_copies_whole_tree_correctly(self):
        c = ClonefileCloner()
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            dst = os.path.join(d, "dst")
            _write(os.path.join(src, "x.txt"), "one")
            _write(os.path.join(src, "sub", "y.txt"), "two")
            c.clone_tree(src, dst)
            self.assertEqual(_read(os.path.join(dst, "x.txt")), "one")
            self.assertEqual(_read(os.path.join(dst, "sub", "y.txt")), "two")


class TestPlatformCowArgv(unittest.TestCase):
    """The CoW copy command is platform-specific (APFS clonefile vs Linux
    reflink). Verify both regardless of the host we run on."""

    def test_darwin_uses_clonefile(self):
        with mock.patch("revertly.clone.sys.platform", "darwin"):
            self.assertEqual(ClonefileCloner._cow_tree_argv("s", "d"),
                             ["cp", "-Rc", "s", "d"])
            self.assertEqual(ClonefileCloner._cow_file_argv("s", "d"),
                             ["cp", "-c", "s", "d"])

    def test_linux_uses_reflink(self):
        with mock.patch("revertly.clone.sys.platform", "linux"):
            self.assertEqual(ClonefileCloner._cow_tree_argv("s", "d"),
                             ["cp", "-R", "--reflink=auto", "s", "d"])
            self.assertEqual(ClonefileCloner._cow_file_argv("s", "d"),
                             ["cp", "--reflink=auto", "s", "d"])
            self.assertFalse(ClonefileCloner().is_cow())  # no cheap-CoW promise


if __name__ == "__main__":
    unittest.main()
