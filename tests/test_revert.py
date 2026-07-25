"""TDD contract tests for revertly.revert — the revert engine.

Run:  python3 -m unittest tests.test_revert -v

These tests build fake session fixtures directly (no dependency on
session.py) via a temp REVERTLY_HOME and revertly.paths helpers.
"""
import json
import os
import shutil
import tempfile
import time
import unittest

from revertly import paths
from revertly.model import ChangeType, SessionMeta
from revertly.revert import Reverter


# ─────────────────────────── fixture helper ───────────────────────────

class _Fixture:
    """A fake session dir + a live project (cwd) tree.

    clone/ holds the PRE-IMAGE of cwd at session start. The caller then
    mutates the project dir to simulate what the agent did, and Reverter
    compares clone/ vs project/ to plan/apply a revert.
    """

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="revertly_home_")
        self.project = tempfile.mkdtemp(prefix="revertly_proj_")
        os.environ["REVERTLY_HOME"] = self.home
        paths.ensure_store()
        self.sid = "2026-07-25T10-00-00_test"
        self._clone = paths.clone_dir(self.sid)
        paths.ensure_dir(self._clone)
        # session ended 1 hour ago by default
        self.ended = time.time() - 3600.0
        self._meta_written = False

    # -- building the pre-image (clone) + matching cwd seed --------------
    def seed(self, relpath, content):
        """Write `content` to BOTH clone/ and project/ (identical start state)."""
        self._write(self._clone, relpath, content)
        self._write(self.project, relpath, content)

    def clone_only(self, relpath, content):
        """A file that exists in the pre-image only (helper for edge cases)."""
        self._write(self._clone, relpath, content)

    # -- simulating what the agent did to the project --------------------
    def modify(self, relpath, content):
        self._write(self.project, relpath, content)

    def delete(self, relpath):
        os.remove(os.path.join(self.project, relpath))

    def create(self, relpath, content):
        self._write(self.project, relpath, content)

    def read_project(self, relpath):
        with open(os.path.join(self.project, relpath), "rb") as f:
            return f.read()

    def project_exists(self, relpath):
        return os.path.exists(os.path.join(self.project, relpath))

    def touch_future(self, relpath, when=None):
        """Bump a project file's mtime past meta.ended (user kept working)."""
        p = os.path.join(self.project, relpath)
        when = when if when is not None else self.ended + 60.0
        os.utime(p, (when, when))

    def finalize(self):
        """Write meta.json. All clone files get mtime <= ended so nothing is
        spuriously flagged as a conflict at rest."""
        for root, _dirs, files in os.walk(self.project):
            for fn in files:
                fp = os.path.join(root, fn)
                # default: file was last touched during the session
                os.utime(fp, (self.ended - 10.0, self.ended - 10.0))
        meta = SessionMeta(
            id=self.sid, name="test", cwd=self.project,
            argv=["claude"], started=self.ended - 100.0, ended=self.ended,
            clone_path=self._clone, armed=True,
        )
        with open(paths.meta_path(self.sid), "w") as f:
            json.dump(meta.to_json_dict(), f)
        self._meta_written = True

    def cleanup(self):
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.project, ignore_errors=True)
        os.environ.pop("REVERTLY_HOME", None)

    @staticmethod
    def _write(base, relpath, content):
        fp = os.path.join(base, relpath)
        os.makedirs(os.path.dirname(fp) or base, exist_ok=True)
        data = content.encode() if isinstance(content, str) else content
        with open(fp, "wb") as f:
            f.write(data)


class RevertTestCase(unittest.TestCase):
    def setUp(self):
        self.fx = _Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def _abs(self, rel):
        return os.path.join(self.fx.project, rel)


# ─────────────────────────── plan classification ───────────────────────────

class TestPlanClassification(RevertTestCase):
    def test_modified_file_in_restores(self):
        self.fx.seed("a.txt", "original")
        self.fx.modify("a.txt", "changed")
        self.fx.finalize()

        plan = Reverter(paths.session_dir(self.fx.sid)).plan()
        paths_in_restores = {c.path for c in plan.restores}
        self.assertIn(self._abs("a.txt"), paths_in_restores)
        c = next(c for c in plan.restores if c.path == self._abs("a.txt"))
        self.assertEqual(c.change_type, ChangeType.MODIFIED)
        self.assertIsNotNone(c.pre_blob)

    def test_deleted_file_in_restores(self):
        self.fx.seed("gone.txt", "keepme")
        self.fx.delete("gone.txt")
        self.fx.finalize()

        plan = Reverter(paths.session_dir(self.fx.sid)).plan()
        c = next(c for c in plan.restores if c.path == self._abs("gone.txt"))
        self.assertEqual(c.change_type, ChangeType.DELETED)
        self.assertIsNotNone(c.pre_blob)

    def test_created_file_in_deletes(self):
        self.fx.seed("a.txt", "same")
        self.fx.create("new.txt", "brand new")
        self.fx.finalize()

        plan = Reverter(paths.session_dir(self.fx.sid)).plan()
        c = next(c for c in plan.deletes if c.path == self._abs("new.txt"))
        self.assertEqual(c.change_type, ChangeType.CREATED)
        self.assertIsNone(c.pre_blob)

    def test_identical_file_no_change(self):
        self.fx.seed("a.txt", "same")
        self.fx.finalize()

        plan = Reverter(paths.session_dir(self.fx.sid)).plan()
        self.assertEqual(plan.restores, [])
        self.assertEqual(plan.deletes, [])
        self.assertTrue(plan.is_clean)


# ─────────────────────────── apply: restore/recreate/delete ────────────────

class TestApply(RevertTestCase):
    def test_apply_restores_modified_bytes(self):
        self.fx.seed("a.txt", "original")
        self.fx.modify("a.txt", "changed")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        r.apply(r.plan())
        self.assertEqual(self.fx.read_project("a.txt"), b"original")

    def test_apply_recreates_deleted(self):
        self.fx.seed("gone.txt", "keepme")
        self.fx.delete("gone.txt")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        r.apply(r.plan())
        self.assertTrue(self.fx.project_exists("gone.txt"))
        self.assertEqual(self.fx.read_project("gone.txt"), b"keepme")

    def test_apply_deletes_created(self):
        self.fx.seed("a.txt", "same")
        self.fx.create("new.txt", "brand new")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        r.apply(r.plan())
        self.assertFalse(self.fx.project_exists("new.txt"))
        # untouched file stays put
        self.assertTrue(self.fx.project_exists("a.txt"))


# ─────────────────────────── non-destructive (marquee) ─────────────────────

class TestNonDestructive(RevertTestCase):
    def test_revert_the_revert_restores_post_session_state(self):
        # Post-session state: a.txt modified, gone.txt deleted, new.txt created.
        self.fx.seed("a.txt", "original")
        self.fx.seed("gone.txt", "keepme")
        self.fx.modify("a.txt", "changed")
        self.fx.delete("gone.txt")
        self.fx.create("new.txt", "brand new")
        self.fx.finalize()

        # snapshot the post-session state so we can compare after revert-revert
        post = {
            "a.txt": b"changed",
            "new.txt": b"brand new",
        }

        r = Reverter(paths.session_dir(self.fx.sid))
        revert_sid = r.apply(r.plan())
        self.assertIsNotNone(revert_sid)

        # after the first revert, we are back to pre-image
        self.assertEqual(self.fx.read_project("a.txt"), b"original")
        self.assertTrue(self.fx.project_exists("gone.txt"))
        self.assertFalse(self.fx.project_exists("new.txt"))

        # a NEW revert-session dir exists, flagged is_revert
        rmeta_path = paths.meta_path(revert_sid)
        self.assertTrue(os.path.exists(rmeta_path))
        with open(rmeta_path) as f:
            rmeta = SessionMeta.from_json_dict(json.load(f))
        self.assertTrue(rmeta.is_revert)
        self.assertEqual(rmeta.reverts_session, self.fx.sid)

        # Now revert THE REVERT -> the post-session state must come back.
        r2 = Reverter(paths.session_dir(revert_sid))
        r2.apply(r2.plan())

        self.assertEqual(self.fx.read_project("a.txt"), post["a.txt"])
        self.assertEqual(self.fx.read_project("new.txt"), post["new.txt"])
        # gone.txt was deleted in post-session state, so it must be gone again
        self.assertFalse(self.fx.project_exists("gone.txt"))

    def test_capture_holds_original_current_bytes(self):
        self.fx.seed("a.txt", "original")
        self.fx.modify("a.txt", "changed")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        revert_sid = r.apply(r.plan())

        # the revert-session's clone must hold the pre-revert (current) bytes
        clone_a = os.path.join(paths.clone_dir(revert_sid),
                               os.path.relpath(self._abs("a.txt"), self.fx.project))
        self.assertTrue(os.path.exists(clone_a))
        with open(clone_a, "rb") as f:
            self.assertEqual(f.read(), b"changed")


# ─────────────────────────── conflict safety ───────────────────────────────

class TestConflictSafety(RevertTestCase):
    def test_modified_after_end_is_conflict_and_not_overwritten(self):
        self.fx.seed("a.txt", "original")
        self.fx.modify("a.txt", "changed")
        self.fx.finalize()
        # user kept working: bump mtime past meta.ended
        self.fx.touch_future("a.txt")

        r = Reverter(paths.session_dir(self.fx.sid))
        plan = r.plan()
        conflict_paths = {c.path for c in plan.conflicts}
        self.assertIn(self._abs("a.txt"), conflict_paths)

        # apply WITHOUT force must NOT overwrite the conflicted file
        r.apply(plan, force=False)
        self.assertEqual(self.fx.read_project("a.txt"), b"changed")

    def test_force_overwrites_conflict(self):
        self.fx.seed("a.txt", "original")
        self.fx.modify("a.txt", "changed")
        self.fx.finalize()
        self.fx.touch_future("a.txt")

        r = Reverter(paths.session_dir(self.fx.sid))
        plan = r.plan()
        self.assertFalse(plan.is_clean)

        r.apply(plan, force=True)
        self.assertEqual(self.fx.read_project("a.txt"), b"original")


# ─────────────────────────── scope restriction ─────────────────────────────

class TestPlanPaths(RevertTestCase):
    def test_plan_paths_restricts_to_subset(self):
        self.fx.seed("src/a.txt", "a-orig")
        self.fx.seed("src/b.txt", "b-orig")
        self.fx.seed("docs/c.txt", "c-orig")
        self.fx.modify("src/a.txt", "a-new")
        self.fx.modify("src/b.txt", "b-new")
        self.fx.modify("docs/c.txt", "c-new")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        # restrict to just src/a.txt (relative path)
        plan = r.plan_paths(["src/a.txt"])
        restore_paths = {c.path for c in plan.restores}
        self.assertEqual(restore_paths, {self._abs("src/a.txt")})

    def test_plan_paths_dir_includes_everything_under(self):
        self.fx.seed("src/a.txt", "a-orig")
        self.fx.seed("src/nested/b.txt", "b-orig")
        self.fx.seed("docs/c.txt", "c-orig")
        self.fx.modify("src/a.txt", "a-new")
        self.fx.modify("src/nested/b.txt", "b-new")
        self.fx.modify("docs/c.txt", "c-new")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        plan = r.plan_paths(["src"])   # a directory
        restore_paths = {c.path for c in plan.restores}
        self.assertEqual(restore_paths,
                         {self._abs("src/a.txt"), self._abs("src/nested/b.txt")})

    def test_plan_paths_absolute_path_accepted(self):
        self.fx.seed("a.txt", "orig")
        self.fx.modify("a.txt", "new")
        self.fx.finalize()

        r = Reverter(paths.session_dir(self.fx.sid))
        plan = r.plan_paths([self._abs("a.txt")])
        self.assertEqual({c.path for c in plan.restores}, {self._abs("a.txt")})


# ─────────────────────────── dry run ───────────────────────────────────────

class TestDryRun(RevertTestCase):
    def test_dry_run_mutates_nothing(self):
        self.fx.seed("a.txt", "original")
        self.fx.seed("gone.txt", "keepme")
        self.fx.modify("a.txt", "changed")
        self.fx.delete("gone.txt")
        self.fx.create("new.txt", "brand new")
        self.fx.finalize()

        before_sessions = set(paths.list_session_ids())

        r = Reverter(paths.session_dir(self.fx.sid))
        result = r.apply(r.plan(), dry_run=True)

        # project untouched
        self.assertEqual(self.fx.read_project("a.txt"), b"changed")
        self.assertFalse(self.fx.project_exists("gone.txt"))
        self.assertTrue(self.fx.project_exists("new.txt"))

        # no new revert-session created on disk
        after_sessions = set(paths.list_session_ids())
        self.assertEqual(before_sessions, after_sessions)
        # result is either None or a would-be id string, but nothing on disk
        if result is not None:
            self.assertFalse(os.path.exists(paths.session_dir(result)))


class TestPlanPathsGlobs(RevertTestCase):
    def _session(self):
        self.fx.seed("src/app.py", "orig-app")
        self.fx.seed("src/util.py", "orig-util")
        self.fx.seed("docs/readme.md", "orig-docs")
        self.fx.modify("src/app.py", "changed-app")
        self.fx.modify("src/util.py", "changed-util")
        self.fx.modify("docs/readme.md", "changed-docs")
        self.fx.finalize()
        return Reverter(paths.session_dir(self.fx.sid))

    def test_glob_selects_by_extension_at_any_depth(self):
        plan = self._session().plan_paths(["*.py"])
        got = sorted(os.path.relpath(c.path, self.fx.project)
                     for c in plan.restores)
        self.assertEqual(got, ["src/app.py", "src/util.py"])

    def test_glob_relative_pattern(self):
        plan = self._session().plan_paths(["src/*.py"])
        self.assertEqual(len(plan.restores), 2)

    def test_glob_and_prefix_mix(self):
        plan = self._session().plan_paths(["docs", "*.py"])
        self.assertEqual(len(plan.restores), 3)

    def test_prefix_semantics_still_exact(self):
        plan = self._session().plan_paths(["docs"])
        got = [os.path.relpath(c.path, self.fx.project) for c in plan.restores]
        self.assertEqual(got, ["docs/readme.md"])

    def test_literal_path_with_brackets_still_matches(self):
        # Next.js-style dirs contain [] — they must behave as literal paths,
        # not fnmatch character classes (regression: glob detection dropped
        # them from the prefix list entirely).
        self.fx.seed("app/[slug]/page.tsx", "orig")
        self.fx.modify("app/[slug]/page.tsx", "broken")
        self.fx.finalize()
        r = Reverter(paths.session_dir(self.fx.sid))
        plan = r.plan_paths(["app/[slug]/page.tsx"])
        got = [os.path.relpath(c.path, self.fx.project) for c in plan.restores]
        self.assertEqual(got, ["app/[slug]/page.tsx"])


if __name__ == "__main__":
    unittest.main()
