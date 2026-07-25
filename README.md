# revertly — a seatbelt, not a cage

Run Claude Code (or any command) exactly as you always do. `revertly` sits
invisibly underneath, records everything it touches, and can undo any of it —
one bad edit, a trashed project, or an agent that decides to `rm -rf`.

**Phase 1 (this repo): local, offline, zero-config, zero third-party deps.**
Pure Python 3 stdlib — it runs on stock macOS with nothing to install.

See `DESIGN.md`, `PRODUCT.md`, `THREAT-MODEL.md`, `TECH-DESIGN.md` for the full
thinking. This README is how to use it.

## Requirements

- macOS (APFS) and `python3` — both already on a stock Mac. Nothing else.

## Install

```sh
git clone <this-repo> revertly && cd revertly
./install.sh
```

That's it. `install.sh` installs a `revertly` launcher and a `claude` shim into
`~/.revertly/bin`, and adds that dir to your `PATH` (in a clearly-marked,
reversible block in your shell profile). Open a **new terminal**, then:

```sh
revertly doctor        # confirms the shim is first in PATH, snapshots work, etc.
```

Now use `claude` exactly as before — revertly arms the safety net automatically on
every run. Prefer not to edit your profile? `./install.sh --no-profile` just
prints the one PATH line to add yourself.

## Getting started (60-second tour)

```sh
claude "refactor the parser"     # runs normally; revertly is recording underneath
revertly last                       # what did that session touch? (+ integrity check)
revertly diff                       # unified diff of every change, pre vs now
revertly revert --dry-run           # preview an undo — changes nothing
revertly revert                     # undo the session (asks first; non-destructive)
revertly ui                         # open the visual control panel in your browser
```

Escape hatches, any time:

```sh
REVERTLY_DISABLE=1 claude …         # run once with no net
revertly pause    /  revertly resume   # disarm/rearm without uninstalling
REVERTLY_NO_SNAPSHOT=1 claude …     # skip the APFS snapshot layer for this run
```

## Uninstall

```sh
./uninstall.sh                   # removes shim + launcher + the PATH line
./uninstall.sh --purge           # also delete ~/.revertly (all session history)
```

Uninstalling leaves your real `claude` and your machine exactly as before —
"leaving must be as clean as arriving." Session history is kept by default so a
reinstall still has your revert points; add `--purge` to wipe it.

## What happens on every run

1. **APFS snapshot** of the volume (disaster backstop; skip with
   `REVERTLY_NO_SNAPSHOT=1`). Snapshot deletion needs root — so it already
   outranks a same-user agent.
2. **CoW clone** (`cp -Rc`) of the project dir — the exact pre-image reverts
   restore from.
3. **Watcher** journals every create/write/delete to a hash-chained
   `journal.jsonl`, and fires **tripwires** (credentials, shell rc, launch
   agents, `.env`, and revertly's own files) in real time.
4. On exit: a one-line summary, and everything stays revertible.

## Commands

```
revertly status                 armed? recent sessions, store location
revertly last                   summary of the most recent session (+ integrity check)
revertly log [session] [--tripwires] [--outside] [--tool Edit]
revertly diff [session] [path…] unified diff, pre-image vs current
revertly versions <path>        which sessions hold a pre-image of this file
revertly revert [session] [path…] [--dry-run] [--force] [--yes]
revertly ui [--port N]          local control panel (timeline/diff/revert/live)
revertly verify [session|--all] audit journal hash chains for tampering
revertly gc [--keep 30]         prune old sessions (tripwire-flagged kept longest)
revertly doctor                 health check incl. a security section
revertly pause | resume | config
revertly install [--no-profile] | uninstall [--purge]
```

## Security model (what it does and doesn't protect)

revertly runs as *you*, same as the agent — so it can't *cage* a determined,
hijacked agent. What it guarantees instead:

- **Recoverability survives even total sabotage.** The snapshot + clone are
  taken *before* the agent starts, and APFS snapshot deletion needs root — so
  the disaster backstop is already out of a same-user agent's reach. Even
  `rm -rf ~/.revertly` can't make damage *permanent*.
- **You can't be disabled quietly.** The journal is a **hash chain** (edits are
  detectable — `revertly verify`), sealed journals are made **immutable**
  (`chflags uchg`, so a naive `rm`/overwrite is denied), every tripwire and
  self-tamper attempt is written to a cross-session **incident log** and fired
  as a **desktop notification** the instant it happens, and revertly's own files
  + your shell rc are the highest-severity `SELF_TAMPER` tripwires.
- **The watcher can't be silently blinded.** `revertly doctor` flags a config
  that broadens `exclude` to nothing or empties the tripwire set.

Honest ceiling: this is tamper-**evidence** and tamper-**raising**, not
tamper-**proof**. A determined same-UID attacker can `chflags nouchg` and delete
— but only visibly, and never retroactively (the hash chain + root-gated
snapshot see to that). True tamper-**resistance** is the Tier-2 root daemon /
Tier-3 Endpoint Security build — see `THREAT-MODEL.md §9` for the full
implemented-vs-roadmap matrix.

Revert is **non-destructive**: every revert is itself a session, so you can
always `revertly revert <revert-id>` to undo an undo. Files changed *after* a
session are flagged as conflicts and never silently overwritten (use `--force`).

## Known Phase-1 limits (honest, by design)

- The watcher **polls** the project dir + a bounded set of sensitive dirs.
  Whole-`$HOME` coverage and real read-auditing need the FSEvents/`fanotify`
  backend (later). `/etc` is not polled.
- The shim is an **ergonomic default, not a security boundary** — a same-user
  agent can bypass or disable it. Recoverability (root-gated snapshot) and
  tamper-evidence (hash chain) survive that; true tamper-*resistance* needs the
  Tier-2 root daemon / Tier-3 Endpoint Security build (see `THREAT-MODEL.md`).
- No telemetry, admin, or blocking rules — that's Phase 2.

## Develop / test

```sh
./verify.sh        # py_compile gate + full unittest suite + e2e smoke
```

111 tests (incl. an end-to-end lifecycle test and a security suite) across
model, config, journal, snapshot, clone, watch, tripwire, revert, session, ui,
and hardening. TDD throughout.
