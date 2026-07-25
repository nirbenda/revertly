"""Tests for revertly.tripwire — TripwireEngine classification.

Run:  python3 -m unittest tests.test_tripwire -v

Covers invariant #6 (SELF_TAMPER): writes/deletes under ~/.revertly, the shim,
or shell rc files classify as a SELF_TAMPER tripwire at ALERT.
"""
import os
import unittest

from revertly.config import Config
from revertly.model import FsOp, Severity, TripwireHit
from revertly.tripwire import TripwireEngine


def home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


class TestSelfTamper(unittest.TestCase):
    def setUp(self):
        self.engine = TripwireEngine(Config())

    def test_revertly_state_is_self_tamper(self):
        hit = self.engine.check(home(".revertly", "config.toml"), FsOp.WRITE)
        self.assertIsInstance(hit, TripwireHit)
        self.assertTrue(hit.self_tamper)
        self.assertEqual(hit.severity, Severity.ALERT)
        self.assertEqual(hit.op, FsOp.WRITE)

    def test_revertly_root_itself_is_self_tamper(self):
        hit = self.engine.check(home(".revertly"), FsOp.DELETE)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.self_tamper)

    def test_zshrc_is_self_tamper(self):
        hit = self.engine.check(home(".zshrc"), FsOp.WRITE)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.self_tamper)
        self.assertEqual(hit.severity, Severity.ALERT)

    def test_bashrc_is_self_tamper(self):
        hit = self.engine.check(home(".bashrc"), FsOp.WRITE)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.self_tamper)

    def test_self_tamper_pattern_is_a_self_tamper_glob(self):
        hit = self.engine.check(home(".zshrc"), FsOp.WRITE)
        self.assertIn(hit.pattern, Config().self_tamper_globs())

    def test_relative_path_expanded_to_absolute(self):
        # A tilde path should be expanded to the home dir and match.
        hit = self.engine.check("~/.zshrc", FsOp.WRITE)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.self_tamper)


class TestOrdinaryTripwire(unittest.TestCase):
    def setUp(self):
        self.engine = TripwireEngine(Config())

    def test_ssh_key_is_tripwire_not_self_tamper(self):
        hit = self.engine.check(home(".ssh", "id_rsa"), FsOp.READ)
        self.assertIsInstance(hit, TripwireHit)
        self.assertFalse(hit.self_tamper)
        self.assertEqual(hit.severity, Severity.ALERT)

    def test_tripwire_records_op_and_path(self):
        p = home(".ssh", "id_rsa")
        hit = self.engine.check(p, FsOp.READ)
        self.assertEqual(hit.op, FsOp.READ)
        self.assertEqual(hit.path, os.path.abspath(os.path.expanduser(p)))

    def test_tripwire_pattern_is_a_tripwire_glob(self):
        hit = self.engine.check(home(".ssh", "id_rsa"), FsOp.READ)
        self.assertIn(hit.pattern, Config().tripwire_globs_all())


class TestNoHit(unittest.TestCase):
    def setUp(self):
        self.engine = TripwireEngine(Config())

    def test_ordinary_project_path_is_none(self):
        hit = self.engine.check(home("prj", "myrepo", "main.py"), FsOp.WRITE)
        self.assertIsNone(hit)

    def test_tmp_scratch_path_is_none(self):
        hit = self.engine.check("/tmp/scratch/notes.txt", FsOp.WRITE)
        self.assertIsNone(hit)


class TestSelfTamperPriority(unittest.TestCase):
    def test_self_tamper_takes_priority_over_ordinary(self):
        # ~/.revertly is in both DEFAULT_EXCLUDE and SELF_TAMPER; ensure the hit
        # is classified as self_tamper (priority) rather than a plain tripwire.
        hit = TripwireEngine(Config()).check(home(".revertly", "sessions", "x"),
                                             FsOp.WRITE)
        self.assertIsNotNone(hit)
        self.assertTrue(hit.self_tamper)


if __name__ == "__main__":
    unittest.main()
