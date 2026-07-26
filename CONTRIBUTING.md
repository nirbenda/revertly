# Contributing to revertly

Thanks for helping. revertly is a small, dependency-free tool with a strict
quality bar because it is a **safety net** — a bug here can lose a user's data.

## Ground rules

- **Pure Python 3.9+ standard library. Zero third-party dependencies.** This is
  a hard constraint: it must run on a stock Mac with nothing to install. If you
  reach for a dependency, that's a design discussion first.
- **macOS / APFS** is the Phase-1 target. Keep platform-specific bits (clone,
  snapshot, `chflags`) behind the existing abstractions so the core stays
  portable for later.
- **Fail-safe / fail-open.** The shim, guard, and hook must never break the
  user's agent: if revertly errors, the wrapped command still runs. Deletion
  paths must be conflict-aware and non-destructive by default.
- **Honesty in docs.** Don't claim a capability the code doesn't have. If a
  feature is partial or roadmap, say so (see the status notes in `PRODUCT.md`).

## Dev setup

```sh
git clone <your-fork> revertly && cd revertly
./verify.sh        # py_compile gate + full unittest suite + e2e smoke
```

No install step, no virtualenv needed — it's stdlib only. Run the CLI straight
from the tree with `PYTHONPATH=. python3 -m revertly …`.

## Before you open a PR

- **Add tests.** revertly is TDD throughout (currently 228 tests). New behavior
  needs coverage, especially anything in the revert / retention / guard paths.
- **`./verify.sh` must pass** (CI runs it on macOS across Python 3.9/3.11/3.12).
- Match the surrounding style: small modules, clear comments that state
  *constraints* (not restate the code), and the existing error-return
  conventions.
- For UI changes (`revertly/ui/index.html`), keep it **self-contained** (no CDN,
  no third-party JS/CSS) and verify it renders (a headless-browser screenshot is
  the norm here).

## Good first areas

- Additional agent CLIs in `revertly/agents.py` (detection is just a name).
- More `guard`/`hooks` detection patterns (with tests + a low false-positive
  bar).
- Docs / examples.

## Security

Please report vulnerabilities privately — see [`SECURITY.md`](SECURITY.md), not
a public issue.
