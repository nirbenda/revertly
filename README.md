<div align="center">

# revertly

### a seatbelt, not a cage

Run Claude Code (or any command) exactly as you always do. **revertly** sits invisibly
underneath, records everything it touches, and can undo any of it — one bad edit,
a trashed project, or an agent that decides to `rm -rf`.

![platform](https://img.shields.io/badge/platform-macOS%20(APFS)-black?logo=apple)
![deps](https://img.shields.io/badge/dependencies-zero%20·%20pure%20python%20stdlib-3fb950)
![tests](https://img.shields.io/badge/tests-111%20passing-58a6ff)
![phase](https://img.shields.io/badge/phase%201-local%20·%20offline%20·%20zero%20config-bc8cff)

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
| Agent subtly broke a config, noticed days later | `revertly versions config/app.yaml` → `revertly revert --file config/app.yaml --to v0` — rest of the session's work untouched |
| Agent deleted the project (`rm -rf`, hallucinated cleanup) | `revertly revert` → "restore 1,204 deleted files from clone" → back in seconds |
| Prompt injection planted a LaunchAgent + edited `~/.zshrc` | Both tripwires fired mid-session; `revertly revert <session> ~/Library/LaunchAgents ~/.zshrc` — footprint gone, project work kept |
| Injected agent read `~/.ssh/id_ed25519` | Read-tripwire alerts instantly → kill the session, `revertly log` confirms what was read, rotate that key |
| You regret a revert | Reverts are sessions too: `revertly revert <revert-id>` — **nothing is ever lost** |

## Commands

```
revertly status                 armed? recent sessions, store location
revertly last                   summary of the most recent session (+ integrity check)
revertly log [session] [--tripwires] [--outside] [--tool Edit] [--path X]
revertly find <pattern> [--op delete] [--since 7d]
                                search EVERY session — "what happened to X, when?"
revertly diff [session] [path…] unified diff, pre-image vs current
revertly versions <path>        which sessions can restore this file, and what each did to it
revertly revert [session] [path…|glob…] [--dry-run] [--force] [--yes]
revertly rm <session…> [--force] PERMANENTLY delete sessions (the one destructive command)
revertly ui [--port N]          control panel (timeline/find/diff/revert/live)
revertly verify [session|--all] audit journal hash chains for tampering
revertly gc [--keep 30]         prune old sessions (tripwire-flagged kept longest)
revertly doctor                 health check incl. a security section
revertly pause | resume | config
revertly install [--no-profile] | uninstall [--purge]
```

Reverts are **preview-first** (plan printed, confirmation required),
**conflict-aware** (files you changed *after* the session are flagged, shown as
a 3-way decision, never silently clobbered), and **non-destructive** (every
revert is itself a session — you can always undo the undo). Revert paths
accept **globs**: `revertly revert <session> '*.py'` restores every Python
file the session touched.

The `revertly ui` control panel can do all of it visually: filter sessions and
events, **search every session** for a path (Find tab), view or download any
pre-image, and **execute reverts** (preview → confirm; mutating actions are
CSRF-token-guarded and the server rejects non-loopback hosts). Every UI action
shows its equivalent CLI command.

## Security model — the honest version

revertly runs as *you*, same as the agent — so it can't *cage* a determined,
hijacked agent. What it guarantees instead:

- **Recoverability survives total sabotage.** Snapshot + clone are taken
  *before* the agent starts, and APFS snapshot deletion needs root. Even
  `rm -rf ~/.revertly` can't make damage *permanent*.
- **It can't be disabled quietly.** The journal is a hash chain (`revertly verify`
  detects edits), sealed journals are immutable, every tripwire and self-tamper
  attempt hits a cross-session incident log *and* a desktop notification the
  instant it happens.
- **The watcher can't be silently blinded.** `revertly doctor` flags a config
  that broadens `exclude` to nothing or empties the tripwire set.

Honest ceiling: this is tamper-**evidence** and tamper-**raising**, not
tamper-**proofing**. A determined same-UID attacker can `chflags nouchg` and
delete — but only visibly, and never retroactively. True tamper-*resistance* is
the Tier-2 root daemon / Tier-3 Endpoint Security build — see
[`THREAT-MODEL.md`](THREAT-MODEL.md) §9 for the implemented-vs-roadmap matrix.

**Known Phase-1 limits, by design:** the watcher *polls* the project dir plus a
bounded set of sensitive dirs (whole-`$HOME` and real read-auditing need the
FSEvents/ES backend, later); the shim is an ergonomic default, not a security
boundary; no blocking rules, telemetry, or admin — that's Phase 2.

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

111 tests (including an end-to-end lifecycle test and a security suite) across
model, config, journal, snapshot, clone, watch, tripwire, revert, session, ui,
and hardening. TDD throughout.

## Read more

| Doc | What's in it |
|---|---|
| [`PRODUCT.md`](PRODUCT.md) | Object model, user flows S1–S11, Phase-2 fleet vision, success metrics |
| [`DESIGN.md`](DESIGN.md) | Design principles and product thinking |
| [`TECH-DESIGN.md`](TECH-DESIGN.md) | Architecture, invariants, module map |
| [`THREAT-MODEL.md`](THREAT-MODEL.md) | Adversary tiers, attack trees, implemented-vs-roadmap matrix |
