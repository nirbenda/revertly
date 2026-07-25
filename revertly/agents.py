"""revertly.agents — the coding-agent CLIs revertly can wrap, and PATH detection.

revertly's recorder is agent-agnostic (it wraps ANY command), but the install
flow is friendlier if it can *detect* which agent CLIs you actually have and
offer to bind them. This is the registry + detection behind that.

`revertly bind <cmd>` accepts any command name, so this list is just the set we
recognize and propose by default — not a restriction.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from revertly import paths

# (command, human name). CLI agents only — GUI/IDE agents (Cursor app, Copilot
# in VS Code) can't be PATH-shimmed; they need the ambient watch mode.
KNOWN_AGENTS: List[Tuple[str, str]] = [
    ("claude", "Claude Code (Anthropic)"),
    ("codex", "Codex CLI (OpenAI)"),
    ("gemini", "Gemini CLI (Google)"),
    ("aider", "Aider"),
    ("cursor-agent", "Cursor CLI"),
    ("amp", "Amp (Sourcegraph)"),
    ("opencode", "OpenCode"),
    ("goose", "Goose (Block)"),
    ("qwen", "Qwen Code"),
]

_SHIM_MARKER = "revertly shim for"   # written into every shim we install


def display_name(cmd: str) -> str:
    for c, name in KNOWN_AGENTS:
        if c == cmd:
            return name
    return cmd


def real_on_path(cmd: str) -> Optional[str]:
    """The real executable for `cmd` on PATH, skipping revertly's own bin dir
    (so an already-installed shim never masks the real binary). None if absent.
    """
    shim_dir = os.path.realpath(paths.bin_dir())
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.realpath(d) == shim_dir:
            continue
        cand = os.path.join(d, cmd)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def detect() -> List[Tuple[str, str, str]]:
    """Known agents present on PATH, as (command, human name, real_path)."""
    out = []
    for cmd, name in KNOWN_AGENTS:
        rp = real_on_path(cmd)
        if rp:
            out.append((cmd, name, rp))
    return out


def is_revertly_shim(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _SHIM_MARKER in f.read(400)
    except OSError:
        return False


def bound_agents() -> List[str]:
    """Command names currently bound (a revertly shim exists in bin/)."""
    bd = paths.bin_dir()
    if not os.path.isdir(bd):
        return []
    out = []
    for f in sorted(os.listdir(bd)):
        p = os.path.join(bd, f)
        if f != "revertly" and os.path.isfile(p) and is_revertly_shim(p):
            out.append(f)
    return out
