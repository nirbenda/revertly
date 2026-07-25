# revertly — a seatbelt, not a cage

Run Claude Code exactly as you always do. `revertly` sits invisibly underneath,
records everything it touches, and can undo any of it — up to and including
"it deleted the filesystem."

No kernel extensions. No entitlements. No containers. No daemons. No root.
Every mechanism below is a boring, decades-proven OS primitive glued together
by a small wrapper. That is the whole point.

## The one design decision that matters

**`revertly` interposes on the `claude` command itself.** A shim named `claude`
sits earlier in `PATH` (or a shell alias) and exec's the real binary after
arming the safety net. The user types `claude` like always. Zero workflow
change, zero training, uninstall = delete one line.

```
you type: claude "fix the tests"
              │
              ▼
   ~/.revertly/bin/claude          (shim, first in PATH)
              │  1. arm safety net (snapshot + clone + watcher)
              │  2. exec real claude, args passed through untouched
              │  3. on exit: seal the session journal, print 1-line summary
              ▼
   /opt/homebrew/bin/claude     (the real thing, completely unaware)
```

## The safety net: three cheap layers, deepest first

### Layer 1 — APFS volume snapshot (the disaster backstop)
At session start: `tmutil localsnapshot`. One command, ~instant,
copy-on-write, zero space until things change, no sudo. This is the
"even if it wipes the whole filesystem" guarantee. Snapshots auto-expire
in ~24h — fine, this layer is for *immediate* catastrophe, not archives.

### Layer 2 — APFS clonefile of the project (the precise pre-image)
At session start: `cp -Rc $PWD → ~/.revertly/sessions/<id>/clone/`.
`clonefile(2)` makes this near-instant and near-zero-cost regardless of
project size (CoW blocks, not copies). Now every file's exact pre-session
content is durably on disk. This is what fine-grained revert restores from —
one file, one directory, or everything.

### Layer 3 — FSEvents journal (the audit trail)
A watcher (child process of the shim — lives and dies with the session,
no daemon) subscribes to FSEvents on `$HOME` with per-file granularity.
Every create/write/rename/unlink during the session window lands in
`journal.jsonl`. Userspace API, no privileges. Noise paths
(`~/Library/Caches`, `~/.revertly`, browser dirs…) are excluded by config.

Honest limitation, stated up front: FSEvents does not attribute a PID.
We scope by *session time window* + exclusions instead of pretending to
track processes. For "what did this Claude session touch," that's the
right 95% answer at 1% of the complexity. (On Linux, `fanotify` gives
real PID attribution and `overlayfs` gives true redirect-on-write — the
port is an upgrade, not a rewrite.)

### Optional Layer 4 — Claude Code hooks (intent, not enforcement)
The shim can inject `PreToolUse`/`PostToolUse` hooks via `--settings` so
the journal records *why* a file changed ("Edit tool, session xyz") next
to *what* changed. Pure annotation. Enforcement stays out of v1.

## The interface

```
claude …                # normal usage — net armed automatically
revertly status            # is the net armed? recent sessions
revertly log  [session]    # what did that session touch
revertly diff [session]    # pre-image vs current, plain unified diff
revertly revert [session] [path…]   # restore file(s), a dir, or everything
revertly gc                # prune old sessions (also auto after N days)
REVERTLY_DISABLE=1 claude  # escape hatch, always
```

Session store — plain files, inspectable with `ls` and `cat`, no database:

```
~/.revertly/sessions/2026-07-25T10-30-00_a1b2/
├── meta.json        # cwd, argv, start/end, snapshot name, exit code
├── clone/           # CoW pre-image of the project dir
└── journal.jsonl    # one FSEvents record per line
```

## Recovery, worst case first

| Damage                         | Recovery                                        |
|--------------------------------|-------------------------------------------------|
| Bad edit to one file           | `revertly revert <session> path/to/file`           |
| Trashed the project dir        | `revertly revert <session>` (restores from clone)  |
| Touched files outside project  | journal names them; restore from APFS snapshot  |
| Nuked the whole filesystem     | boot recovery → restore APFS local snapshot     |

## What v1 deliberately does NOT do

- No blocking/AUTH mode. Observe and revert; don't referee. A tool that
  can silently veto operations is a debugging nightmare.
- No process attribution on macOS. See Layer 3.
- No network monitoring. Different problem, different tool.
- No GUI, no service, no config beyond one exclusions file.

Each of these is an *add later if genuinely needed*, not a v1 gap.

## Why this passes the Linus test

1. **Userspace only.** The kernel already has CoW snapshots and an FS
   event journal. Use them; don't reimplement them in a system extension.
2. **Does one thing.** Record and revert. Not sandbox, not firewall,
   not policy engine.
3. **Plain text everywhere.** The whole state is `cat`-able.
4. **No lock-in, no residue.** Remove the shim, nothing else changes.
   The real `claude` never knew revertly existed.
5. **Fails safe.** If the shim crashes, it exec's the real claude anyway —
   worst case you ran Claude the way everyone runs it today.
