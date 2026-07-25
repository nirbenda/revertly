"""Tests for revertly.agents — detection + bind/unbind lifecycle.

Run:  python3 -m unittest tests.test_agents -v
"""
import os
import stat
import tempfile
import unittest

from revertly import agents, paths, shim


class TestAgents(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_agents_home_")
        self.fakebin = tempfile.mkdtemp(prefix="revertly_agents_bin_")
        self._old_home = os.environ.get("REVERTLY_HOME")
        self._old_path = os.environ.get("PATH")
        os.environ["REVERTLY_HOME"] = self.home
        # put a couple of fake agent binaries on PATH
        for cmd in ("claude", "aider"):
            p = os.path.join(self.fakebin, cmd)
            with open(p, "w") as f:
                f.write("#!/bin/bash\necho hi\n")
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        os.environ["PATH"] = self.fakebin + os.pathsep + os.environ.get("PATH", "")
        paths.ensure_store()

    def tearDown(self):
        import shutil
        for k, v in (("REVERTLY_HOME", self._old_home), ("PATH", self._old_path)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.fakebin, ignore_errors=True)

    def test_detect_finds_agents_on_path(self):
        found = {c for c, _, _ in agents.detect()}
        self.assertIn("claude", found)
        self.assertIn("aider", found)
        self.assertNotIn("gemini", found)   # not on PATH

    def test_real_on_path_skips_shim_dir(self):
        # once bound, real_on_path must still find the REAL binary, not the shim
        shim.install_shim("claude")
        real = agents.real_on_path("claude")
        self.assertIsNotNone(real)
        self.assertTrue(real.startswith(self.fakebin))

    def test_bind_and_bound_agents(self):
        shim.install_shim("claude")
        shim.install_shim("aider")
        self.assertEqual(set(agents.bound_agents()), {"claude", "aider"})

    def test_is_revertly_shim_only_matches_our_shims(self):
        shim.install_shim("claude")
        self.assertTrue(agents.is_revertly_shim(os.path.join(paths.bin_dir(), "claude")))
        # a random executable is not a revertly shim
        other = os.path.join(paths.bin_dir(), "notashim")
        with open(other, "w") as f:
            f.write("#!/bin/bash\necho nope\n")
        self.assertFalse(agents.is_revertly_shim(other))

    def test_uninstall_removes_all_shims(self):
        shim.install_launcher()
        shim.install_shim("claude")
        shim.install_shim("aider")
        res = shim.uninstall(profile=False)
        # bin dir should have no shims or launcher left
        left = [f for f in os.listdir(paths.bin_dir())]
        self.assertEqual(agents.bound_agents(), [])
        self.assertNotIn("revertly", left)


if __name__ == "__main__":
    unittest.main()
