<div align="center">

<img src="docs/assets/hero.svg" width="840" alt="revertly — a seatbelt for AI coding agents, not a cage">

<p><b>Let the agent go full speed. Undo anything it does. Catch the sketchy stuff live.</b></p>

<p>
Run <b>Claude Code · Codex · Cursor</b> exactly as you always do — revertly sits underneath,
snapshots your project <i>before</i> the agent starts, records every file it (or anything it
spawns) touches, and lets you <b>revert any of it</b>: one file, one session, or a whole
<code>rm -rf</code>. It <b>alerts &amp; blocks</b> secret reads and dangerous commands the instant
they happen. 100% local — no accounts, no network, nothing leaves your machine.
</p>

![CI](https://github.com/nirbenda/revertly/actions/workflows/ci.yml/badge.svg)
![platform](https://img.shields.io/badge/macOS-APFS-0B0E14?logo=apple)
![agents](https://img.shields.io/badge/agents-Claude·Codex·Gemini·Aider·Cursor-3DDC97)
![deps](https://img.shields.io/badge/deps-zero·pure_stdlib-1FB6C4)
![tests](https://img.shields.io/badge/tests-228_passing-3DDC97)
![license](https://img.shields.io/badge/license-MIT-8fa8a0)

</div>

> ⚠️ **Phase 1 — experimental.** Local, **macOS (APFS) only**, single-machine.
> It's a developer safety net that's **tamper-evident, not tamper-proof** (a
> same-UID/hijacked agent can be *loud and logged*, not *stopped* — see
> [`THREAT-MODEL.md`](THREAT-MODEL.md)). Not yet a managed/fleet security
> control. Try it, break it, tell us — but don't deploy it as your only
> guardrail.

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
- **Agents:** `install` **detects the agent CLIs on your PATH and binds the
  ones you choose** — **Claude Code, Codex, Gemini, Aider, Cursor CLI** and more
  (`revertly agents` to list, `revertly bind <cmd>` for any other). Every bound
  agent gets the filesystem watcher **and** the harness-agnostic command guard
  (secret-read / `curl|sh` / persistence detection). Claude Code *additionally*
  gets a hook for higher-fidelity tool-call inspection. GUI/IDE agents (the
  Cursor app, Copilot in VS Code) can't be shimmed — an ambient watch mode for
  those is planned.

## Install

Stock macOS is all you need — APFS and `python3` are already there.

```sh
git clone <this-repo> revertly && cd revertly
./install.sh           # detects your agent CLIs and asks which to bind
revertly doctor        # new terminal first — confirms shim order, snapshots, store health
```

`install.sh` puts a `revertly` launcher in `~/.revertly/bin`, **detects the
agent CLIs on your PATH** (Claude Code, Codex, Gemini, Aider, Cursor CLI, …) and
lets you pick which to bind, then adds the bin dir to your shell profile in one
clearly-marked, reversible block (`--no-profile` to add the PATH line yourself;
`--all` / `--agents claude,aider` to skip the prompt). From then on, each bound
agent works exactly as before — the net arms itself on every run.

```sh
revertly agents                 # which agents are on PATH, and which are bound
revertly bind codex             # bind another one anytime
revertly unbind aider           # or remove one
```

**Works with any CLI agent** — the recorder wraps *any* command, so binding is
just a shim per command name. GUI/IDE agents (Cursor app, Copilot in VS Code)
aren't shimmable (no command to wrap); an ambient watch mode for those is planned.

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
| Injected agent tries to **read** `~/.ssh/id_rsa` or `.env` | With the **Claude Code hook** installed, the read fires a `READ` alert + incident the moment the agent's tool call runs — you can kill the session and rotate the key |
| Agent runs `curl … \| bash`, sets a LaunchAgent, or `rm -rf ~/.revertly` | The hook flags it as `SUSPICIOUS` / `SELF_TAMPER` before it runs; every session ends with a security summary (`N sensitive reads, M suspicious commands`) |
| You regret a revert | Reverts are sessions too: `revertly revert <revert-id>` — **nothing is ever lost** |

> **Scope, stated plainly.** Three layers, from most to least agnostic:
> - **Filesystem watcher** — catches every create/modify/delete/rename in the
>   project (by *anything* — the agent, a shell command, or a script it spawns).
>   Revert restores from the pre-image **clone of the project dir**; files
>   *outside* it are alerted but not content-reverted (that's the APFS snapshot's
>   job — a manual Recovery restore). Fully **agnostic** — no per-agent wiring.
> - **Command guard** — revertly prepends intercepting shims for dangerous
>   commands (`curl`, `bash`, `nc`, `launchctl`, `crontab`, …) to the agent's
>   `PATH`, so a `curl … | bash`, a `launchctl load`, or a `cat ~/.ssh/id_rsa`
>   is seen — and, in `block` mode, **refused with a message the agent reads and
>   corrects on** — no matter *which* agent runs it. Fully **agnostic**. Set
>   `[guard] mode = "block"` in the config to enforce (default is `alert`).
> - **Claude Code hook** (optional, installed with the `claude` binding) — sees
>   the agent's structured **tool calls** for higher-fidelity read/command
>   detection where the harness supports hooks.
>
> **Honest ceiling:** the guard and hook are *userspace* — a determined agent
> evades them by calling `/usr/bin/curl` directly or doing the read/network
> in-process (Python `open()`/`urllib`). They are tamper-**raising**, not a
> boundary. The real boundary is the kernel: an opt-in `sandbox-exec` profile
> and, ultimately, the Endpoint-Security build (Tier 3).

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
revertly ui [--port N]           visual control panel (timeline/find/diff/revert/security/storage/live)
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
on its own: under `[retention]` in `~/.revertly/config.toml` set
`sessions = "30d"` (age) and/or `max_disk = "10GB"` (cap) — and each session-end
prunes the oldest non-flagged sessions to stay under them.
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
mtime-preserving edits can slip; `/etc` isn't polled). The hook layer sees the
**agent's own tool calls** — a binary/subprocess the agent spawns that reads a
secret or opens a network socket is invisible until the Endpoint-Security build.
The hook is **alert-only** (blocking is Phase 2). The shim is an ergonomic
default, not a security boundary; no telemetry or admin — Phase 2.

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
