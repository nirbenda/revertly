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
    for d in (revertly_home(), sessions_root(), mirror_root(), bin_dir()):
        ensure_dir(d)


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
