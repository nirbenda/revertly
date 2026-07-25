"""Tests for revertly.cli surfaces added in the UX pass: restore shortcut,
honest status PATH check, doctor --install mode.

Run:  python3 -m unittest tests.test_cli -v
"""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from revertly import cli, paths
from revertly.model import SessionMeta


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_cli_home_")
        self.proj = tempfile.mkdtemp(prefix="revertly_cli_proj_")
        self._old = os.environ.get("REVERTLY_HOME")
        os.environ["REVERTLY_HOME"] = self.home
        os.environ["REVERTLY_NO_HARDEN"] = "1"
        paths.ensure_store()

    def tearDown(self):
        if self._old is None:
            os.environ.pop("REVERTLY_HOME", None)
        else:
            os.environ["REVERTLY_HOME"] = self._old
        os.environ.pop("REVERTLY_NO_HARDEN", None)
        import shutil
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.proj, ignore_errors=True)

    def _seed_session(self, sid, rel, clone_content):
        """A session whose clone holds `clone_content` for `rel`."""
        clone = paths.clone_dir(sid)
        p = os.path.join(clone, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(clone_content)
        # ended in the future: current file edits count as "during the
        # session" (no post-session-conflict), which is the restore scenario.
        meta = SessionMeta(id=sid, name="t", cwd=self.proj, argv=["claude"],
                           started=time.time() - 100, ended=time.time() + 3600,
                           clone_path=clone, armed=True)
        os.makedirs(paths.session_dir(sid), exist_ok=True)
        with open(paths.meta_path(sid), "w") as f:
            json.dump(meta.to_json_dict(), f)
        open(paths.journal_path(sid), "a").close()


class TestRestore(_Fixture):
    def _run(self, argv):
        args = cli.build_parser().parse_args(argv)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_restore_picks_newest_preimage(self):
        self._seed_session("2026-07-25T09-00-00_old", "app.yaml", "OLD")
        self._seed_session("2026-07-25T10-00-00_new", "app.yaml", "NEW")
        live = os.path.join(self.proj, "app.yaml")
        with open(live, "w") as f:
            f.write("BROKEN")
        rc, out = self._run(["restore", live, "--yes"])
        self.assertEqual(rc, 0)
        with open(live) as f:
            self.assertEqual(f.read(), "NEW")     # newest pre-image wins
        self.assertIn("2026-07-25T10-00-00_new", out)

    def test_restore_no_preimage(self):
        rc, out = self._run(["restore", os.path.join(self.proj, "ghost.txt"),
                             "--yes"])
        self.assertEqual(rc, 1)
        self.assertIn("no session holds a pre-image", out)

    def test_restore_dry_run_changes_nothing(self):
        self._seed_session("2026-07-25T09-00-00_s", "f.txt", "ORIG")
        live = os.path.join(self.proj, "f.txt")
        with open(live, "w") as f:
            f.write("changed")
        rc, out = self._run(["restore", live, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", out)
        with open(live) as f:
            self.assertEqual(f.read(), "changed")


class TestClear(_Fixture):
    def _run(self, argv):
        args = cli.build_parser().parse_args(argv)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = args.func(args)
        return rc, buf.getvalue()

    def _flagged_meta(self, sid):
        m = json.load(open(paths.meta_path(sid)))
        m["flagged"] = True
        json.dump(m, open(paths.meta_path(sid), "w"))

    def test_clear_before_session_removes_only_older(self):
        for s in ("2026-07-25T08-00-00_a", "2026-07-25T09-00-00_b",
                  "2026-07-25T10-00-00_c"):
            self._seed_session(s, "f.txt", "x")
        # backdate started so ordering is real
        for i, s in enumerate(("2026-07-25T08-00-00_a", "2026-07-25T09-00-00_b",
                               "2026-07-25T10-00-00_c")):
            m = json.load(open(paths.meta_path(s)))
            m["started"] = 1000 + i; m["ended"] = 1000 + i + 0.5
            json.dump(m, open(paths.meta_path(s), "w"))
        rc, out = self._run(["clear", "--before", "2026-07-25T09-00-00_b", "--yes"])
        self.assertEqual(rc, 0)
        left = set(paths.list_session_ids())
        self.assertEqual(left, {"2026-07-25T09-00-00_b", "2026-07-25T10-00-00_c"})

    def test_clear_all_keeps_flagged_by_default(self):
        self._seed_session("2026-07-25T08-00-00_a", "f.txt", "x")
        self._seed_session("2026-07-25T09-00-00_ev", "f.txt", "x")
        self._flagged_meta("2026-07-25T09-00-00_ev")
        rc, out = self._run(["clear", "--all", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(paths.list_session_ids()), {"2026-07-25T09-00-00_ev"})

    def test_clear_all_include_flagged(self):
        self._seed_session("2026-07-25T09-00-00_ev", "f.txt", "x")
        self._flagged_meta("2026-07-25T09-00-00_ev")
        rc, out = self._run(["clear", "--all", "--include-flagged", "--yes"])
        self.assertEqual(rc, 0)
        self.assertEqual(paths.list_session_ids(), [])

    def test_clear_requires_a_selector(self):
        rc, out = self._run(["clear", "--yes"])
        self.assertEqual(rc, 2)
        self.assertIn("specify", out)

    def test_clear_dry_run_changes_nothing(self):
        self._seed_session("2026-07-25T08-00-00_a", "f.txt", "x")
        rc, out = self._run(["clear", "--all", "--dry-run"])
        self.assertIn("dry-run", out)
        self.assertEqual(len(paths.list_session_ids()), 1)


class TestStatusHonesty(_Fixture):
    def test_status_reports_no_agents_bound(self):
        args = cli.build_parser().parse_args(["status"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            args.func(args)
        # no shim in this temp store's bin -> must not claim "armed"
        self.assertNotIn("armed", buf.getvalue())
        self.assertIn("no agents bound", buf.getvalue())


class TestDoctorInstallMode(_Fixture):
    def test_install_mode_no_warn(self):
        # create a shim so it's "installed" but (not first in PATH here)
        os.makedirs(paths.bin_dir(), exist_ok=True)
        open(os.path.join(paths.bin_dir(), "claude"), "w").close()
        args = cli.build_parser().parse_args(["doctor", "--install"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = args.func(args)
        out = buf.getvalue()
        self.assertEqual(rc, 0)                    # install mode never fails
        self.assertNotIn("WARN", out)
        self.assertIn("installed", out)


if __name__ == "__main__":
    unittest.main()
