# Changelog

All notable changes to revertly are documented here. This project is pre-1.0;
until 1.0, minor versions may include breaking changes.

## [Unreleased]

## [0.1.0] — Phase 1 (local)

First public release. Local, offline, macOS (APFS), zero third-party
dependencies. **Experimental** — see the honest limits in `THREAT-MODEL.md`.

### Record & revert
- Wraps `claude` (or any command) and arms a safety net before it runs: APFS
  volume snapshot + copy-on-write pre-image clone + a hash-chained journal.
- Filesystem watcher captures every create / modify / delete / **rename** in the
  project — by the agent, a shell command, or a script it spawns.
- Rename-aware, conflict-aware, non-destructive revert (every revert is itself a
  session you can undo); `revertly restore <file>` one-shot; `find`, `diff`,
  `versions`, `log`.
- Revert plan honors excludes (never touches `.git` / `node_modules`).

### Security
- Sensitive-path **tripwires** (credentials, persistence, revertly's own state
  as `SELF_TAMPER`) with real-time desktop notification + incident log.
- **Claude Code hook** (optional) detects secret reads and suspicious tool calls.
- **Harness-agnostic command guard**: intercepting shims for dangerous commands
  (`curl|sh`, reverse shells, `launchctl`, secret reads, self-tamper) — `alert`
  by default, opt-in `block` that returns a message the agent can act on. Works
  for any agent that shells out. Fail-open.

### Storage lifecycle
- Automatic retention (age + disk cap), `revertly clear` at a safe point,
  `status` disk usage. Live/flagged sessions protected.

### Agents & install
- `install` detects agent CLIs on PATH and binds the ones you choose;
  `bind` / `unbind` / `agents`. Supports Claude Code, Codex, Gemini, Aider,
  Cursor CLI, and any other CLI agent.

### UI
- Local control panel: Timeline, Find, Diff (searchable, binary-aware), Revert
  composer (inspect pre-images without reverting), Storage, live **Security**
  feed, Live.

[Unreleased]: https://github.com/nirbendavid/revertly/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nirbendavid/revertly/releases/tag/v0.1.0
