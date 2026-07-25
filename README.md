<div align="center">

# revertly

### a seatbelt for AI coding agents — not a cage

You run **Claude Code** exactly as you always do. **revertly** sits underneath it:
it snapshots your project *before* the agent starts, records every file the agent
(or anything it runs) creates, changes, deletes, or renames, and lets you undo any
of it — one file, one session, or a whole `rm -rf`. It also trips an alert the
instant the agent touches something sensitive (SSH keys, `.env`, shell config).

Everything is local. No accounts, no network, nothing leaves your machine.

![platform](https://img.shields.io/badge/macOS-APFS-black?logo=apple)
![works with](https://img.shields.io/badge/works%20with-Claude%20Code-bc8cff)
![deps](https://img.shields.io/badge/dependencies-zero%20·%20pure%20python%20stdlib-3fb950)
![tests](https://img.shields.io/badge/tests-191%20passing-58a6ff)
![phase](https://img.shields.io/badge/phase%201-local%20·%20offline-3fb950)

</div>

<br>

<p align="center">
  <img src="docs/assets/demo.svg" width="880" alt="Animated demo: an agent session breaks the parser and deletes README.md; revertly diff shows exactly what changed; revertly revert restores everything.">
</p>

<p align="center"><sub>↑ real output from a real session — only the paths were shortened.</sub></p>

## Why

Sandboxes make agents safe by making them useless. revertly makes the **machine
forgiving** instead of making the **agent constrained**: the agent runs at full
speed, with no cage — and nothing it does can be permanent or invisible.

- **Permanence — gone.** Every file the session creates, edits, or deletes has a
  pre-image. `revertly revert` erases a session's entire footprint, including the
  persistence tricks (shell rc, LaunchAgents) a human would never think to check.
- **Invisibility — gone.** Sensitive paths are tripwired: touch `~/.ssh`, `.env`,
  your shell rc — you get a desktop notification *while it happens*, not a
  forensic surprise weeks later.

## How it works (the mental model)

Four nouns, and every command is a view onto one of them:

- **Session** — one run of `claude` under revertly. It gets an id and a name
  from your prompt (`2026-07-25_a1b2 "fix-the-tests"`). This is the unit you
  inspect and undo.
- **Pre-image (clone)** — a copy-on-write snapshot of your project taken *before*
  the agent starts. Reverts restore from this. On APFS it's near-instant and
  shares disk blocks, so it's cheap — but deleted files become real bytes, which
  is why the store grows and there's a **Storage** view to prune it.
- **Journal** — an append-only, hash-chained log of every file event in the
  session. It's what `log`, `find`, and `diff` read, and `verify` audits for
  tampering.
- **Tripwire** — a watched sensitive path (credentials, shell config,
  LaunchAgents, revertly's own files). Touching one fires a desktop notification
  and an incident-log entry the instant it happens.

So: **the agent works** → revertly **records** (journal) against a **pre-image**
(clone) → you **inspect** (`last`/`diff`/`find`) and **undo** (`restore`/`revert`)
→ and **clean up** (`status`/`clear`) when you're at a safe point. Nothing lives
only inside a tool — it's all plain files under `~/.revertly`.

## Requirements & compatibility

- **OS:** macOS with an **APFS** volume (the default on modern Macs). The
  copy-on-write clones and volume snapshots are APFS features. Linux/other
  filesystems are on the roadmap (the watcher and revert engine are portable;
  the clone/snapshot layer is what's macOS-specific today).
- **Runtime:** `python3` — already on a stock Mac. **Zero third-party dependencies.**
- **Agent:** **Claude Code** works out of the box — `install.sh` drops in a
  `claude` shim that arms the net on every run. Under the hood revertly wraps
  *any* command (`revertly shim -- <cmd> …`), so it isn't Claude-specific;
  first-class shims for other agents (Cursor, Codex CLI, Aider, Antigravity, …)
  are a natural next step and planned. The recording is filesystem-level, so it
  already captures whatever *any* of them — or a script they run — does on disk.

## Install

Stock macOS is all you need — APFS and `python3` are already there.

```sh
git clone <this-repo> revertly && cd revertly
./install.sh
revertly doctor        # new terminal first — confirms shim order, snapshots, store health
```

`install.sh` puts a `revertly` launcher and a `claude` shim in `~/.revertly/bin`
and adds one clearly-marked, reversible block to your shell profile
(`--no-profile` to skip that and add the PATH line yourself). From then on,
`claude` works exactly as before — the net arms itself on every run.

## The 60-second tour

```sh
claude "refactor the parser"     # runs normally; revertly records underneath
revertly last                    # what did that session touch? (+ integrity check)
revertly diff                    # unified diff of every change, pre vs now
revertly revert --dry-run        # preview the undo — changes nothing
revertly revert                  # undo the session (asks first; non-destructive)
revertly ui                      # visual control panel: timeline / diff / revert / live
```

Escape hatches, any time:

```sh
REVERTLY_DISABLE=1 claude …      # run once with no net
revertly pause / revertly resume # disarm / rearm without uninstalling
REVERTLY_NO_SNAPSHOT=1 claude …  # skip the APFS snapshot layer for one run
```

## How the net is built

Before the wrapped command ever executes, four independent layers arm — each one
covers for the failure (or sabotage) of the ones above it:

<p align="center">
  <img src="docs/assets/layers.svg" width="880" alt="The four layers: tripwires (real-time alerts), hash-chained journal (tamper-evident), copy-on-write clone (the revert source), and a root-gated APFS volume snapshot (disaster backstop).">
</p>

1. **APFS snapshot** of the volume — taken first, deletable only by root, so the
   disaster backstop already outranks a same-user agent.
2. **CoW clone** (`cp -Rc`) of the project — the byte-exact pre-image every
   revert restores from. Copy-on-write means it costs almost nothing.
3. **Watcher + journal** — every create/write/delete lands in an append-only,
   **hash-chained** `journal.jsonl`; sealed immutable (`chflags uchg`) on exit.
4. **Tripwires** fire in real time on credentials, shell rc, LaunchAgents,
   `.env` — and on revertly's own files (`SELF_TAMPER`, highest severity).

On exit you get one summary line, and everything stays revertible.

## When it saves you

<p align="center">
  <img src="docs/assets/tripwire.svg" width="880" alt="Animated demo: an injected session writes a LaunchAgent; tripwires fire a desktop notification mid-session; one revert command removes the footprint while keeping legitimate work.">
</p>

| The day something goes wrong | What you do |
|---|---|
| Agent subtly broke a config, noticed days later | `revertly versions config/app.yaml` → `revertly revert <session> config/app.yaml` — rest of the session's work untouched |
| Agent deleted the project (`rm -rf`, hallucinated cleanup) | `revertly revert` → "restore 1,204 deleted files from clone" → back in seconds |
| Agent moved/renamed files (even a `A→B→C` chain) | Reverts are rename-aware: `revertly revert <session> A.txt` restores A and removes the moved copy — no stranded duplicate |
| Injected agent wrote a LaunchAgent or edited `~/.zshrc` | A **tripwire fires** mid-session (notification + incident log) so you can stop it. In-project footprint reverts cleanly; **out-of-project files are alerted, not auto-reverted** — restore those from the APFS snapshot (see below) |
| You regret a revert | Reverts are sessions too: `revertly revert <revert-id>` — **nothing is ever lost** |

> **Scope, stated plainly:** revert restores from the pre-image **clone of the
> project directory**. Files *outside* the project (`~/.zshrc`, LaunchAgents,
> `/etc`) are **watched and alerted** — you'll know the instant one is touched —
> but not journaled with a pre-image, so revert can't roll their *contents*
> back. That's the volume-level APFS snapshot's job (a manual Recovery
> restore). Read-auditing (catching a *read* of `~/.ssh/id_rsa`) needs the
> Endpoint Security backend and is **not** in Phase 1 — see `THREAT-MODEL.md`.

## Commands

Grouped by what you're trying to do (run `revertly <command> -h` for details
and examples on any of them):

**See what the agent just did**
```
revertly last                    summary of the most recent session (+ integrity check)
revertly diff [session] [path…]  unified diff of every change, pre-image vs now
revertly log [session] [--tripwires] [--outside] [--tool Edit] [--path X]
revertly find <pattern> [--op delete] [--since 7d]   "what happened to X, and when?"
```

**Undo**
```
revertly restore <path>          give one file/dir back — no session id needed
revertly revert [session] [path…|glob…] [--dry-run] [--force] [--yes]
revertly versions <path>         which sessions can restore this file, and what each did
```

**Manage stored history** (clones pile up — see the next section)
```
revertly status                  armed? disk usage, recent sessions
revertly clear [--all | --before <id|7d|date> | --keep Nd] [--include-flagged]
revertly gc [--keep N] [--before X]   apply the retention policy (age + disk cap)
revertly rm <session…> [--force]      permanently delete specific sessions
```

**Set up, inspect, and check health**
```
revertly install [--no-profile]  add the claude shim to your PATH
revertly ui [--port N]           visual control panel (timeline/find/diff/revert/storage/live)
revertly doctor                  is the net armed and healthy? (incl. a security section)
revertly verify [session|--all]  audit journal hash chains for tampering
revertly pause | resume          disarm / rearm without uninstalling
revertly config | uninstall [--purge]
```

## Managing stored history

revertly keeps a pre-image **clone** of the project plus a journal for every
`claude` run, so deleted files and diffs accumulate. Three ways to keep that in
check — deletion is the one irreversible operation, so all of them protect the
**live session** and **flagged (evidence)** sessions, and log every prune:

```sh
revertly status                     # how much disk, per-session sizes, cap %
revertly clear --before <session>   # "delete everything before this point"
revertly clear --all                # wipe all history, keep revertly armed
revertly clear --keep 7d            # drop anything older than 7 days
```

`clear --before` takes a session id, an age (`7d`/`12h`), or a date
(`YYYY-MM-DD`); add `--include-flagged` to also drop evidence sessions,
`--dry-run` to preview, `--yes` to skip the prompt. **Automatic retention** runs
on its own: set `retention_days` / `max_disk_gb` in `~/.revertly/config.toml`
and each session-end prunes the oldest non-flagged sessions to stay under them.
The **Storage tab** in `revertly ui` does all of this visually — usage bar,
clear-before picker with a freed-space preview, and a typed confirmation before
anything evidence-bearing is removed.

Reverts are **preview-first** (plan printed, confirmation required),
**conflict-aware** (files changed *after* the session — by edit **or by a
later move**, which ctime betrays even though `mv` preserves mtime — are
flagged, never silently clobbered), and **non-destructive** (every revert is
itself a session — you can always undo the undo). Revert paths accept
**globs** (`'*.py'`), and reverts are **rename-aware**: moves are journaled
as `rename old → new`, and reverting a path follows its whole rename chain —
even across sessions — so `A → B → C` collapses to "restore A, remove C"
instead of stranding duplicates. Apply order is deletes-first, so
file↔directory swaps revert cleanly.

The `revertly ui` control panel can do all of it visually: filter sessions and
events, **search every session** for a path (Find tab), view or download any
pre-image, and **execute reverts** (preview → confirm; mutating actions are
CSRF-token-guarded and the server rejects non-loopback hosts). Every UI action
shows its equivalent CLI command.

## Security model — the honest version

revertly runs as *you*, same as the agent — so it can't *cage* a determined,
hijacked agent. What it does raise the cost of:

- **Recoverability is pre-positioned.** The snapshot (if enabled) and the clone
  are taken *before* the agent starts. Deleting the clone or the store while a
  session is live **fires a SELF_TAMPER alert** (dedicated sentinels watch the
  store, the shim, and shell-rc files); a same-UID attacker can still remove
  them, but not silently.
- **Tamper leaves evidence.** The journal is a hash chain, sealed journals are
  made immutable, and seal writes an immutable anchor so `revertly verify`
  catches even a clean-prefix **truncation** — not just in-place edits. Every
  tripwire, self-tamper, and *disable* action (`pause`, `rm`, `REVERTLY_DISABLE`)
  writes a cross-session incident line and a desktop notification.
- **Blinding is announced.** A config that broadens `exclude` to nothing or
  empties the tripwire set is flagged at **arm time** (and by `revertly doctor`).
- **Secrets in the store are contained.** The store root is `0700`, so other
  local users can't read the project clones it holds.

**Honest ceiling — read this.** This is tamper-**evidence** and
tamper-**raising**, not tamper-**proofing**. A same-UID attacker can `chflags
nouchg` then delete/rewrite, or bypass the shim entirely — the guarantees above
make that *loud and logged*, not *impossible*. Recoverability of *out-of-project*
damage depends on the APFS snapshot, which is skippable (`REVERTLY_NO_SNAPSHOT`)
and not wired into `revertly revert` (it's a manual Recovery restore). True
tamper-*resistance* and a root-owned mirror are the Tier-2 daemon / Tier-3
Endpoint Security build — see [`THREAT-MODEL.md`](THREAT-MODEL.md) §9 for the
implemented-vs-roadmap matrix.

**Known Phase-1 limits, by design:** the watcher *polls* (sub-second churn and
mtime-preserving edits can slip; `/etc` isn't polled); **reads are not detected**
(no read-tripwires — that needs the ES backend); the shim is an ergonomic
default, not a security boundary; no blocking rules, telemetry, or admin — Phase 2.

## Uninstall

```sh
./uninstall.sh                   # removes shim + launcher + the PATH line
./uninstall.sh --purge           # also delete ~/.revertly (all session history)
```

Leaving must be as clean as arriving: your real `claude` and your machine end up
exactly as before. Session history is kept by default so a reinstall still has
your revert points.

## Develop / test

```sh
./verify.sh        # py_compile gate + full unittest suite + e2e smoke
```

191 tests (including an end-to-end lifecycle test, a security/tamper suite,
move-chain/data-loss regressions, and the retention planner) across model,
config, journal, snapshot, clone, watch, tripwire, search, revert, retention,
session, cli, ui, and hardening. TDD throughout.

## Read more

| Doc | What's in it |
|---|---|
| [`PRODUCT.md`](PRODUCT.md) | Object model, user flows S1–S11, Phase-2 fleet vision, success metrics |
| [`DESIGN.md`](DESIGN.md) | Design principles and product thinking |
| [`TECH-DESIGN.md`](TECH-DESIGN.md) | Architecture, invariants, module map |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | Adversary tiers, attack trees, implemented-vs-roadmap matrix |
