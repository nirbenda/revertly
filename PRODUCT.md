# revertly — Product Definition (CPO cut)

**One-liner:** Run AI coding agents at full speed on real machines. Everything they
touch is journaled, versioned, and revertible — from one bad edit to a wiped disk.

**Positioning:** revertly is not a sandbox and never will be. Sandboxes make agents
safe by making them useless. revertly makes the *machine* forgiving instead of making
the *agent* constrained. Seatbelt, not cage.

**Two phases:**
- **Phase 1 — Local:** single-user, fully offline, complete control loop
  (observe → inspect → revert). Ships value on day one with zero infrastructure.
- **Phase 2 — Fleet:** opt-in telemetry, admin console, centrally-managed rules.
  Turns the same local primitive into an org-wide safety layer.

---

## 1. Object model (the nouns users think in)

| Noun | What it is | Lifetime |
|---|---|---|
| **Session** | One run of `claude` under revertly. Has ID + auto-name from the prompt (`2026-07-25_a1b2 "fix-the-tests"`). | Until GC (default 30 days) |
| **Journal** | Append-only JSONL of every file event (FSEvents) + every tool call (hooks) in a session. | With the session |
| **Version** | A file's content at a point in time. `v0` = session start (from clone); `v1..vn` = after each agent tool-touch (CoW clone per touch — cheap). | With the session |
| **Checkpoint** | A named marker in the timeline ("after tool call #12"). Revert targets are checkpoints, not raw timestamps. | With the session |
| **Snapshot** | The APFS volume snapshot from session start. Disaster backstop only. | ~24h (OS-managed) |
| **Tripwire** | A watched sensitive path/pattern. Touching one fires an alert (Phase 1) or a rule (Phase 2). | Config |

Everything is plain files under `~/.revertly/`. The UI and CLI are views over the
same files; nothing exists only inside a tool.

---

## 2. Control surface (how the user drives it)

### 2.1 Ambient (zero-effort — the default experience)
- `claude …` works exactly as before. The shim arms the net silently.
- **Session-end summary line** printed after claude exits:
  `revertly: 37 files touched (35 in project, 2 outside ⚠) — 'revertly last' to inspect`
- **Tripwire alerts** surface immediately as a macOS notification *and* an inline
  terminal line — never only in a log nobody reads.

### 2.2 CLI (the power surface — everything scriptable)
```
revertly status                      # armed? current/recent sessions, disk usage
revertly last                        # summary of most recent session (the 90% command)
revertly log [session] [--outside] [--tripwires] [--tool Edit] [--path 'src/**']
revertly diff [session] [path…]      # unified diff, pre vs now (or version vs version)
revertly versions <path>             # the version chain of one file across sessions
revertly revert …                    # see §4
revertly ui                          # open the local control panel
revertly config                      # edit ~/.revertly/config.toml (scope, exclusions, tripwires, retention)
revertly pause / resume              # temporarily disarm without uninstalling
revertly gc [--keep 30d]             # prune sessions
revertly doctor                      # verify shim order, watcher health, snapshot ability
REVERTLY_DISABLE=1 claude            # per-invocation escape hatch, always honored
```

### 2.3 Control panel — `revertly ui`
A single local page on `localhost` (no accounts, no network, works offline).
Four views:

1. **Timeline** — sessions as rows; each expands into its checkpoint stream:
   tool calls interleaved with file events. Injection-relevant events (tripwires,
   out-of-project writes, sensitive reads) are visually flagged.
2. **Diff view** — any file, any two versions, side-by-side. "Restore this
   version" button on every version.
3. **Revert composer** — checkbox tree of a session's changes; select any subset;
   preview the combined diff; one button: *Revert selected*. Shows the plan
   ("will restore 12 files, delete 3 created files, warn on 1 conflict") before
   acting.
4. **Live view** — while a session runs: files touched streaming in real time,
   current tool call, running count of outside-project touches. The "watch the
   agent's hands" screen.

Design rule: **the UI can do nothing the CLI can't.** Every UI action shows the
equivalent CLI command it will run.

### 2.4 Configuration (one file, sane defaults)
`~/.revertly/config.toml`:
```toml
[watch]
scope = "$HOME"                 # what the journal covers
exclude = ["~/Library/Caches", "~/.revertly", "**/node_modules", "**/.git/objects"]

[tripwires]                     # default set ships enabled
paths = ["~/.ssh/**", "~/.aws/**", "~/.config/gh/**", "~/.zshrc", "~/.zprofile",
         "~/Library/LaunchAgents/**", "/etc/**", "**/id_rsa*", "**/.env"]
on_write = "alert"              # phase 1: alert | log     (phase 2 adds: block)
on_read  = "alert"              # via hook layer (tool-level reads)

[retention]
sessions = "30d"
max_disk = "10GB"               # oldest sessions pruned first
```

---

## 3. Versioning & journal (what "we can always go back" actually means)

**Version chain per file.** `v0` is captured by the session-start clone. Then a
`PostToolUse` hook fires after every Edit/Write/Bash, and any file the event
window shows as touched gets a CoW clone appended to its chain. Result: not just
"before vs after the session," but **every intermediate state the agent moved
through** — undo tool call #12 while keeping #13's work.

**Journal record** (one JSONL line per event):
```json
{"t":"2026-07-25T10:31:02Z","kind":"fs","op":"write","path":"src/app.ts","version":"v3","checkpoint":12}
{"t":"2026-07-25T10:31:02Z","kind":"tool","tool":"Edit","target":"src/app.ts","checkpoint":12}
{"t":"2026-07-25T10:33:40Z","kind":"tripwire","op":"read","path":"~/.ssh/id_ed25519","tool":"Read","action":"alerted"}
```
Two correlated streams: **fs** (ground truth — what changed on disk, from
FSEvents) and **tool** (intent — what the agent *said* it was doing, from hooks).
Divergence between them is itself a signal (see §5).

**Honest limits, in the doc not the fine print:** FSEvents doesn't report *reads*
and doesn't attribute PIDs. Read-tripwires therefore come from the hook layer
(they see Claude's Read/Grep/Bash tools — which covers the agent, not arbitrary
spawned binaries). Full read auditing is a Phase 2 item (Endpoint Security build,
enterprise-only, where the entitlement cost is justified).

---

## 4. Revert (the whole point — defined precisely)

**Principles:**
1. **Non-destructive.** Every revert first captures current state as a new
   "revert session." You can always revert a revert. There is no operation in
   revertly that loses data.
2. **Preview-first.** Every revert prints its plan (restore / delete / conflict
   counts) and requires confirm — `--yes` for scripts.
3. **Conflict-aware.** If a file changed *after* the session being reverted
   (you kept working, or another session touched it), revertly flags it, shows a
   3-way diff, and requires an explicit per-file decision. No silent clobbering.

**The grammar:**
```
revertly revert                          # whole most-recent session (with preview)
revertly revert <session>                # whole named session
revertly revert <session> <path…>        # just these files/dirs
revertly revert <session> --to cp:12     # rewind session to checkpoint 12
revertly revert --file src/app.ts --to v3   # one file to a specific version
revertly revert -i                       # interactive picker (TUI version of the Revert composer)
```

**Semantics per change type:** modified → restore pre-image · created → delete
(it's in the revert-session if you regret it) · deleted → restore from clone ·
renamed → rename back (journal recorded both ends).

---

## 5. The safety story: prompt injection & unsafe hallucinations

Be precise about the claim. revertly does **not** prevent an injected or
hallucinating agent from *attempting* anything. What it removes is the two
things that make those attacks actually hurt: **permanence** and **invisibility**.

**Threats, concretely:**
- *External prompt injection:* Claude reads a README / webpage / issue containing
  hidden instructions → tries to read `~/.ssh`, plant a LaunchAgent, edit
  `~/.zshrc`, or exfiltrate `.env`.
- *Unsafe hallucination:* Claude confidently runs `rm -rf` on the wrong path,
  "cleans up" files it invented a reason to delete, rewrites config it
  misunderstood.

**How each layer answers:**

1. **Blast radius → zero permanence.** Whatever a hijacked agent writes, deletes,
   or plants is in the journal with a pre-image. `revertly revert` erases the
   attack's *entire footprint* — including persistence mechanisms (rc files,
   LaunchAgents, cron) that a human would never think to check. Injection's real
   power is *persistence after the session ends*; revertly specifically kills that.

2. **Tripwires → the attack announces itself.** Injections target the same small
   set of paths (credentials, shell rc, launchd, `/etc`). Those are default
   tripwires. The moment the agent reads `~/.ssh/id_ed25519` or writes a
   LaunchAgent, the user gets a notification *while it's happening* — not a
   forensic surprise weeks later. Hallucinations trip the same wires: an agent
   that decides to "fix" your dotfiles gets flagged on the first write.

3. **Intent/effect divergence → hallucination detector.** Two independent
   streams: what the agent *claimed* (tool log) vs what *happened on disk*
   (fs log). A Bash call described as "run the tests" that unlinked 200 files
   shows up as a flagged divergence in the session summary. This catches the
   scariest class: actions the agent itself misrepresented or misunderstood.

4. **Out-of-scope highlighting → cheap anomaly signal.** 95% of legitimate
   sessions touch only the project dir. Every summary front-loads the
   exceptions: `2 files outside project ⚠`. Injection and hallucination damage
   is almost by definition *out of scope*, so the default report surfaces it.

5. **Forensics → you learn what happened.** Full timeline: which tool call, in
   response to which prompt context, touched what, in what order. After an
   incident you don't wonder — you read.

**Honest gap (and the roadmap answer):** data already *read and sent over the
network* cannot be reverted. Phase 1 mitigates (read-tripwires alert in
real time so you can kill the session; secrets under tripwire paths); Phase 2
rules can *block* tripwire reads pre-execution via the hook layer, and the
enterprise ES build extends read-audit beyond agent tools. We say this plainly
rather than implying revert == exfil protection.

---

## 6. User flows — every scenario, end to end

**S1 — Happy path (must stay invisible).**
`claude "add pagination"` → works as always → exit → one summary line, all
in-project. User does nothing. *Success = revertly cost 0 seconds and 0 thought.*

**S2 — "Wait, what is it doing?" (mid-session).**
Agent is churning. Second terminal: `revertly last` (or `revertly ui` → Live view) →
sees files streaming, all in `src/`. Reassured, or Ctrl-C's the agent. Either
way the session seals normally and stays revertible.

**S3 — Bad edit found later.**
Yesterday's session subtly broke a config. `revertly versions config/app.yaml` →
chain across sessions → `revertly diff --file config/app.yaml v0 v2` → pinpoint →
`revertly revert --file config/app.yaml --to v0` → conflict check (user hasn't
touched it since) → clean restore. Rest of the session's work untouched.

**S4 — Agent deleted the project.**
Hallucinated cleanup: `rm -rf ~/prj/app`. FSEvents journals the unlinks.
`revertly revert` → preview: "restore 1,204 deleted files from clone" → confirm →
project back in seconds (CoW clone was complete). No Time Machine spelunking.

**S5 — Persistence attempt (injection-style).**
Session writes `~/Library/LaunchAgents/com.helper.plist` + appends to
`~/.zshrc`. Both tripwires fire → notification mid-session. User:
`revertly log --tripwires` → sees both writes with diffs → `revertly revert <session>
~/Library/LaunchAgents ~/.zshrc` → footprint gone, legitimate project work kept.
Session flagged in history for later review.

**S6 — Credential read (exfil attempt).**
Injected instructions make Claude Read `~/.ssh/id_ed25519`. Read-tripwire
(hook layer) fires *before the damage compounds* → user kills the session →
`revertly log` confirms what was read → rotate that key. revertly turned "silent
exfil" into "immediate, specific incident response." (Phase 2: rule blocks the
read outright.)

**S7 — Catastrophe (filesystem gone).**
Worst case: agent nukes far beyond the project. Volume-level APFS snapshot from
session start exists. Boot to Recovery → restore local snapshot → machine as of
session start. `revertly doctor` verifies snapshot capability *up front* so this
backstop is known-good before it's ever needed.

**S8 — Regretting a revert.**
Reverted a whole session, realizes half was good. Reverts are sessions:
`revertly revert <revert-session> <paths…>` selectively un-reverts. Nothing was
ever lost (§4 principle 1).

**S9 — Concurrent sessions.**
Two terminals, two claudes. Each shim invocation = its own session, own clone,
own journal. FSEvents windows overlap; events in shared paths are attributed to
all overlapping sessions with a `shared:true` flag, and revert previews warn on
cross-session conflicts (§4 principle 3 handles the rest).

**S10 — Disk pressure.**
CoW keeps costs low, but long sessions on huge repos add up. `max_disk` cap:
oldest sessions pruned first, tripwire-flagged sessions pruned *last*, warning
at 80%. `revertly gc` for manual control. Never silently disarm — if revertly cannot
arm (no space, watcher failed), it says so and asks: proceed unprotected or
abort. (Configurable: `on_arm_failure = "ask" | "proceed" | "abort"`.)

**S11 — Pause / disable / uninstall.**
`revertly pause` (until `resume`) · `REVERTLY_DISABLE=1 claude` (one run) ·
uninstall = remove shim line; sessions dir remains readable, deletable with one
`rm -rf ~/.revertly`. Leaving must be as clean as arriving — that's what makes
trying it a no-risk decision.

---

## 7. Phase 2 — Fleet: telemetry, admin, rules

*Premise: the org's problem isn't one developer's bad session — it's having no
idea what a hundred agents are doing on a hundred laptops.*

**Telemetry (opt-in, metadata-only by default).**
Ships per-session: counts, scope stats, tripwire events, intent/effect
divergences, revert usage. **Never file contents, never diffs, never prompts**
unless an org explicitly enables content-level incident capture — and then the
UI shows the user exactly what leaves the machine (`revertly telemetry show`
displays the actual payloads). Trust here is the product; one silent overreach
kills it.

**Admin console.**
Fleet dashboard: sessions/day, tripwire heat-map (which paths, which repos),
divergence outliers, revert frequency (a team reverting constantly has an
agent-quality problem worth seeing). Incident view: every tripwire event with
its session timeline (metadata), one click to request the local journal from
the user (consent-based pull, not silent push).

**Rules engine (the AUTH mode, arriving only now — deliberately).**
Central policy bundles, signed, distributed to shims:
```toml
[[rule]]
match = { op = "read",  path = "~/.ssh/**" }        # via hook layer, pre-execution
action = "block"                                     # block | alert | log
[[rule]]
match = { op = "write", path = "~/Library/LaunchAgents/**" }
action = "block"
[[rule]]
match = { tool = "Bash", command = "curl * --data *" }
action = "alert"
```
Enforcement point: PreToolUse hooks (block before execution) + tripwire layer.
Blocking waited for Phase 2 on purpose: rules need the telemetry corpus to be
tuned on real data, and a v1 that falsely blocks developers dies in a week.
Local rules always alert-only by default; *blocking* rules come from a policy
an admin owns, so there's someone accountable for false positives.

**Phase 2 also unlocks:** the Endpoint Security build (real read-audit and
PID attribution — enterprise-only, where managed-device entitlements are
normal), and org-wide retention policies.

---

## 8. Success metrics

**Phase 1:** % of claude runs through the shim (activation) · time-to-recover
from a bad session (target: < 2 min, vs hours) · % of users who run a revert in
first month (proves trust in the net) · summary-line → `revertly last` clickthrough ·
uninstall rate (canary for "seamless" failing).
**Phase 2:** fleet coverage · tripwire true/false-positive ratio (rule-tuning
health) · mean time-to-detect for injection-pattern events · % incidents fully
reverted.

## 9. Non-goals (kept explicit so scope stays honest)

- Phase 1: no blocking, no network monitoring, no daemon, no accounts, no cloud.
- Ever: not a sandbox, not an EDR replacement, not a code-review tool, and never
  a judge of *what the agent should do* — only a perfect memory of *what it did*
  and the power to undo it.
