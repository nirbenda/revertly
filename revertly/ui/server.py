"""revertly local control-panel HTTP server (read + preview + guarded actions).

Stdlib only (http.server, json, urllib, difflib). Reads the FROZEN on-disk
store directly via revertly.paths — never imports sibling worker modules for
reads. The revert action imports revertly.revert lazily so the panel loads even
if revert.py is briefly unavailable.

Action security (this server can now MUTATE — execute reverts, delete
sessions — so it defends the two classic localhost-panel holes):
  * DNS-rebinding: every request's Host header must be a loopback name.
  * CSRF: mutating requests (real revert, session delete) must carry the
    per-run token in X-Revertly-Token. The token is minted at import time and
    injected into index.html, which only a same-origin page can read.
Dry-run previews and GETs stay tokenless — they change nothing.

Store layout (frozen):
    <sessions_root>/<id>/meta.json          SessionMeta json
    <sessions_root>/<id>/journal.jsonl      one Event json per line
    <sessions_root>/<id>/clone/<relpath>    CoW pre-image of cwd (v0)

Public API:
    list_sessions() -> list[dict]        newest first
    load_session(id) -> dict | None      {meta, events}
    serve(host, port) -> (httpd, port)   background ThreadingHTTPServer
    Handler                              BaseHTTPRequestHandler subclass
"""
from __future__ import annotations

import difflib
import json
import os
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from revertly import paths
from revertly.search import MUTATING_OPS as _MUTATING_OPS
from revertly.search import read_events_raw, read_meta_raw

_HERE = os.path.dirname(os.path.abspath(__file__))
_INDEX_HTML = os.path.join(_HERE, "index.html")

# Per-run action token (CSRF guard for mutating endpoints).
ACTION_TOKEN = secrets.token_hex(16)

_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")


def _host_is_loopback(host_header) -> bool:
    """True if the Host header names a loopback address (DNS-rebind guard)."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("[") and "]" in host:      # [::1]:port
        host = host[:host.index("]") + 1]
    elif host.count(":") == 1:                    # name:port
        host = host.rsplit(":", 1)[0]
    # else: bare IPv6 like ::1 — no port to strip
    return host in _LOOPBACK_HOSTS


def _valid_sid(sid) -> bool:
    """Session ids are single path components — never let one traverse."""
    return bool(sid) and "/" not in sid and os.sep not in sid \
        and sid not in (".", "..")


# ─────────────────────────── store readers ───────────────────────────

def _read_meta(session_id: str):
    """meta dict, or None if the session is unknown (drives 404s)."""
    if not os.path.isfile(paths.meta_path(session_id)):
        return None
    return read_meta_raw(session_id) or None


# journal reading is owned by revertly.search — one tolerant reader everywhere
_read_events = read_events_raw


def _is_outside_project(ev: dict, cwd) -> bool:
    """An fs mutation to a path outside the session cwd."""
    if ev.get("kind") != "fs":
        return False
    if ev.get("op") not in _MUTATING_OPS:
        return False
    path = ev.get("path")
    if not path or not cwd:
        return False
    try:
        real_cwd = os.path.normpath(cwd)
        real_path = os.path.normpath(path)
    except (TypeError, ValueError):
        return False
    prefix = real_cwd.rstrip(os.sep) + os.sep
    return not (real_path == real_cwd or real_path.startswith(prefix))


def _session_row(session_id: str):
    meta = _read_meta(session_id)
    if meta is None:
        return None
    events = _read_events(session_id)
    cwd = meta.get("cwd")
    tripwire_count = sum(
        1 for e in events if e.get("kind") in ("tripwire", "self_tamper"))
    outside_count = sum(1 for e in events if _is_outside_project(e, cwd))
    return {
        "id": meta.get("id", session_id),
        "name": meta.get("name", ""),
        "cwd": cwd,
        "started": meta.get("started"),
        "ended": meta.get("ended"),
        "armed": bool(meta.get("armed", False)),
        "is_revert": bool(meta.get("is_revert", False)),
        "event_count": len(events),
        "tripwire_count": tripwire_count,
        "outside_count": outside_count,
    }


def list_sessions():
    """All sessions as summary rows, newest first."""
    rows = []
    for sid in paths.list_session_ids():
        row = _session_row(sid)
        if row is not None:
            rows.append(row)
    # ids are timestamp-prefixed and sort lexicographically; newest first.
    rows.sort(key=lambda r: r["id"], reverse=True)
    return rows


def load_session(session_id: str):
    """Full session: {meta, events}, or None if unknown."""
    meta = _read_meta(session_id)
    if meta is None:
        return None
    return {"meta": meta, "events": _read_events(session_id)}


def _read_text_best_effort(path):
    """Return (text, is_binary). Missing file -> ("", False)."""
    if not path or not os.path.isfile(path):
        return "", False
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return "", False
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), True


def build_diff(session_id: str, abs_path: str):
    """Unified diff of clone/ pre-image vs current cwd file.

    Returns {path, pre, cur, pre_binary, cur_binary, diff} or None if the
    session is unknown.
    """
    meta = _read_meta(session_id)
    if meta is None:
        return None
    cwd = os.path.realpath(meta.get("cwd") or "")
    # Confinement: BOTH sides only ever read inside the session's project dir
    # (this endpoint is a tokenless GET — it must not become a file oracle).
    norm_abs = os.path.realpath(os.path.normpath(abs_path))
    if not paths.is_under(norm_abs, cwd):
        return {"error": "path is outside the session project dir"}
    rel = os.path.relpath(norm_abs, cwd)
    pre_path = os.path.join(paths.clone_dir(session_id), rel)

    pre, pre_bin = _read_text_best_effort(pre_path)
    cur, cur_bin = _read_text_best_effort(norm_abs)

    diff = "".join(difflib.unified_diff(
        pre.splitlines(keepends=True),
        cur.splitlines(keepends=True),
        fromfile="pre/" + os.path.basename(abs_path),
        tofile="cur/" + os.path.basename(abs_path),
    ))
    return {
        "path": abs_path,
        "pre": pre,
        "cur": cur,
        "pre_binary": pre_bin,
        "cur_binary": cur_bin,
        "diff": diff,
    }


def locate_file(session_id: str, abs_path: str, which: str):
    """Resolve the on-disk location of a session file for view/download.

    which='pre' -> the clone/ pre-image; which='cur' -> the live file.
    Returns (real_path, filename) or (None, error_message). Both variants are
    confined: pre must resolve inside clone/, cur inside the session cwd —
    no `..`/symlink escape can read arbitrary files.
    """
    meta = _read_meta(session_id)
    if meta is None:
        return None, "unknown session: %s" % session_id
    cwd = os.path.realpath(meta.get("cwd") or "")
    norm_abs = os.path.realpath(os.path.normpath(abs_path))
    if not paths.is_under(norm_abs, cwd):
        return None, "path is outside the session project dir"
    rel = os.path.relpath(norm_abs, cwd)

    if which == "pre":
        root = os.path.realpath(paths.clone_dir(session_id))
    elif which == "cur":
        root = cwd
    else:
        return None, "which must be 'pre' or 'cur'"
    candidate = os.path.realpath(os.path.join(root, rel))
    if not paths.is_under(candidate, root):
        return None, "path escapes the session root"
    if not os.path.isfile(candidate):
        return None, "no %s copy of %s" % (which, rel)
    return candidate, os.path.basename(candidate)


def delete_session(session_id: str, force: bool = False):
    """Permanently remove one session from the store. Returns (status, payload).

    Mirrors the CLI's evidence guard: tripwire-flagged (or still-live)
    sessions are refused with 409 unless force=True — the UI must escalate
    explicitly, same as `revertly rm --force`.
    """
    meta = _read_meta(session_id)
    if meta is None:
        return 404, {"error": "unknown session: %s" % session_id}
    if not force:
        reasons = []
        if meta.get("ended") is None:
            reasons.append("session appears to be still running")
        trips = sum(1 for e in _read_events(session_id)
                    if e.get("kind") in ("tripwire", "self_tamper"))
        if trips:
            reasons.append("%d tripwire event(s) — it may be evidence" % trips)
        if reasons:
            return 409, {"error": "refusing to delete: %s (pass force to "
                                   "override)" % "; ".join(reasons)}
    paths.rmtree_force(paths.session_dir(session_id))
    return 200, {"deleted": session_id}


def run_revert(session_id: str, paths_list, dry_run: bool, force: bool = False):
    """Lazily import revertly.revert and build a plan.

    Returns (status_code, payload_dict). In tests dry_run is always True so
    nothing is mutated. If revert.py is unavailable, returns 501.
    """
    if _read_meta(session_id) is None:
        return 404, {"error": "unknown session: %s" % session_id}
    try:
        from revertly import revert as revert_mod  # lazy, guarded
    except ImportError as e:
        return 501, {"error": "revert unavailable: %s" % e}

    sdir = paths.session_dir(session_id)
    try:
        reverter = _make_reverter(revert_mod.Reverter, sdir)
        if paths_list:
            plan = _call_plan(reverter.plan_paths, sdir, list(paths_list))
        else:
            plan = _call_plan(reverter.plan, sdir)
        revert_id = reverter.apply(plan, dry_run=dry_run, force=force)
    except Exception as e:  # keep the panel robust against revert internals
        return 500, {"error": "revert failed: %s: %s" % (type(e).__name__, e)}

    payload = {
        "session_id": getattr(plan, "session_id", session_id),
        "dry_run": bool(dry_run),
        "restores": [_change_to_dict(c) for c in getattr(plan, "restores", [])],
        "deletes": [_change_to_dict(c) for c in getattr(plan, "deletes", [])],
        "conflicts": [_conflict_to_dict(c)
                      for c in getattr(plan, "conflicts", [])],
    }
    summ = getattr(plan, "summary", None)
    payload["summary"] = summ() if callable(summ) else str(summ)
    payload["is_clean"] = bool(getattr(plan, "is_clean", True))
    payload["revert_id"] = revert_id
    # design rule: the UI shows the equivalent CLI for every action
    import shlex
    payload["cli"] = "revertly revert %s%s%s" % (
        shlex.quote(session_id),
        "".join(" " + shlex.quote(p) for p in (paths_list or [])),
        " --dry-run" if dry_run else " --yes")
    return 200, payload


def _make_reverter(cls, session_dir):
    """Construct a Reverter, tolerant of no-arg or session_dir constructors."""
    import inspect
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        params = {}
    required = [p for p in params.values()
                if p.default is p.empty
                and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if required:
        return cls(session_dir)
    return cls()


def _call_plan(fn, session_dir, *extra):
    """Call plan()/plan_paths(), tolerant of whether it wants session_dir.

    TECH-DESIGN specifies plan(session_dir); the shipped Reverter binds
    session_dir in __init__ and takes only the paths list. Try the
    contract signature first, fall back to the bound form.
    """
    try:
        return fn(session_dir, *extra)
    except TypeError:
        return fn(*extra)


def _change_to_dict(c):
    ct = getattr(c, "change_type", None)
    return {
        "path": getattr(c, "path", None),
        "change_type": getattr(ct, "value", ct),
    }


def _conflict_to_dict(c):
    return {"path": getattr(c, "path", None),
            "reason": getattr(c, "reason", None)}


# ─────────────────────────── HTTP handler ───────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "revertly-ui/1"

    # silence default stderr logging during tests
    def log_message(self, fmt, *args):
        pass

    # -- response helpers --
    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body, status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._send_json({"error": message}, status=status)

    def _guard(self) -> bool:
        """Reject non-loopback Host headers (DNS-rebinding). True = rejected."""
        if not _host_is_loopback(self.headers.get("Host")):
            self._error(403, "forbidden: non-loopback Host header")
            return True
        return False

    def _has_token(self) -> bool:
        tok = self.headers.get("X-Revertly-Token") or ""
        return secrets.compare_digest(tok, ACTION_TOKEN)

    # -- routing --
    def do_GET(self):
        if self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            return self._serve_index()
        if path == "/api/sessions":
            return self._send_json(list_sessions())
        if path == "/api/find":
            return self._serve_find(query)

        # /api/session/<id> and /api/session/<id>/{diff,file}
        prefix = "/api/session/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest.endswith("/diff"):
                sid = urllib.parse.unquote(rest[:-len("/diff")])
                if not _valid_sid(sid):
                    return self._error(400, "invalid session id")
                return self._serve_diff(sid, query)
            if rest.endswith("/file"):
                sid = urllib.parse.unquote(rest[:-len("/file")])
                if not _valid_sid(sid):
                    return self._error(400, "invalid session id")
                return self._serve_file(sid, query)
            sid = urllib.parse.unquote(rest)
            if _valid_sid(sid):
                data = load_session(sid)
                if data is None:
                    return self._error(404, "unknown session: %s" % sid)
                return self._send_json(data)

        self._error(404, "not found: %s" % path)

    def do_POST(self):
        if self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        prefix = "/api/session/"
        for suffix, handler in (("/revert", self._serve_revert),
                                ("/delete", self._serve_delete)):
            if path.startswith(prefix) and path.endswith(suffix):
                sid = urllib.parse.unquote(path[len(prefix):-len(suffix)])
                if not _valid_sid(sid):
                    return self._error(400, "invalid session id")
                return handler(sid)
        self._error(404, "not found: %s" % path)

    # -- endpoint impls --
    def _serve_index(self):
        try:
            with open(_INDEX_HTML, "rb") as f:
                body = f.read()
        except OSError as e:
            return self._error(500, "cannot read index.html: %s" % e)
        body = body.replace(b"__REVERTLY_TOKEN__", ACTION_TOKEN.encode("ascii"))
        self._send_html(body)

    def _serve_find(self, query):
        q = (query.get("q") or [""])[0]
        if not q:
            return self._error(400, "missing required query param: q")
        op = (query.get("op") or [None])[0]
        try:
            from revertly.search import find_events  # lazy, guarded
        except ImportError as e:
            return self._error(501, "search unavailable: %s" % e)
        self._send_json(find_events(q, op=op))

    def _serve_file(self, sid, query):
        path_vals = query.get("path")
        if not path_vals or not path_vals[0]:
            return self._error(400, "missing required query param: path")
        which = (query.get("which") or ["pre"])[0]
        located, name = locate_file(sid, path_vals[0], which)
        if located is None:
            return self._error(404, name)
        try:
            with open(located, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._error(500, "cannot read file: %s" % e)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition",
                         'attachment; filename="%s.%s"' % (name, which))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_delete(self, sid):
        if not self._has_token():
            return self._error(403, "missing or bad X-Revertly-Token")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        force = bool(isinstance(body, dict) and body.get("force"))
        status, payload = delete_session(sid, force=force)
        self._send_json(payload, status=status)

    def _serve_diff(self, sid, query):
        path_vals = query.get("path")
        if not path_vals or not path_vals[0]:
            return self._error(400, "missing required query param: path")
        result = build_diff(sid, path_vals[0])
        if result is None:
            return self._error(404, "unknown session: %s" % sid)
        if "error" in result:
            return self._error(404, result["error"])
        self._send_json(result)

    def _serve_revert(self, sid):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            return self._error(400, "invalid JSON body")
        if not isinstance(body, dict):
            return self._error(400, "body must be a JSON object")
        paths_list = body.get("paths") or []
        if not isinstance(paths_list, list):
            return self._error(400, "'paths' must be a list")
        dry_run = bool(body.get("dry_run", True))
        force = bool(body.get("force", False))
        if not paths_list and not body.get("all"):
            # an empty selection must NEVER silently mean "everything" —
            # whole-session revert requires an explicit {"all": true}
            return self._error(
                400, "no paths selected; pass {\"all\": true} to revert "
                     "the whole session")
        if not dry_run and not self._has_token():
            # executing a real revert mutates the filesystem — token required
            return self._error(403, "missing or bad X-Revertly-Token")
        status, payload = run_revert(sid, paths_list, dry_run, force=force)
        self._send_json(payload, status=status)


# ─────────────────────────── server bootstrap ───────────────────────────

def make_server(host="127.0.0.1", port=0):
    """Create (but do not start) a ThreadingHTTPServer bound to host:port."""
    return ThreadingHTTPServer((host, port), Handler)


def serve(host="127.0.0.1", port=0):
    """Start serving in a background thread. Returns (httpd, bound_port).

    port=0 -> OS picks a free port; read it back from server_address.
    """
    httpd = make_server(host, port)
    bound_port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, bound_port


def main(host="127.0.0.1", port=8721):
    """Run in the foreground (used by `revertly ui`)."""
    httpd = make_server(host, port)
    bound = httpd.server_address[1]
    print("revertly control panel: http://%s:%d/" % (host, bound))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
