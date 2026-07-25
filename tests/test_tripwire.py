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

    def test_custom_revertly_home_is_self_tamper(self):
        # regression: self-tamper globs were hard-coded to ~/.revertly and
        # missed a custom $REVERTLY_HOME entirely, so SELF_TAMPER never fired
        # under a temp store (tests, CI, non-default installs).
        old = os.environ.get("REVERTLY_HOME")
        os.environ["REVERTLY_HOME"] = "/tmp/custom-revertly-store"
        try:
            engine = TripwireEngine(Config())
            hit = engine.check(
                "/tmp/custom-revertly-store/sessions/x/clone/f.txt", FsOp.DELETE)
            self.assertIsNotNone(hit)
            self.assertTrue(hit.self_tamper)
        finally:
            if old is None:
                os.environ.pop("REVERTLY_HOME", None)
            else:
                os.environ["REVERTLY_HOME"] = old

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

    def test_env_family_all_trip(self):
        # the whole .env family — not just the bare name — must fire (the
        # .local/.production variants are where secrets actually live).
        for name in (".env", ".env.local", ".env.production", ".env.development",
                     "secrets.env"):
            hit = self.engine.check(home("prj", "app", name), FsOp.WRITE)
            self.assertIsNotNone(hit, f"{name} should trip")
            self.assertFalse(hit.self_tamper)


class TestExcludeGlobs(unittest.TestCase):
    def test_directory_itself_is_excluded(self):
        # regression (H4): `**/node_modules/**` must match the DIRECTORY too,
        # or the watcher never prunes it and descends every poll.
        cfg = Config()
        for d in ("/proj/node_modules", "/proj/.git", "/proj/src/.claude"):
            self.assertTrue(cfg.is_excluded(d), f"{d} dir should be excluded")

    def test_git_and_claude_excluded(self):
        cfg = Config()
        self.assertTrue(cfg.is_excluded("/proj/.git/refs/main"))
        self.assertTrue(cfg.is_excluded("/proj/.claude/settings.json"))

    def test_source_file_not_excluded(self):
        cfg = Config()
        self.assertFalse(cfg.is_excluded("/proj/src/app.py"))


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
