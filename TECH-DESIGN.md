# revertly — Phase 1 Technical Design

Scope: **local** solution only. Observe → inspect → revert, fully offline, no
telemetry, no admin, no rules engine. Implements DESIGN.md + the Phase-1 parts
of PRODUCT.md, with the Tier-1/Tier-2 self-defense of THREAT-MODEL.md.

## Stack

- **Language:** Python 3 (stdlib only). Ships on macOS; zero install to try.
- **Tests:** `unittest` (stdlib). TDD: tests first, red → green → refactor.
- **UI:** `http.server` + a single embedded HTML/JS page. No build step.
- **External commands:** `tmutil` (snapshots), `cp -Rc` (APFS clonefile).
  Each wrapped behind an interface with a Fake for hermetic tests.

Hard rule: **no third-party dependencies.** If stdlib can't do it, we abstract
it behind an interface and ship a Fake + a best-effort real backend.

## Package layout (each file = one owner, disjoint for parallel work)

```
revertly/
  model.py       FROZEN contract: Event, SessionMeta, Version, Change,
                 RevertPlan, Conflict, TripwireHit, enums. (owner: CTO)
  config.py      Config dataclass, defaults, glob matching, tripwire set,
                 SELF_TAMPER paths. (owner: CTO)
  paths.py       Store layout: ~/.revertly roots, session dir naming, id gen. (CTO)
  journal.py     Append-only JSONL, hash chain, heartbeat, read+verify. (Worker A)
  snapshot.py    Snapshotter interface, TmutilSnapshotter, FakeSnapshotter. (Worker B)
  clone.py       Cloner interface, ClonefileCloner, FakeCloner. (Worker B)
  watch.py       Watcher interface, PollingWatcher, FakeWatcher. (Worker C)
  tripwire.py    Matching engine over config; classifies FS ops into hits. (Worker C)
  revert.py      plan()/apply(): pre-image vs current, conflicts, non-destructive. (Worker D)
  session.py     Lifecycle: arm()/seal(); wires snapshot+clone+journal+watch. (CTO integ)
  cli.py         argparse dispatch for all subcommands. (CTO integ)
  shim.py        `claude` shim entrypoint: arm, exec real claude, seal. (CTO)
  ui/server.py   Local control-panel HTTP server (read + revert actions). (Worker E)
  ui/index.html  Single-page UI: timeline / diff / revert composer / live. (Worker E)
  __main__.py    `python3 -m revertly` → cli.main(). (CTO)
tests/
  test_model.py, test_config.py, test_journal.py, test_snapshot.py,
  test_clone.py, test_watch.py, test_tripwire.py, test_search.py,
  test_revert.py, test_retention.py, test_session.py, test_cli.py,
  test_ui.py, test_security.py, test_e2e.py
verify.sh        The verification loop (see below).
```

## Store layout on disk (all plain files, `cat`-able)

```
~/.revertly/
  config.toml                     # user config (Tier-1)
  bin/claude                      # the shim
  incidents.log                   # cross-session tripwire / destructive-action log
  sessions/<id>/
    meta.json                     # SessionMeta (immutable once sealed)
    journal.jsonl                 # hash-chained Event stream (immutable once sealed)
    journal.seal                  # immutable {final seq, hash} — truncation anchor
    clone/                        # CoW pre-image of cwd (v0 of every file)
    versions/<pathhash>/vN        # (Phase 2) per-file version blobs v1..vn — not written yet
  mirror/                         # (Tier-2) root-owned journal mirror — Phase 1 stubs the dir
```

## Contracts between modules (the interfaces workers code to)

```python
# snapshot.py
class Snapshotter(Protocol):
    def create(self) -> Optional[str]: ...        # returns snapshot name or None
    def delete(self, name: str) -> None: ...       # privileged; may raise
    def can_snapshot(self) -> bool: ...

# clone.py
class Cloner(Protocol):
    def clone_tree(self, src: str, dst: str) -> None: ...   # CoW copy dir
    def clone_file(self, src: str, dst: str) -> None: ...
    def is_cow(self) -> bool: ...

# watch.py
class Watcher(Protocol):
    def start(self, root: str, on_event: Callable[[Event], None]) -> None: ...
    def stop(self) -> None: ...
    # PollingWatcher diffs tree snapshots; emits FS create/write/delete Events.

# journal.py
class Journal:
    def __init__(self, path: str): ...
    def append(self, e: Event) -> Event: ...       # fills seq/prev_hash/hash, fsync
    def heartbeat(self) -> None: ...
    @staticmethod
    def read(path) -> list[Event]: ...
    @staticmethod
    def verify(path) -> tuple[bool, Optional[int]]: ...  # (ok, first_bad_seq)

# tripwire.py
class TripwireEngine:
    def __init__(self, cfg: Config): ...
    def check(self, path: str, op: FsOp) -> Optional[TripwireHit]: ...

# revert.py
class Reverter:
    def plan(self, session_dir: str) -> RevertPlan: ...
    def plan_paths(self, session_dir: str, paths: list[str]) -> RevertPlan: ...
    def apply(self, plan: RevertPlan, *, dry_run: bool=False) -> None: ...
    # apply() is NON-DESTRUCTIVE: it captures current state (a revert-session)
    # before mutating, so any revert can itself be reverted.
```

## Key invariants (enforced by tests)

1. **Arm-before-exec:** `session.arm()` completes snapshot+clone+journal-open
   and starts the watcher *before* the wrapped command is exec'd. Test asserts
   ordering via a recording Fake.
2. **Fail-closed:** if arming fails, honor `on_arm_failure` (ask|proceed|abort);
   default `abort` under a managed flag, `ask` otherwise. Never silently run
   unprotected.
3. **Non-destructive revert:** `apply()` always writes a pre-revert capture
   first. Test: revert, then revert-the-revert, original restored.
4. **Conflict safety:** a path modified after session end is a Conflict, never
   silently overwritten. Test asserts it lands in `plan.conflicts`.
5. **Hash chain:** tampering with any journal line makes `verify()` return
   `(False, seq)`. Test truncates/edits and asserts detection.
6. **SELF_TAMPER:** any write/delete under `~/.revertly`, the shim, or shell rc
   files classifies as a `SELF_TAMPER` tripwire at `ALERT`. Test asserts it.

## Verification loop (`verify.sh`, run after every change)

```
1. python3 -m py_compile revertly/**/*.py        # syntax gate
2. python3 -m unittest discover -s tests -v    # full suite
3. tests/test_e2e.py                            # end-to-end smoke:
     arm a fake session in a temp dir → mutate files →
     seal → assert journal + versions → revert → assert restored →
     revert the revert → assert re-applied
4. print PASS/FAIL summary; nonzero exit on any failure
```

Each worker runs `python3 -m unittest tests.test_<their_module>` in a tight
red→green loop until their module is green, then the CTO runs the full
`verify.sh` for integration.

## Explicit Phase-1 exclusions (do NOT build)

Telemetry, admin console, rules/AUTH blocking, off-box mirror, Endpoint
Security extension, real fswatch/FSEvents backend (polling is the Phase-1
watcher), network monitoring, packaging/signing. Interfaces leave room for all
of them; implementations are out of scope now.
