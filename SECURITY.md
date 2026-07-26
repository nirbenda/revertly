# Security Policy

revertly is a **safety net for AI coding agents**, so we hold ourselves to a
high bar for honesty about what it does and does not protect. Please read the
scope below before reporting — it saves everyone time.

## Reporting a vulnerability

**Do not open a public issue for a security bug.** Instead:

- Use **GitHub → Security → "Report a vulnerability"** (private advisory), or
- email the maintainer (see `pyproject.toml` / the repo profile).

Please include a description, affected version/commit, and a reproduction. We
aim to acknowledge within a few days. There is no bug-bounty program.

## Supported versions

revertly is pre-1.0 (Phase 1). Only the **latest release / `main`** is
supported for security fixes.

## Scope — what IS a vulnerability

- A revert, `clear`, `gc`, or `rm` that **destroys data it shouldn't** (e.g.
  deleting files outside its plan, clobbering a conflict without flagging it).
- The local UI server serving store contents or accepting mutating actions
  **without the loopback-Host + token guards**, or any path-traversal /
  arbitrary-file read from its endpoints.
- The command guard or hook **crashing or blocking the agent** in a way that
  isn't fail-open, or leaking secrets into a world-readable location.
- Store files (clones, journals, incident log) created **world-readable** such
  that another local user can read project secrets.
- A journal that **passes `revertly verify` after being tampered with** in a
  way `verify` claims to detect (edit, reorder, truncation).

## Out of scope — known & documented limits (NOT vulnerabilities)

revertly's Phase-1 threat model is **tamper-EVIDENT and tamper-RAISING, not
tamper-PROOF** (see [`THREAT-MODEL.md`](THREAT-MODEL.md)). The following are
understood, documented limits — please do not report them as vulnerabilities:

- A **same-UID attacker** (or a hijacked agent running as you) can `chflags
  nouchg` and delete/rewrite the store, or bypass the shim
  (`REVERTLY_DISABLE=1`, absolute-path invocation). revertly makes this *loud
  and logged*, not *impossible*.
- The **command guard and hook are userspace** and evadable by calling a binary
  by absolute path or doing the read/network in-process (e.g. Python `open()`).
  They are alert/raise layers, not a boundary. The real boundary (kernel
  Endpoint Security / `sandbox-exec`) is roadmap.
- **Reads by a spawned binary/subprocess** and **network egress** are not seen
  in Phase 1 (the watcher polls the filesystem; the hook sees the agent's own
  tool calls).
- The APFS snapshot is a **manual Recovery restore**, not wired into
  `revertly revert`, and is skippable (`REVERTLY_NO_SNAPSHOT`).

If you're unsure whether something is in scope, report it privately and we'll
sort it out.
