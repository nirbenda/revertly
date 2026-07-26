"""Tests for revertly.guard — the agnostic command-interception layer.

Run:  python3 -m unittest tests.test_guard -v
"""
import os
import stat
import tempfile
import unittest

from revertly import guard, hooks, paths
from revertly.config import Config


def _finding(kind):
    return hooks.Finding(kind, "alert", f"a {kind} thing")


class TestDecide(unittest.TestCase):
    def cfg(self, mode):
        c = Config()
        c.guard_mode = mode
        return c

    def test_alert_mode_never_blocks(self):
        action, _ = guard.decide([_finding("SUSPICIOUS")], self.cfg("alert"), False)
        self.assertEqual(action, "allow")

    def test_block_mode_blocks_suspicious(self):
        action, f = guard.decide([_finding("SUSPICIOUS")], self.cfg("block"), False)
        self.assertEqual(action, "block")
        self.assertEqual(f.kind, "SUSPICIOUS")

    def test_block_mode_blocks_secret_read_and_self_tamper(self):
        for kind in ("READ", "SELF_TAMPER"):
            action, _ = guard.decide([_finding(kind)], self.cfg("block"), False)
            self.assertEqual(action, "block", kind)

    def test_allow_escape_overrides_block(self):
        action, _ = guard.decide([_finding("SUSPICIOUS")], self.cfg("block"), True)
        self.assertEqual(action, "allow")

    def test_non_blockable_kind_passes(self):
        action, _ = guard.decide([_finding("INFO")], self.cfg("block"), False)
        self.assertEqual(action, "allow")


class TestShims(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_guard_home_")
        self.fakebin = tempfile.mkdtemp(prefix="revertly_guard_bin_")
        self._old_home = os.environ.get("REVERTLY_HOME")
        self._old_path = os.environ.get("PATH")
        os.environ["REVERTLY_HOME"] = self.home
        # provide a couple of the guarded commands as fake binaries
        for cmd in ("curl", "bash"):
            p = os.path.join(self.fakebin, cmd)
            with open(p, "w") as f:
                f.write("#!/bin/bash\ntrue\n")
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        os.environ["PATH"] = self.fakebin + os.pathsep + os.environ.get("PATH", "")

    def tearDown(self):
        import shutil
        for k, v in (("REVERTLY_HOME", self._old_home), ("PATH", self._old_path)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.fakebin, ignore_errors=True)

    def test_ensure_creates_shims_for_present_commands(self):
        made = guard.ensure_cmd_shims()
        self.assertIn("curl", made)
        self.assertIn("bash", made)
        self.assertTrue(os.access(os.path.join(paths.cmdbin_dir(), "curl"), os.X_OK))

    def test_resolve_real_skips_cmdbin(self):
        guard.ensure_cmd_shims()
        # cmdbin/curl exists; resolve_real must still return the FAKEBIN curl
        real = guard.resolve_real("curl")
        self.assertIsNotNone(real)
        self.assertTrue(real.startswith(self.fakebin))
        self.assertNotEqual(os.path.dirname(real), paths.cmdbin_dir())


if __name__ == "__main__":
    unittest.main()
