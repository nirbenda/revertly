"""Snapshot-scope safety guard.

Regression for a HIGH-severity incident: launching `claude` from $HOME made
revertly try to clone the entire home directory (Library/, caches, cloud-synced
trees) before starting the agent. On macOS with EDR/Spotlight inspecting every
filesystem op, that produced a complete system freeze needing a hard restart.

The guard must refuse to snapshot an unbounded root ($HOME, /, an ancestor of
home) or an oversized tree, and instead run the agent UNPROTECTED (fail-safe).
"""
import os
import tempfile
import unittest
from unittest import mock

from revertly.config import Config
from revertly.session import (Session, _broad_root_reason,
                              _entry_count_exceeds)
from revertly.clone import FakeCloner
from revertly.snapshot import FakeSnapshotter
from revertly.watch import FakeWatcher


class TestBroadRootReason(unittest.TestCase):
    def test_home_is_refused(self):
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertIsNotNone(_broad_root_reason(home))

    def test_filesystem_root_is_refused(self):
        self.assertIsNotNone(_broad_root_reason(os.path.sep))

    def test_ancestor_of_home_is_refused(self):
        # e.g. /Users (macOS) or /home (Linux) — even broader than home itself
        parent = os.path.dirname(os.path.realpath(os.path.expanduser("~")))
        self.assertIsNotNone(_broad_root_reason(parent))

    def test_normal_dir_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_broad_root_reason(d))


class TestEntryCountExceeds(unittest.TestCase):
    def _seed(self, d, n, sub=""):
        base = os.path.join(d, sub) if sub else d
        os.makedirs(base, exist_ok=True)
        for i in range(n):
            open(os.path.join(base, f"f{i}"), "w").close()

    def test_under_limit(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, 5)
            self.assertFalse(_entry_count_exceeds(d, 100))

    def test_over_limit(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, 20)
            self.assertTrue(_entry_count_exceeds(d, 5))

    def test_zero_limit_is_unlimited(self):
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, 20)
            self.assertFalse(_entry_count_exceeds(d, 0))

    def test_excluded_dirs_are_not_counted(self):
        # a huge node_modules must NOT trip the limit on an otherwise small
        # project — excluded dirs are pruned from the count.
        cfg = Config()
        with tempfile.TemporaryDirectory() as d:
            self._seed(d, 50, sub="node_modules")
            open(os.path.join(d, "app.py"), "w").close()
            self.assertFalse(_entry_count_exceeds(d, 10, cfg.is_excluded))


class TestSnapshotBlockReason(unittest.TestCase):
    def _sess(self, cwd, cfg=None):
        return Session(cwd=cwd, argv=["claude"], cfg=cfg or Config(),
                       snapshotter=FakeSnapshotter(), cloner=FakeCloner(),
                       watcher=FakeWatcher())

    def test_project_dir_is_ok(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a.txt"), "w").close()
            self.assertIsNone(self._sess(d).snapshot_block_reason())

    def test_home_is_blocked(self):
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertIsNotNone(self._sess(home).snapshot_block_reason())

    def test_override_allows_home(self):
        home = os.path.realpath(os.path.expanduser("~"))
        cfg = Config(); cfg.allow_broad_snapshot = True
        self.assertIsNone(self._sess(home, cfg).snapshot_block_reason())

    def test_oversized_tree_is_blocked(self):
        cfg = Config(); cfg.max_snapshot_entries = 3
        with tempfile.TemporaryDirectory() as d:
            for i in range(10):
                open(os.path.join(d, f"f{i}"), "w").close()
            self.assertIn("too large", self._sess(d, cfg).snapshot_block_reason())


if __name__ == "__main__":
    unittest.main()
