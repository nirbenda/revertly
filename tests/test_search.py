"""Tests for revertly.search — cross-session path search ("where is my file?").

Run:  python3 -m unittest tests.test_search -v
"""
import json
import os
import shutil
import tempfile
import unittest

from revertly import paths
from revertly.search import find_events, path_matches


class TestPathMatches(unittest.TestCase):
    def test_substring_case_insensitive(self):
        self.assertTrue(path_matches("/Users/x/prj/TEMP.md", "temp"))
        self.assertTrue(path_matches("/Users/x/prj/TEMP.md", "prj/TEMP"))
        self.assertFalse(path_matches("/Users/x/prj/TEMP.md", "nope"))

    def test_glob_full_path_and_basename(self):
        self.assertTrue(path_matches("/home/a/.env", "*.env"))
        self.assertTrue(path_matches("/home/a/secrets.env", "*.env"))
        self.assertTrue(path_matches("/p/src/app.py", "src/*.py"))
        self.assertFalse(path_matches("/p/src/app.py", "*.md"))

    def test_none_and_empty(self):
        self.assertFalse(path_matches(None, "x"))
        self.assertFalse(path_matches("/a/b", ""))


class TestFindEvents(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_search_")
        self._old = os.environ.get("REVERTLY_HOME")
        os.environ["REVERTLY_HOME"] = self.home
        self._seed("2026-07-18T09-00-00_old", "older", [
            {"kind": "fs", "op": "delete", "path": "/p/config/app.yaml", "t": 100.0},
            {"kind": "fs", "op": "write", "path": "/p/src/main.py", "t": 101.0},
            {"kind": "heartbeat", "t": 102.0},
        ])
        self._seed("2026-07-25T09-00-00_new", "newer", [
            {"kind": "fs", "op": "write", "path": "/p/config/app.yaml", "t": 200.0},
            {"kind": "tripwire", "op": "read", "path": "/home/u/.ssh/id_rsa",
             "t": 201.0},
        ])

    def tearDown(self):
        if self._old is None:
            os.environ.pop("REVERTLY_HOME", None)
        else:
            os.environ["REVERTLY_HOME"] = self._old
        shutil.rmtree(self.home, ignore_errors=True)

    def _seed(self, sid, name, events):
        sdir = paths.session_dir(sid)
        os.makedirs(sdir, exist_ok=True)
        with open(paths.meta_path(sid), "w") as f:
            json.dump({"id": sid, "name": name, "cwd": "/p"}, f)
        with open(paths.journal_path(sid), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_finds_across_sessions_newest_first(self):
        hits = find_events("app.yaml")
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["session_id"], "2026-07-25T09-00-00_new")
        self.assertEqual(hits[0]["op"], "write")
        self.assertEqual(hits[1]["session_id"], "2026-07-18T09-00-00_old")
        self.assertEqual(hits[1]["op"], "delete")

    def test_op_filter(self):
        hits = find_events("app.yaml", op="delete")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["op"], "delete")

    def test_since_filter(self):
        hits = find_events("app.yaml", since=150.0)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["session_id"], "2026-07-25T09-00-00_new")

    def test_tripwires_are_searchable(self):
        hits = find_events("*.ssh/*")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["kind"], "tripwire")

    def test_heartbeats_never_match(self):
        self.assertEqual(find_events("*"), find_events("*"))
        for h in find_events("*"):
            self.assertNotEqual(h["kind"], "heartbeat")

    def test_carries_session_context(self):
        h = find_events("main.py")[0]
        self.assertEqual(h["session_name"], "older")
        self.assertEqual(h["cwd"], "/p")


if __name__ == "__main__":
    unittest.main()
