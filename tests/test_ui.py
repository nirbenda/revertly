"""Tests for revertly.ui.server — the local control panel.

Hermetic: sets $REVERTLY_HOME to a temp dir and hand-creates a session dir with
meta.json + journal.jsonl + clone/. Store reads are unit-tested directly
(list_sessions/load_session) and end-to-end over HTTP via urllib.

Run:  python3 -m unittest tests.test_ui -v
"""
import json
import os
import shutil
import tempfile
import unittest
import urllib.request
import urllib.error

from revertly import paths
from revertly.ui import server


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class _StoreFixture(unittest.TestCase):
    """Base: builds a temp store with one seeded session."""

    SESSION_ID = "2026-07-25T10-30-00_a1b2"

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="revertly_ui_test_")
        self._old_home = os.environ.get("REVERTLY_HOME")
        os.environ["REVERTLY_HOME"] = self.home
        self._seed()

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("REVERTLY_HOME", None)
        else:
            os.environ["REVERTLY_HOME"] = self._old_home
        shutil.rmtree(self.home, ignore_errors=True)

    def _seed(self):
        sid = self.SESSION_ID
        sdir = paths.session_dir(sid)
        os.makedirs(sdir, exist_ok=True)

        meta = {
            "id": sid,
            "name": "fix-the-tests",
            "cwd": os.path.join(self.home, "project"),
            "argv": ["claude", "go"],
            "started": 1000.0,
            "ended": 1200.0,
            "armed": True,
            "is_revert": False,
        }
        _write(paths.meta_path(sid), json.dumps(meta))

        # journal: a tool event, an fs write, a tripwire, a self_tamper,
        # and an out-of-project fs write.
        events = [
            {"kind": "tool", "t": 1001.0, "tool": "Edit",
             "target": "app.py", "checkpoint": 1},
            {"kind": "fs", "t": 1001.5, "op": "write",
             "path": os.path.join(meta["cwd"], "app.py"),
             "version": "v1", "checkpoint": 1},
            {"kind": "tripwire", "t": 1002.0, "op": "read",
             "path": "~/.ssh/id_ed25519", "severity": "alert"},
            {"kind": "self_tamper", "t": 1003.0, "op": "write",
             "path": os.path.join(self.home, "config.toml"),
             "severity": "alert"},
            {"kind": "fs", "t": 1004.0, "op": "write",
             "path": "/etc/hosts", "version": "v1"},
        ]
        lines = "\n".join(json.dumps(e) for e in events) + "\n"
        _write(paths.journal_path(sid), lines)

        # clone/ pre-image of app.py, and the live cwd copy (changed).
        rel = "app.py"
        _write(os.path.join(paths.clone_dir(sid), rel),
               "line one\nline two\n")
        _write(os.path.join(meta["cwd"], rel),
               "line one\nline two changed\nline three\n")
        self.meta = meta
        self.changed_abs = os.path.join(meta["cwd"], rel)


class TestStoreFunctions(_StoreFixture):
    def test_list_sessions_counts(self):
        rows = server.list_sessions()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], self.SESSION_ID)
        self.assertEqual(row["name"], "fix-the-tests")
        self.assertEqual(row["event_count"], 5)
        self.assertEqual(row["tripwire_count"], 2)   # tripwire + self_tamper
        self.assertEqual(row["outside_count"], 1)    # /etc/hosts fs write
        self.assertTrue(row["armed"])
        self.assertFalse(row["is_revert"])

    def test_load_session(self):
        data = server.load_session(self.SESSION_ID)
        self.assertEqual(data["meta"]["id"], self.SESSION_ID)
        self.assertEqual(len(data["events"]), 5)
        self.assertEqual(data["events"][0]["kind"], "tool")
        self.assertEqual(data["events"][1]["op"], "write")

    def test_load_missing_session(self):
        self.assertIsNone(server.load_session("nope-not-here"))


class TestHttpServer(_StoreFixture):
    def setUp(self):
        super().setUp()
        self.httpd, self.port = server.serve(host="127.0.0.1", port=0)

    def tearDown(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        finally:
            super().tearDown()

    def _get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()

    def _get_json(self, path):
        status, ctype, body = self._get(path)
        return status, json.loads(body.decode("utf-8"))

    def _post_json(self, path, payload, token=None):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Revertly-Token"] = token
        req = urllib.request.Request(
            url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_index_served(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"revertly control panel", body)

    def test_api_sessions(self):
        status, data = self._get_json("/api/sessions")
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)
        ids = [r["id"] for r in data]
        self.assertIn(self.SESSION_ID, ids)

    def test_api_session_events(self):
        status, data = self._get_json("/api/session/%s" % self.SESSION_ID)
        self.assertEqual(status, 200)
        self.assertEqual(data["meta"]["id"], self.SESSION_ID)
        self.assertEqual(len(data["events"]), 5)

    def test_api_session_404(self):
        url = "http://127.0.0.1:%d/api/session/does-not-exist" % self.port
        try:
            urllib.request.urlopen(url, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            body = json.loads(e.read().decode("utf-8"))
            self.assertIn("error", body)

    def test_api_diff(self):
        import urllib.parse
        q = urllib.parse.quote(self.changed_abs)
        status, data = self._get_json(
            "/api/session/%s/diff?path=%s" % (self.SESSION_ID, q))
        self.assertEqual(status, 200)
        self.assertIn("line two changed", data["cur"])
        self.assertIn("line two\n", data["pre"])
        self.assertIn("line two changed", data["diff"])
        self.assertIn("-line two", data["diff"])

    def test_api_diff_bad_params(self):
        url = "http://127.0.0.1:%d/api/session/%s/diff" % (
            self.port, self.SESSION_ID)
        try:
            urllib.request.urlopen(url, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_api_revert_dry_run(self):
        status, data = self._post_json(
            "/api/session/%s/revert" % self.SESSION_ID,
            {"paths": [self.changed_abs], "dry_run": True})
        # Accept either a plan summary (revert importable) or a 501 (not yet).
        self.assertIn(status, (200, 501))
        if status == 200:
            # RevertPlan summary shape.
            self.assertIn("session_id", data)
            self.assertIn("summary", data)
            self.assertIn("cli", data)
        else:
            self.assertIn("error", data)

    # ── find across sessions ─────────────────────────────────────────

    def test_api_find(self):
        status, data = self._get_json("/api/find?q=app.py")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["session_id"], self.SESSION_ID)
        self.assertEqual(data[0]["op"], "write")

    def test_api_find_requires_q(self):
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d/api/find" % self.port, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    # ── raw file view/download ───────────────────────────────────────

    def test_api_file_pre_download(self):
        import urllib.parse
        q = urllib.parse.quote(self.changed_abs)
        status, ctype, body = self._get(
            "/api/session/%s/file?path=%s&which=pre" % (self.SESSION_ID, q))
        self.assertEqual(status, 200)
        self.assertEqual(body, b"line one\nline two\n")

    def test_api_file_cur_download(self):
        import urllib.parse
        q = urllib.parse.quote(self.changed_abs)
        status, ctype, body = self._get(
            "/api/session/%s/file?path=%s&which=cur" % (self.SESSION_ID, q))
        self.assertEqual(status, 200)
        self.assertIn(b"line two changed", body)

    def test_api_file_denies_escape(self):
        import urllib.parse
        outside = os.path.join(self.meta["cwd"], "..", "..", "etc", "passwd")
        q = urllib.parse.quote(outside)
        url = ("http://127.0.0.1:%d/api/session/%s/file?path=%s&which=pre"
               % (self.port, self.SESSION_ID, q))
        try:
            urllib.request.urlopen(url, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    # ── mutating actions need the per-run token ──────────────────────

    def test_real_revert_requires_token(self):
        status, data = self._post_json(
            "/api/session/%s/revert" % self.SESSION_ID,
            {"paths": [self.changed_abs], "dry_run": False})
        self.assertEqual(status, 403)
        self.assertIn("error", data)
        # nothing was mutated
        with open(self.changed_abs) as f:
            self.assertIn("line two changed", f.read())

    def test_real_revert_with_token_restores_file(self):
        status, data = self._post_json(
            "/api/session/%s/revert" % self.SESSION_ID,
            {"paths": [self.changed_abs], "dry_run": False, "force": True},
            token=server.ACTION_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("revert_id"))
        with open(self.changed_abs) as f:
            self.assertEqual(f.read(), "line one\nline two\n")

    def test_delete_session_requires_token(self):
        status, data = self._post_json(
            "/api/session/%s/delete" % self.SESSION_ID, {})
        self.assertEqual(status, 403)
        self.assertTrue(os.path.isdir(paths.session_dir(self.SESSION_ID)))

    def test_delete_session_with_token(self):
        status, data = self._post_json(
            "/api/session/%s/delete" % self.SESSION_ID, {},
            token=server.ACTION_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(data["deleted"], self.SESSION_ID)
        self.assertFalse(os.path.isdir(paths.session_dir(self.SESSION_ID)))

    # ── DNS-rebinding guard ──────────────────────────────────────────

    def test_non_loopback_host_rejected(self):
        url = "http://127.0.0.1:%d/api/sessions" % self.port
        req = urllib.request.Request(url, headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)

    def test_token_injected_into_index(self):
        status, ctype, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(server.ACTION_TOKEN.encode("ascii"), body)
        self.assertNotIn(b"__REVERTLY_TOKEN__", body)


if __name__ == "__main__":
    unittest.main()
