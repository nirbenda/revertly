# revertly — Self-Defense Threat Model

*How does a malicious prompt disable revertly, and how do we stop it?*

This document is about attacks on **revertly itself**, not on the project files.
An injected or hallucinating agent that wants its damage to be permanent has
one prerequisite: **neutralize the safety net first.** If it can, everything
in PRODUCT.md is theater. So this is the load-bearing document.

---

## 0. The one fact everything follows from

Claude runs as *you*. revertly runs as *you*. **Same UID = same power.**

On a POSIX system, two processes with the same effective UID can signal, trace,
read, and overwrite each other's files at will. There is no `kill` the agent
can't send, no file it can't `rm`, no config it can't edit — because revertly's
own files and processes belong to the same user the agent is running as.

**Therefore: a purely same-privilege, in-band monitor is fundamentally
defeatable by a determined adversary.** Any doc that claims otherwise is lying.
What we *can* do is:

1. Make the thing that actually matters — **recoverability** — survive even
   total destruction of revertly, by pre-positioning it behind a privilege boundary
   the agent doesn't have.
2. Make tampering **loud and evident** rather than silent, so "disable revertly"
   is itself the most alarming event in the journal.
3. In the managed/enterprise tier, **cross the privilege boundary for real** so
   the monitor outranks the agent and the fight is no longer fair.

The rest of this doc is those three moves, made concrete.

---

## 1. Attack catalogue (same-UID, what an injected prompt would actually run)

| # | Attack | Command an injection would emit |
|---|---|---|
| A1 | Kill the watcher | `pkill -f fswatch` / `kill <pid>` |
| A2 | Wipe all evidence + pre-images | `rm -rf ~/.revertly` |
| A3 | Truncate/rewrite the journal | `> journal.jsonl` / edit records |
| A4 | Neuter config | add `**` to `exclude`, delete tripwires |
| A5 | Remove the shim | `rm ~/.revertly/bin/claude`; edit `~/.zshrc` PATH |
| A6 | Bypass the shim | call the real binary by absolute path |
| A7 | Disable via env | `REVERTLY_DISABLE=1 claude …` for a nested run |
| A8 | Destroy the backstop | `tmutil deletelocalsnapshots …` |
| A9 | Starve the backstop | fill the disk → macOS auto-purges local snapshots |
| A10 | Race the watcher | do damage in the FSEvents latency window, then A2 |
| A11 | Tamper the hooks | rewrite injected `--settings` so intent log lies |
| A12 | Replace revertly binary | overwrite `revertly`/watcher with a no-op |

Every one of these is trivially within a same-UID agent's reach. So we don't
pretend to *prevent* them all in Phase 1 — we make the ones that destroy
*recoverability* fail, and make the rest *evident*.

---

## 2. Defense principle #1 — Pre-position recoverability behind a boundary

**The decisive design choice: order of operations.** The volume snapshot and
the CoW clone are taken *before Claude's process is ever exec'd*. So even if the
first token the injected agent emits is `rm -rf ~/.revertly && tmutil
deletelocalsnapshots`, the pre-session state already exists.

Now make that pre-positioned state outrank the agent:

- **APFS snapshot deletion is privileged.** `tmutil deletelocalsnapshots`
  requires root. A same-UID agent *cannot* delete the session-start snapshot
  without a separate privilege escalation. This is the single most important
  robustness property revertly has: **the disaster backstop is already outside the
  agent's reach, using an OS primitive, for free.** (A8 defended.)
- **The clone/journal are mirrored to a root-owned store** (see §4). Even
  `rm -rf ~/.revertly` (A2) only destroys the *user-writable* copy; the mirror
  survives because the agent's UID can't write it.
- **A9 (disk-starvation auto-purge) is defended by monitoring, not prevention:**
  revertly refuses to arm below a free-space floor and alerts loudly if free space
  crosses the auto-thinning threshold mid-session — turning a silent backstop
  loss into a visible "your safety net is at risk" event.

Result: the worst a same-UID attacker achieves against recoverability is
"no *journal* of what I did" — never "my damage is permanent." **Permanence,
the only thing that truly matters, is defended even against total revertly erasure.**

---

## 3. Defense principle #2 — Make tampering loud and tamper-*evident*

We can't stop a same-UID `rm`, but we can guarantee you *know* it happened and
can't be fooled about it.

- **revertly's own paths are the loudest tripwires.** Any write, unlink, or signal
  targeting `~/.revertly/**`, the shim, the watcher PID, `~/.zshrc` PATH lines, or
  the hooks settings fires a `SELF_TAMPER` alert — the highest-severity event,
  surfaced as a notification *the instant it's attempted*. "Disable revertly" thus
  becomes the most conspicuous thing an agent can do, not a quiet first step.
- **The journal is a hash chain.** Each record carries `H(prev_record)`.
  Truncation (A3), deletion, or reordering breaks the chain → detected on next
  read. You cannot silently rewrite history; you can only *visibly* destroy it,
  which itself is a `SELF_TAMPER` event already mirrored off-UID.
- **Continuous flush + mirror.** Events are fsync'd and mirrored on write, not
  buffered until session end. The *attempt* to delete revertly is journaled before
  the delete completes, and the record is already in the root-owned mirror.
- **Heartbeat.** The journal carries a periodic heartbeat; a gap = watcher was
  killed (A1). A gap is itself a logged, alertable anomaly — silence is evidence.
- **Watchdog/respawn.** A small supervisor re-spawns a killed watcher and
  records the kill+respawn. Doesn't *prevent* A1 (attacker can loop the kill),
  but converts it into a visible, repeated `SELF_TAMPER` storm.

Net: Phase 1's honest promise isn't "you can't disable me." It's **"you cannot
disable me *quietly*, and you cannot make me forget what you did before you
tried."**

---

## 4. Defense principle #3 — Cross the privilege boundary (the real fix)

Everything above raises the bar within one UID. Real robustness needs the
monitor to outrank the agent. This is a **ladder**, deployed by tier:

### Tier 1 (Phase 1 default): user-only
Best-effort, honest ceiling as described in §2–§3. Appropriate threat model:
**mistakes and unsophisticated injections.** Recoverability survives (snapshot
is root-gated); evidence is tamper-evident but user-destroyable. State this
plainly to users — do not oversell.

### Tier 2 (Phase 1.5): root-owned collector daemon  ← the pragmatic upgrade
Install once with `sudo`: a **LaunchDaemon running as root** that owns the
journal/clone mirror and receives events over a local socket.

- Journal + mirror dir are **root-owned, 0700, agent UID has no write.**
  A2/A3/A4/A12 against the mirror now *fail* (permission denied) — and the
  denied attempt is itself logged by the daemon.
- The daemon `chflags schg` (system-immutable) the sealed journal — cannot be
  cleared without root + booting outside normal mode.
- Config that governs tripwires/exclusions lives in the root-owned store; the
  user's `~/.revertly/config.toml` can only *narrow* protection with an audit
  record, never silently widen exclusions.

This is modest — one daemon, one sudo at install — and it moves A2/A3/A4/A8/A12
from "trivial" to "requires a privilege escalation the agent doesn't have."

### Tier 3 (Phase 2, enterprise): Endpoint Security system extension
Now the collector is a **signed ES system extension**, and the platform itself
defends it:

- Runs as root, **protected by SIP** — even root can't tamper without disabling
  system protections.
- **Cannot be killed by a user process.** Better: an ES client can subscribe to
  `ES_EVENT_TYPE_AUTH_SIGNAL` and **deny signals sent to itself** — revertly can
  literally refuse the `kill` (A1 defended at the kernel).
- Sees `exec`/`open`/`unlink`/`rename` **below the process layer with real PID
  attribution**, so shim-bypass (A6) and nested/env tricks (A7) are visible:
  any `claude` binary exec, however launched, is caught — the shim stops being
  the enforcement point and becomes merely a convenience.
- `REVERTLY_DISABLE` (A7) is **ignored under managed policy**; only an admin-signed
  policy can disable protection, and that disablement is itself telemetered.
- Pre-execution AUTH means the self-tamper tripwires become **blocks**, not just
  alerts: the write to `~/.revertly` or the `kill` is denied before it happens.

This is why the ES build lives in the enterprise tier: the entitlement and
managed-device posture are only justified where tamper-*resistance* (not just
tamper-*evidence*) is a hard requirement. It converges on the same tool EDR uses
— but now for the correct reason: protecting the monitor, not merely watching.

### Tier 4 (Phase 2, org): recoverability + evidence leave the machine
Telemetry and (opt-in) journal mirroring go **off-box** to a store the agent's
machine has no write path to. Even a fully root-compromised laptop cannot alter
the org's record of what happened. The agent can't reach across the boundary it
isn't on.

---

## 5. Shim-bypass & env tricks, specifically (A5/A6/A7)

These deserve a direct answer because they're the "just don't use the wrapper"
class, and they expose the limit of a shim.

- **Phase 1 truth:** a shim is a *convenience and a default*, not a security
  control. An agent that calls the real binary by absolute path, or edits PATH,
  is simply not protected for that run — and revertly should be honest that the
  shim's job is to make the safe path the *easy default*, not an inescapable one.
- **Detection without a boundary:** the FSEvents watcher (if already armed by a
  prior/parallel session) will still journal the damage even from a bypassing
  claude — you lose intent correlation, not the fs record. And a bypassing
  `claude` exec that writes to tripwire paths still trips wires if any watcher
  is live.
- **Real fix is Tier 3:** ES catches *every* `claude` exec by PID regardless of
  how it was launched, so A6/A7 collapse. There is no reliable same-UID way to
  guarantee "all claude launches are wrapped"; you need the kernel to see execs.

Design consequence: **never let the shim be load-bearing for a security claim.**
It's the ergonomic front door. The security guarantees come from (a) pre-
positioned root-gated recoverability and (b), for resistance, the ES layer.

---

## 6. Race window (A10) — bound it, don't deny it

FSEvents is asynchronous (sub-second, but nonzero) and reads aren't covered.
An attacker can damage-then-erase inside the latency window.

- **Recoverability is race-immune:** the clone+snapshot predate the session
  entirely, so no in-session race affects the ability to *revert*. The race only
  affects the *journal's completeness*, not recovery.
- **Evidence:** hash-chain + off-UID mirror mean anything that *did* land in the
  journal can't be retroactively scrubbed; the erase attempt (A2) is itself an
  event.
- **Elimination:** Tier 3 ES is synchronous and pre-execution — no window.

So the race degrades *forensic completeness* under Tier 1, never *recovery*, and
vanishes at Tier 3.

---

## 7. Honest promise, per tier (put this in the README verbatim)

| Tier | Prevents disable? | Survives disable? | Threat it's honest against |
|---|---|---|---|
| 1 — user-only | No | **Recovery yes** (root-gated snapshot); evidence tamper-*evident* | Mistakes, naive injection |
| 2 — root daemon | Mirror/journal yes; process no | Yes | Injection that runs shell |
| 3 — ES extension | **Yes** (kernel + SIP + AUTH_SIGNAL) | Yes | Determined same-UID adversary |
| 4 — off-box | Yes + record is unreachable | Yes | Compromised host |

**The line we will not cross:** we never claim Tier 1 stops a determined
attacker. We claim Tier 1 guarantees *recoverability* and *tamper-evidence*, and
that stopping a determined attacker requires Tier 3+. Selling Tier 1 as
tamper-*proof* would be the one thing that actually makes users less safe
(false confidence). Honesty about the ceiling is a feature.

---

## 8. What changes in DESIGN.md / PRODUCT.md as a result

1. **Order of operations is now a hard invariant:** snapshot + clone + mirror
   MUST complete before `claude` is exec'd. Add a fail-closed check
   (`on_arm_failure` default `ask`, managed default `abort`).
2. **`SELF_TAMPER` becomes the top tripwire class**, always-on, not user-
   removable (in Tier 2+ it's root-owned).
3. **Journal gains a hash chain + heartbeat.** (Cheap, big evidentiary payoff.)
4. **Ship the root-owned collector (Tier 2) as the recommended install**, not
   just user-only — one sudo at setup buys most of the resistance.
5. **Reposition the shim in docs** as ergonomic default, explicitly *not* a
   security boundary. Security claims attach to the root/ES layers.
6. **Blocking self-tamper** is the *first* rule the Phase-2 engine ships, and the
   ES `AUTH_SIGNAL` self-protection is the enterprise headline.

---

## 9. Implementation status (Phase 1, Tier 1) — what is actually shipped

Grounding the theory above in the code that exists today:

| Defense | Status | Where |
|---|---|---|
| Arm-before-exec ordering (§2) | ✅ done | `session.arm()`, enforced by test |
| Fail-closed on arm failure | ✅ done | `session._handle_arm_failure`, `shim` |
| Pre-positioned recoverability (snapshot+clone before exec) | ✅ done | `session.arm()` |
| Root-gated snapshot backstop (deletion needs root) | ✅ (OS) | `tmutil` — not deletable by same-UID agent |
| `SELF_TAMPER` top tripwire class, not user-removable | ✅ done | `config.SELF_TAMPER_GLOBS`, always in `tripwire_globs_all()` |
| Hash-chained, fsync'd journal + heartbeat | ✅ done | `journal.py` |
| Tamper **detection** (`verify`) incl. garbled lines | ✅ done | `Journal.verify`, `revertly verify --all` |
| Tamper-**raising** immutability on sealed evidence (`uchg`) | ✅ done | `paths.make_immutable`, `session.seal()` |
| Real-time incident announcement (log + macOS notification) | ✅ done | `session._notify`, `incidents.log` |
| Config-weakening detection (broad excludes / emptied tripwires) | ✅ done | `config.risky_excludes`, `doctor` |
| Non-destructive, conflict-aware revert | ✅ done | `revert.py` |
| **Off-UID mirror** of journal | ⛔ Tier 2 | needs root daemon |
| **Root-owned collector** (write-denied to agent) | ⛔ Tier 2 | LaunchDaemon, one sudo |
| **ES extension** (deny `kill`, PID attribution, block bypass) | ⛔ Tier 3 | enterprise |
| **Off-box record** | ⛔ Phase 2 | fleet |

Honest reading: **Tier 1 is fully implemented.** A determined same-UID attacker
can still `chflags nouchg` and then delete — but not *silently*: it's an extra
step, the hash chain catches content edits, the deletion of an immutable file is
conspicuous, and recoverability (the root-gated snapshot) is already out of
reach. The jump from tamper-*evidence* to tamper-*resistance* is exactly the
Tier-2 root daemon, which is the next security milestone.
