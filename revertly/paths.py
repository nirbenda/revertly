"""revertly store layout and session-id generation. Pure path logic + mkdir.

The root is overridable via $REVERTLY_HOME so tests never touch the real store.
"""
from __future__ import annotations

import os
import secrets
import shutil
import stat
import time
from typing import Optional


def revertly_home() -> str:
    return os.environ.get("REVERTLY_HOME") or os.path.expanduser("~/.revertly")


def incidents_log() -> str:
    # cross-session, append-only record of tripwire / SELF_TAMPER hits
    return os.path.join(revertly_home(), "incidents.log")


def sessions_root() -> str:
    return os.path.join(revertly_home(), "sessions")


def mirror_root() -> str:
    # Tier-2 root-owned mirror lives here. Phase 1 just ensures the dir exists.
    return os.path.join(revertly_home(), "mirror")


def bin_dir() -> str:
    return os.path.join(revertly_home(), "bin")


def config_path() -> str:
    return os.path.join(revertly_home(), "config.toml")


def new_session_id(name_hint: str = "") -> str:
    ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime())
    suffix = secrets.token_hex(2)
    return f"{ts}_{suffix}"


def session_dir(session_id: str) -> str:
    return os.path.join(sessions_root(), session_id)


def clone_dir(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "clone")


def versions_dir(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "versions")


def journal_path(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "journal.jsonl")


def meta_path(session_id: str) -> str:
    return os.path.join(session_dir(session_id), "meta.json")


def seal_path(session_id: str) -> str:
    # immutable anchor written at seal: the final {seq, hash}. Lets verify()
    # detect TRUNCATION (a clean-prefix cut that the hash chain alone accepts).
    return os.path.join(session_dir(session_id), "journal.seal")


def is_under(path: str, root: str) -> bool:
    """True if `path` equals `root` or lies beneath it (string-prefix on
    normalized paths — callers pass already-normalized/realpathed values).
    The one place the 'is this path inside that dir' check lives; a bare
    startswith(root) is a bug (/a/bc matches /a/b)."""
    if not path or not root:
        return False
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root + os.sep)


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def ensure_store() -> None:
    home = revertly_home()
    for d in (home, sessions_root(), mirror_root(), bin_dir()):
        ensure_dir(d)
    # The store holds full clones of projects — including any secrets they
    # contain (.env, keys). Lock the root to 0700 so no OTHER local user can
    # traverse in, regardless of inner file perms (default umask leaves clone
    # files 0644). One chmod on the root is sufficient: 0700 blocks traversal.
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass


def append_incident(tag: str, detail: str, session_id: str = "-") -> None:
    """Append a line to the cross-session incident log. Used for tripwire
    hits AND for destructive/disable actions (rm, gc, purge, pause, disable)
    so they can never happen silently. One 4-column schema everywhere:
    `timestamp \\t session \\t tag \\t detail`. Best-effort; never raises."""
    import time as _time
    try:
        ensure_store()
        line = (f"{_time.strftime('%Y-%m-%dT%H:%M:%S')}\t{session_id}\t"
                f"{tag}\t{detail}\n")
        with open(incidents_log(), "a") as f:
            f.write(line)
    except OSError:
        pass


def list_session_ids() -> list:
    root = sessions_root()
    if not os.path.isdir(root):
        return []
    return sorted(
        (d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))),
    )


def latest_session_id() -> Optional[str]:
    ids = list_session_ids()
    return ids[-1] if ids else None


def tree_size(path: str) -> int:
    """Total bytes of all files under path (follows no symlinks)."""
    total = 0
    if not os.path.isdir(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.lstat(os.path.join(root, fn)).st_size
            except OSError:
                pass
    return total


def session_size(session_id: str) -> int:
    return tree_size(session_dir(session_id))


def store_size() -> int:
    return tree_size(sessions_root())


# ─────────────────── tamper-raising immutability (Tier-1) ───────────────────
# macOS user-immutable flag (uchg). A same-UID attacker CAN clear it, but only
# with a deliberate extra step (`chflags nouchg`) — which raises the bar and,
# combined with the hash chain, makes silent tampering hard. Not tamper-PROOF
# (that needs the Tier-2 root daemon); it's tamper-RAISING. See THREAT-MODEL.md.

def make_immutable(path: str) -> bool:
    if os.environ.get("REVERTLY_NO_HARDEN"):
        return False
    try:
        os.chflags(path, os.stat(path).st_flags | stat.UF_IMMUTABLE)
        return True
    except (OSError, AttributeError):
        return False


def clear_immutable(path: str) -> None:
    try:
        os.chflags(path, os.stat(path).st_flags & ~stat.UF_IMMUTABLE)
    except (OSError, AttributeError):
        pass


def is_immutable(path: str) -> bool:
    try:
        return bool(os.stat(path).st_flags & stat.UF_IMMUTABLE)
    except (OSError, AttributeError):
        return False


def rmtree_force(path: str) -> None:
    """Delete a tree even if it contains immutable files (clears uchg first)."""
    if not os.path.exists(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            clear_immutable(os.path.join(root, name))
    clear_immutable(path)
    shutil.rmtree(path, ignore_errors=True)
