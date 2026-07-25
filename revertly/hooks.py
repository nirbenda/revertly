"""revertly.hooks — the tool-call inspection layer (the "hook layer").

The polling watcher sees files change on disk, but it is blind to two things a
prompt-injected agent does: READING secrets, and running SUSPICIOUS commands
(that don't leave an obvious file trace). Claude Code fires a hook before each
tool call; `revertly hook` receives that payload and classifies it:

  * READ        — Read/Grep/Glob (or a Bash `cat ~/.ssh/id_rsa`) of a secret
  * SUSPICIOUS  — curl|sh, reverse shells, launchd/cron persistence, base64|sh…
  * SELF_TAMPER — a command that disables/erases revertly

Findings are appended to the cross-session incident log and (if a revertly
session is active) a per-session `hooks.jsonl`, and raised as a desktop
notification — the same "announce it the instant it happens" contract as the
filesystem tripwires. Phase 1 is ALERT-ONLY: the hook never blocks the tool
(it always exits 0), so it can't break the agent. Blocking is Phase 2.

Honest ceiling: this covers the AGENT's own tool calls. A binary or subprocess
the agent spawns that reads secrets or opens a socket is NOT visible here —
that needs the Endpoint-Security build (Tier 3).
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import List

from revertly import paths
from revertly.config import Config, match_glob

# tools that read file content
_READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}

# substrings that betray a secret being touched from a shell command
_SECRET_TOKENS = [
    "/.ssh/", "id_rsa", "id_ed25519", "id_ecdsa", "/.aws/", "/.gnupg/",
    ".env", ".pem", "/.config/gh", ".npmrc", ".netrc", "authorized_keys",
    "/etc/shadow", "/etc/sudoers", "keychain", "credentials",
]

# (regex, human label) — dangerous command shapes
_SUSPICIOUS = [
    (r"\bcurl\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "pipe curl into a shell"),
    (r"\bwget\b[^|]*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "pipe wget into a shell"),
    (r"\bbase64\b\s+(-d|-D|--decode)\b.*\|\s*(sh|bash|zsh)", "base64-decode into a shell"),
    (r"\|\s*(sh|bash|zsh)\b\s*($|-c|-s|-)", "pipe into a shell"),
    (r"/dev/tcp/", "reverse shell via /dev/tcp"),
    (r"\bn(c|cat)\b.*\s-e\b", "netcat -e (reverse shell)"),
    (r"\blaunchctl\s+(load|bootstrap)\b", "launchd persistence (launchctl load)"),
    (r"\bcrontab\b", "cron persistence"),
    (r"osascript\b.*login\s*item", "login-item persistence"),
    (r"defaults\s+write\b.*LoginItems", "login-item persistence"),
    (r">>?\s*~?/?(\.zshrc|\.bashrc|\.zprofile|\.bash_profile|\.profile)\b", "write to a shell rc file"),
    (r"\beval\b\s*[\"'`$]", "eval of a dynamic string"),
    (r"\bosascript\b.*-e\b", "osascript (AppleScript execution)"),
]

# commands that specifically target revertly itself
_SELF_TAMPER = [
    (r"rm\s+-[rf]{1,2}\s+[^\n]*\.revertly", "delete the revertly store"),
    (r"\brevertly\s+(uninstall|unbind|pause)\b", "disable revertly"),
    (r"REVERTLY_DISABLE", "bypass revertly (REVERTLY_DISABLE)"),
    (r"chflags\s+nouchg", "clear immutable flag (tamper with sealed evidence)"),
    (r">\s*[^\n]*\.revertly/(paused|config)", "tamper with revertly config/pause"),
]


class Finding:
    def __init__(self, kind, severity, detail, path=None):
        self.kind = kind          # READ | SUSPICIOUS | SELF_TAMPER
        self.severity = severity  # alert | warn
        self.detail = detail
        self.path = path

    def as_dict(self):
        return {"t": time.time(), "kind": self.kind, "severity": self.severity,
                "detail": self.detail, "path": self.path, "via": "hook"}


def _is_sensitive_path(path: str, cfg: Config) -> bool:
    if not path:
        return False
    ap = os.path.abspath(os.path.expanduser(path))
    globs = cfg.tripwire_globs_all() + cfg.self_tamper_globs()
    return match_glob(ap, globs) is not None


def classify(tool_name: str, tool_input: dict, cfg: Config = None) -> List[Finding]:
    """Return the security findings for one tool call. Pure — no I/O."""
    cfg = cfg or Config()
    tool_input = tool_input or {}
    out: List[Finding] = []

    # 1. a read tool aimed at a secret
    if tool_name in _READ_TOOLS:
        for key in ("file_path", "path", "notebook_path"):
            p = tool_input.get(key)
            if p and _is_sensitive_path(p, cfg):
                out.append(Finding("READ", "alert",
                                   f"{tool_name} of sensitive path {p}", p))

    # 2. a shell command — inspect the text
    if tool_name == "Bash":
        cmd = tool_input.get("command") or ""
        low = cmd.lower()
        for tok in _SECRET_TOKENS:
            if tok in low:
                out.append(Finding("READ", "alert",
                                   f"Bash command references a secret ({tok}): {cmd[:120]}"))
                break
        for rx, label in _SELF_TAMPER:
            if re.search(rx, cmd, re.I):
                out.append(Finding("SELF_TAMPER", "alert",
                                   f"{label}: {cmd[:120]}"))
        for rx, label in _SUSPICIOUS:
            if re.search(rx, cmd, re.I):
                out.append(Finding("SUSPICIOUS", "alert",
                                   f"{label}: {cmd[:120]}"))
                break   # one suspicious finding per command (avoid double-alerts)
    return out


# ─────────────────────────── handling (I/O) ───────────────────────────

def _desktop_notify(title: str, body: str) -> None:
    if os.environ.get("REVERTLY_NO_NOTIFY"):
        return
    try:
        import shutil
        import subprocess
        if shutil.which("osascript"):
            safe = body.replace('"', "'")[:200]
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "revertly: {title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _session_dir() -> str:
    """The active session dir, if the shim exported it (hooks run as children
    of the wrapped agent, which inherits the env)."""
    return os.environ.get("REVERTLY_SESSION_DIR") or ""


def read_session_findings(session_dir: str) -> List[dict]:
    """The hook findings recorded for a session (from its hooks.jsonl)."""
    out = []
    p = os.path.join(session_dir, "hooks.jsonl")
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def handle(payload: dict) -> int:
    """Process one hook payload. Logs findings, notifies, returns an exit code.
    ALWAYS 0 in Phase 1 (alert-only — never block the agent). Fail-open."""
    try:
        tool_name = payload.get("tool_name") or payload.get("tool") or ""
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
        findings = classify(tool_name, tool_input)
        if not findings:
            return 0
        sdir = _session_dir()
        sid = os.path.basename(sdir.rstrip(os.sep)) if sdir else "-"
        import sys
        for f in findings:
            paths.append_incident(f.kind, f.detail, session_id=sid)
            print(f"revertly ⚠ {f.kind}: {f.detail}", file=sys.stderr)
            _desktop_notify(f.kind, f.detail)
            if sdir:
                try:
                    with open(os.path.join(sdir, "hooks.jsonl"), "a") as h:
                        h.write(json.dumps(f.as_dict()) + "\n")
                except OSError:
                    pass
    except Exception:
        pass
    return 0


def run_from_stdin() -> int:
    """`revertly hook` entrypoint: read the Claude Code hook JSON from stdin."""
    import sys
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return 0   # unparseable -> never block
    if not isinstance(payload, dict):
        return 0
    return handle(payload)


# ─────────────── Claude Code settings.json integration ───────────────
# We merge a PreToolUse hook that calls `<launcher> hook`. Marker-guarded so it
# installs idempotently and uninstalls cleanly without touching other hooks.

_MATCHER = "Read|Grep|Glob|NotebookRead|Bash"


def claude_settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def _hook_command() -> str:
    return f"{os.path.join(paths.bin_dir(), 'revertly')} hook"


def _is_ours(entry: dict) -> bool:
    for h in (entry or {}).get("hooks", []):
        if "revertly" in (h.get("command") or "") and "hook" in (h.get("command") or ""):
            return True
    return False


def install_claude_hook() -> bool:
    """Merge revertly's PreToolUse hook into ~/.claude/settings.json. Returns
    True if written. Preserves any existing hooks/settings."""
    p = claude_settings_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        settings = {}
        if os.path.isfile(p):
            with open(p) as f:
                settings = json.load(f) or {}
        hooks = settings.setdefault("hooks", {})
        pre = hooks.setdefault("PreToolUse", [])
        pre[:] = [e for e in pre if not _is_ours(e)]     # de-dup our own
        pre.append({"matcher": _MATCHER,
                    "hooks": [{"type": "command", "command": _hook_command()}]})
        with open(p, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except (OSError, ValueError):
        return False


def uninstall_claude_hook() -> bool:
    """Remove revertly's hook from ~/.claude/settings.json (leaves the rest)."""
    p = claude_settings_path()
    if not os.path.isfile(p):
        return False
    try:
        with open(p) as f:
            settings = json.load(f) or {}
        pre = settings.get("hooks", {}).get("PreToolUse", [])
        new = [e for e in pre if not _is_ours(e)]
        if new == pre:
            return False
        settings["hooks"]["PreToolUse"] = new
        if not new:
            settings["hooks"].pop("PreToolUse", None)
        with open(p, "w") as f:
            json.dump(settings, f, indent=2)
        return True
    except (OSError, ValueError):
        return False


def is_claude_hook_installed() -> bool:
    p = claude_settings_path()
    if not os.path.isfile(p):
        return False
    try:
        with open(p) as f:
            settings = json.load(f) or {}
        return any(_is_ours(e) for e in
                   settings.get("hooks", {}).get("PreToolUse", []))
    except (OSError, ValueError):
        return False
