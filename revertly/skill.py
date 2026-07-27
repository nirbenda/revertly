"""Agent-facing instructions: a Claude Code Skill + a portable AGENTS.md
snippet that teach a coding agent how to drive revertly on the user's behalf
("what did you change?", "undo that", "oops, put it back").

Both are opt-in — placed only when the user runs `revertly skill --install`
or `revertly install --skill`. Nothing here changes how revertly records or
reverts; it only makes the existing CLI discoverable to the agent.
"""
import os
import shutil

# Claude Code reads skills from ~/.claude/skills/<name>/SKILL.md. The frontmatter
# `description` is what the model matches on to decide when to load the skill.
SKILL_MD = """\
---
name: revertly
description: >-
  Inspect and undo file changes recorded by revertly, the local safety net
  wrapping this coding session. Use whenever the user asks what changed, to
  undo/revert/roll back edits or deletions, to recover a lost or deleted file,
  or says "oops" / "put it back" / "redo that".
---

# revertly — inspect & undo this session's file changes

`revertly` is running underneath this session. It took a copy-on-write snapshot
of the project *before* work began and records every file created, edited,
deleted, or renamed. You can inspect and undo any of it with the `revertly`
CLI. Everything is **local**, **preview-first**, and **itself reversible** —
no change you make with it is ever lost.

## When to use this
- "what did you change?" / "what happened to <file>?" → **inspect**
- "undo that" / "revert" / "roll it back" / "put it back" → **undo**
- you (the agent) deleted or broke files → **recover**
- "oops, I didn't mean to undo that" / "redo" → **redo**

## Inspect (read-only, safe to run anytime)
- `revertly last` — summary of the most recent session, with the exact
  follow-up commands to run next
- `revertly diff [session] [path…]` — unified diff, pre-image vs the file now
- `revertly find <pattern> [--op delete] [--since 7d]` — "what happened to X,
  and when", across every session (prints a recover command per hit)
- `revertly log [session]` — every recorded event
- `revertly versions <path>` — which sessions can restore this file
- `revertly status` — what's armed, disk usage, recent sessions

## Undo (mutating — all preview-first; each undo is a new session you can reverse)
- `revertly restore <path>` — bring one file/dir back (no session id needed)
- `revertly revert [session] [path…|glob]` — undo a whole session, or just some
  paths. Use `--dry-run` to preview, `--yes` to skip the confirm prompt.
- `revertly undo` — one-shot: restore the last runaway-deletion burst
- `revertly redo` — re-apply what your most recent `revertly revert` undid

## How revert behaves (important)
- It restores the **exact pre-session state**: files the session *created* are
  **deleted**, files it *deleted* are **restored**, edits are rolled back. This
  is correct — it's not data loss; every revert prints an id to reverse it.
- It is **conflict-aware**: a file you changed *after* the session is flagged,
  never silently overwritten.
- **Preview first.** Unless the user said "just do it", run with `--dry-run`
  (or the UI) and show the plan before applying.

## Do not
- Do **not** disable revertly — `revertly pause`, `revertly uninstall`, or
  deleting `~/.revertly`. That removes the user's safety net and is logged as
  tampering. If the user truly wants it off, tell them to run it themselves.
"""

# Harness-neutral version for a project's AGENTS.md / CLAUDE.md (Codex, Cursor,
# and others read AGENTS.md). Deliberately short — a pointer, not a manual.
AGENTS_MD = """\
## revertly — local undo / safety net

This machine runs `revertly`, which records every file change in a session and
can undo it. When the user asks what changed, or to undo / revert / recover / redo:

- **inspect:** `revertly last` · `revertly diff [session] [path]` · `revertly find <path>`
- **undo:** `revertly restore <path>` · `revertly revert [session] [paths] [--dry-run]`
  · `revertly undo` (last mass-deletion) · `revertly redo`

All actions are local and **preview-first**; every undo is itself a session you
can reverse (the CLI prints the id). Reverting deletes files the session created
and restores ones it deleted — that's expected. **Never** disable revertly
(`pause` / `uninstall` / removing `~/.revertly`).
"""


def claude_skill_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "skills", "revertly")


def skill_installed() -> bool:
    return os.path.isfile(os.path.join(claude_skill_dir(), "SKILL.md"))


def install_claude_skill() -> str:
    """Write ~/.claude/skills/revertly/SKILL.md. Returns the path."""
    d = claude_skill_dir()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "SKILL.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(SKILL_MD)
    return p


def uninstall_claude_skill() -> bool:
    """Remove the skill dir. Returns True if something was removed."""
    d = claude_skill_dir()
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False
