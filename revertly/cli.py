"""revertly command-line interface. Phase 1, local only.

Usage:
  claude …                      (via the shim — arms the net automatically)
  revertly status | last | log | find | diff | versions | revert | restore
        | rm | clear | ui | config | pause | resume | gc | verify | doctor
        | install | shim
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Optional

from . import paths
from .config import Config, load as load_config
from .journal import Journal
from .model import EventKind, FsOp


def _load_meta(session_id):
    p = paths.meta_path(session_id)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _resolve_session(arg: Optional[str]) -> Optional[str]:
    if arg:
        return arg
    return paths.latest_session_id()


def _confirm(prompt: str) -> bool:
    """y/N prompt that treats a closed/non-TTY stdin as 'no', not a crash."""
    try:
        return input(prompt).strip().lower() == "y"
    except EOFError:
        print("(no tty — aborting; use --yes to skip the prompt)")
        return False


def _pause_flag() -> str:
    return os.path.join(paths.revertly_home(), "paused")


def _shim_path_status():
    """Is the claude shim installed AND first in PATH? Returns
    (installed: bool, first: bool, resolves_to: str|None)."""
    shim = os.path.join(paths.bin_dir(), "claude")
    installed = os.path.exists(shim)
    which = shutil.which("claude")
    first = bool(which and installed
                 and os.path.realpath(which) == os.path.realpath(shim))
    return installed, first, which


# ─────────────────────────── commands ───────────────────────────

def cmd_status(args) -> int:
    paused = os.path.exists(_pause_flag())
    installed, first, which = _shim_path_status()
    if paused:
        state = "PAUSED — 'revertly resume' to re-arm"
    elif not installed:
        state = "NOT INSTALLED — run ./install.sh (claude runs unprotected)"
    elif not first:
        state = (f"⚠ BYPASSED — `claude` resolves to {which}, not the shim; "
                 f"runs are UNPROTECTED (open a new terminal, or fix PATH)")
    else:
        state = "armed (shim is first in PATH; arms on next claude run)"
    print(f"revertly: {state}")
    ids = paths.list_session_ids()
    size = paths.store_size()
    cfg = load_config(paths.config_path())
    cap = f" / {cfg.max_disk_gb:g} GB cap" if cfg.max_disk_gb else ""
    pct = ""
    if cfg.max_disk_gb:
        used = size / (cfg.max_disk_gb * 1e9) * 100
        pct = f"  ({used:.0f}% of cap)" if used >= 1 else ""
    print(f"sessions: {len(ids)}  disk: {_human(size)}{cap}{pct}  "
          f"store: {paths.revertly_home()}")
    if cfg.retention_days:
        print(f"retention: keeping ~{cfg.retention_days}d "
              f"(flagged sessions kept longer) — 'revertly clear' to prune now")
    for sid in ids[-5:][::-1]:
        m = _load_meta(sid) or {}
        flag = " [revert]" if m.get("is_revert") else ""
        ev = " ⚠flagged" if m.get("flagged") else ""
        armed = "armed" if m.get("armed") else "UNARMED"
        print(f"  {sid}{flag}{ev}  {m.get('name','?')}  "
              f"({armed}, {_human(paths.session_size(sid))})")
    return 0


def _human(n: float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def cmd_last(args) -> int:
    sid = paths.latest_session_id()
    if not sid:
        print("revertly: no sessions yet")
        return 0
    return _print_session(sid)


def _print_session(sid) -> int:
    m = _load_meta(sid) or {}
    events = Journal.read(paths.journal_path(sid))
    fs = [e for e in events if e.kind == EventKind.FS]
    trips = [e for e in events if e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER)]
    cwd = m.get("cwd", "")
    outside = [e for e in fs if e.path and not paths.is_under(e.path, cwd)]
    print(f"session {sid}  ({m.get('name','?')})")
    print(f"  cwd={cwd}  armed={m.get('armed')}  exit={m.get('exit_code')}")
    print(f"  {len(fs)} fs changes, {len(outside)} outside project, {len(trips)} tripwire(s)")
    ok, bad = Journal.verify(paths.journal_path(sid))
    print(f"  journal integrity: {'OK' if ok else f'TAMPERED at seq {bad}'}")
    if trips:
        print("  tripwires:")
        for e in trips[:20]:
            tag = "SELF-TAMPER" if e.kind == EventKind.SELF_TAMPER else "tripwire"
            print(f"    {tag}: {e.op.value if e.op else '?'} {e.path}  ({e.detail})")
    return 0


def cmd_log(args) -> int:
    sid = _resolve_session(args.session)
    if not sid:
        print("revertly: no sessions"); return 1
    events = Journal.read(paths.journal_path(sid))
    m = _load_meta(sid) or {}
    cwd = m.get("cwd", "")
    for e in events:
        if args.tripwires and e.kind not in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER):
            continue
        if args.outside and (not e.path or paths.is_under(e.path, cwd)):
            continue
        if args.tool and e.tool != args.tool:
            continue
        if args.path:
            from .search import path_matches
            if not path_matches(e.path, args.path):
                continue
        ts = time.strftime("%H:%M:%S", time.localtime(e.t))
        label = e.path or e.tool or ""
        if e.op == FsOp.RENAME and e.path_from:
            label = f"{e.path_from} → {e.path}"
        print(f"{ts} {e.kind.value:11} {e.op.value if e.op else '':7} {label}")
    return 0


def cmd_find(args) -> int:
    """Search EVERY session for a path — 'what happened to X, and when?'"""
    from .search import find_events
    since = None
    if args.since:
        spec = args.since.strip().lower()
        try:
            if spec.endswith("d"):
                since = time.time() - float(spec[:-1]) * 86400
            elif spec.endswith("h"):
                since = time.time() - float(spec[:-1]) * 3600
            else:
                since = time.time() - float(spec) * 86400
        except ValueError:
            print(f"revertly: bad --since {args.since!r} (use e.g. 7d or 12h)")
            return 2
    hits = find_events(args.pattern, op=args.op, since=since)
    if not hits:
        print(f"revertly find: no events match {args.pattern!r}")
        return 1
    last_sid = None
    for h in hits:
        if h["session_id"] != last_sid:
            last_sid = h["session_id"]
            name = f"  ({h['session_name']})" if h["session_name"] else ""
            print(f"{h['session_id']}{name}")
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h["t"] or 0))
        op = h["op"] or ""
        label = h["path"]
        if op == "rename" and h.get("path_from"):
            label = f"{h['path_from']} → {h['path']}"
        print(f"  {ts}  {h['kind']}/{op:7} {label}")
        if h["kind"] == "fs" and op in ("delete", "write", "rename"):
            import shlex
            if paths.is_under(h["path"], h["cwd"]):
                print(f"           ↳ recover: revertly revert "
                      f"{h['session_id']} {shlex.quote(h['path'])}")
            else:
                print("           ↳ outside the session's project dir — "
                      "no pre-image; not auto-revertible")
    print(f"\n{len(hits)} event(s) across "
          f"{len({h['session_id'] for h in hits})} session(s)")
    return 0




def cmd_rm(args) -> int:
    """Permanently delete specific sessions (their journals, clones, all of it).

    This is the ONE destructive command in revertly, so it is loud about it:
    it prints what each session is before asking, and tripwire-flagged
    sessions require an extra --force.
    """
    missing = [s for s in args.sessions if not os.path.isdir(paths.session_dir(s))]
    if missing:
        print(f"revertly rm: unknown session(s): {', '.join(missing)}")
        return 1
    # never delete a session that is still running (mirrors the UI's guard):
    # its watcher/journal are live, and the pre-image would vanish mid-run.
    import time as _t
    live = [s for s in args.sessions
            if (_load_meta(s) or {}).get("ended") is None
            and _t.time() - float((_load_meta(s) or {}).get("started") or 0) < 86400]
    if live and not args.force:
        print(f"revertly rm: refusing to delete session(s) that appear to be "
              f"RUNNING ({', '.join(live)}) — wait for them to finish, or --force.")
        return 1
    flagged = []
    total = 0
    for sid in args.sessions:
        m = _load_meta(sid) or {}
        trips = sum(1 for e in Journal.read(paths.journal_path(sid))
                    if e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER))
        size = paths.session_size(sid)
        total += size
        tag = f"  ⚠ {trips} tripwire event(s)" if trips else ""
        print(f"  {sid}  ({m.get('name','?')})  {size/1e6:.1f} MB{tag}")
        if trips:
            flagged.append(sid)
    if flagged and not args.force:
        print("revertly rm: refusing to delete tripwire-flagged session(s) "
              f"({', '.join(flagged)}) without --force — they may be evidence.")
        return 1
    if not args.yes:
        if not _confirm(f"PERMANENTLY delete {len(args.sessions)} session(s), "
                        f"{total/1e6:.1f} MB — no revert possible? [y/N] "):
            print("aborted."); return 1
    for sid in args.sessions:
        trips = sum(1 for e in Journal.read(paths.journal_path(sid))
                    if e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER))
        paths.append_incident(
            "RM", f"permanently deleted session {sid}"
                  f"{' (FLAGGED evidence)' if trips else ''}")
        paths.rmtree_force(paths.session_dir(sid))
        print(f"deleted {sid}")
    return 0


def cmd_diff(args) -> int:
    sid = _resolve_session(args.session)
    if not sid:
        print("revertly: no sessions"); return 1
    from .revert import Reverter
    r = Reverter(paths.session_dir(sid))
    plan = r.plan_paths(args.paths) if args.paths else r.plan()
    import difflib
    for ch in plan.restores + plan.deletes:
        pre = _read(ch.pre_blob) if ch.pre_blob else ""
        cur = _read(ch.path)
        d = difflib.unified_diff(pre.splitlines(True), cur.splitlines(True),
                                 fromfile=f"pre:{ch.path}", tofile=f"cur:{ch.path}")
        sys.stdout.writelines(d)
    print(f"\n{plan.summary()}")
    return 0


def _read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except (OSError, TypeError):
        return ""


def cmd_versions(args) -> int:
    # walk sessions newest->oldest, report which have a pre-image of this path
    # and what each session actually DID to it (from its journal).
    target = os.path.abspath(args.path)
    print(f"versions of {target}:")
    found = False
    for sid in paths.list_session_ids()[::-1]:
        m = _load_meta(sid) or {}
        cwd = m.get("cwd", "")
        if not paths.is_under(target, cwd):
            continue
        rel = os.path.relpath(target, cwd)
        blob = os.path.join(paths.clone_dir(sid), rel)
        if not os.path.exists(blob):
            continue
        found = True
        ops = sorted({e.op.value for e in Journal.read(paths.journal_path(sid))
                      if e.kind == EventKind.FS and e.op and e.path
                      and (e.path == target
                           or e.path.startswith(target + os.sep))})
        did = f"session {'/'.join(ops)}d it" if ops else "untouched in session"
        print(f"  {sid}  v0 (pre-image)  [{did}]")
        print(f"    view:    {blob}")
        print(f"    restore: revertly revert {sid} {target}")
    if not found:
        print("  (no session holds a pre-image of this path — it may be "
              "outside every session's project dir, or already GC'd)")
        return 1
    return 0


def _newest_session_with_preimage(target: str):
    """Newest session id holding a pre-image (clone blob) of `target`, or None."""
    for sid in paths.list_session_ids()[::-1]:   # newest first
        m = _load_meta(sid) or {}
        cwd = m.get("cwd", "")
        if not paths.is_under(target, cwd):
            continue
        blob = os.path.join(paths.clone_dir(sid), os.path.relpath(target, cwd))
        if os.path.exists(blob):
            return sid
    return None


def cmd_restore(args) -> int:
    """The 90% ask: 'give me this file back' — no session id needed. Finds the
    newest session holding a pre-image and reverts just this path."""
    target = os.path.abspath(args.path)
    sid = _newest_session_with_preimage(target)
    if not sid:
        print(f"revertly restore: no session holds a pre-image of {target} "
              f"(outside every project dir, or GC'd). Try 'revertly find'.")
        return 1
    from .revert import Reverter
    r = Reverter(paths.session_dir(sid))
    ok, reason = r.is_revertible()
    if not ok:
        print(f"revertly: cannot restore from {sid}: {reason}")
        return 1
    plan = r.plan_paths([target])
    if not plan.restores and not plan.deletes:
        print(f"{target} already matches its pre-image in {sid} — nothing to do.")
        return 0
    print(f"restore {target} from session {sid}: {plan.summary()}")
    for ch in plan.restores:
        print(f"  restore {ch.path}")
    for ch in plan.deletes:
        print(f"  delete  {ch.path}")
    for c in plan.conflicts:
        print(f"  CONFLICT {c.path}: {c.reason}")
    if args.dry_run:
        print("(dry-run: nothing changed)")
        return 0
    if not args.yes and not _confirm("proceed? [y/N] "):
        print("aborted."); return 1
    rid = r.apply(plan, force=args.force)
    if rid is None:
        print("nothing restored (conflicts skipped — re-run with --force).")
        return 1
    print(f"restored. undo with: revertly revert {rid}")
    return 0


def cmd_revert(args) -> int:
    sid = _resolve_session(args.session)
    if not sid:
        print("revertly: no sessions"); return 1
    from .revert import Reverter
    r = Reverter(paths.session_dir(sid))
    ok, reason = r.is_revertible()
    if not ok:
        print(f"revertly: refusing to revert {sid}: {reason}")
        return 1
    plan = r.plan_paths(args.paths) if args.paths else r.plan()
    print(f"revert plan for {sid}: {plan.summary()}")
    for ch in plan.restores:
        print(f"  restore {ch.path}")
    for ch in plan.deletes:
        print(f"  delete  {ch.path}")
    for c in plan.conflicts:
        print(f"  CONFLICT {c.path}: {c.reason}")
    if args.dry_run:
        print("(dry-run: nothing changed)")
        return 0
    if not plan.is_clean and not args.force:
        print("revertly: conflicts present; re-run with --force to override them "
              "(they are skipped otherwise).")
    if not args.yes and not _confirm("proceed? [y/N] "):
        print("aborted."); return 1
    rid = r.apply(plan, force=args.force)
    if rid is None:
        print("nothing to revert (empty plan or all conflicts skipped).")
        return 0
    for err in plan.errors:
        print(f"  ⚠ {err}")
    print(f"reverted{' (with errors above)' if plan.errors else ''}. "
          f"undo this revert with: revertly revert {rid}")
    return 1 if plan.errors else 0


def cmd_ui(args) -> int:
    from .ui.server import serve
    httpd, port = serve(port=args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"revertly control panel: {url}  (Ctrl-C to stop)")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        httpd.shutdown()
    return 0


def cmd_config(args) -> int:
    p = paths.config_path()
    print(f"config: {p}")
    if os.path.exists(p):
        with open(p) as f:
            print(f.read())
    else:
        print("(using defaults; no config file yet)")
    return 0


def cmd_pause(args) -> int:
    paths.ensure_store()
    open(_pause_flag(), "w").close()
    paths.append_incident("PAUSE", "revertly disarmed via 'revertly pause'")
    print("revertly: paused (claude runs unprotected until 'revertly resume')")
    return 0


def cmd_resume(args) -> int:
    try:
        os.remove(_pause_flag())
    except FileNotFoundError:
        pass
    print("revertly: resumed")
    return 0


def _parse_before(spec: str):
    """A cutoff for --before: a session id, or an age like 7d/12h, or an
    ISO-ish date (YYYY-MM-DD). Returns an epoch float, or None on parse error."""
    from . import retention
    s = spec.strip()
    # a known session id -> that session's start time
    if s in paths.list_session_ids():
        m = _load_meta(s) or {}
        return float(m.get("started") or 0.0)
    low = s.lower()
    try:
        if low.endswith("d"):
            return time.time() - float(low[:-1]) * 86400
        if low.endswith("h"):
            return time.time() - float(low[:-1]) * 3600
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except ValueError:
            continue
    return None


def cmd_gc(args) -> int:
    from . import retention
    cfg = load_config(paths.config_path())
    max_bytes = int(cfg.max_disk_gb * 1e9) if cfg.max_disk_gb else None
    # honor the config's retention_days unless --keep overrides it
    keep = args.keep if args.keep is not None else cfg.retention_days
    before = None
    if getattr(args, "before", None):
        before = _parse_before(args.before)
        if before is None:
            print(f"revertly gc: bad --before {args.before!r} "
                  f"(use a session id, 7d/12h, or YYYY-MM-DD)")
            return 2
    items = retention.plan(retention.collect(),
                           keep_days=keep, max_disk_bytes=max_bytes,
                           before=before)
    removed = retention.apply(items)
    freed = sum(i.size for i in items)
    print(f"revertly gc: removed {removed} session(s), freed {_human(freed)} "
          f"(kept ≤{keep}d"
          f"{f', ≤{cfg.max_disk_gb:g}GB' if max_bytes else ''}; "
          f"flagged/live kept)")
    return 0


def cmd_clear(args) -> int:
    """The 'I'm at a safe point — clear history' command. Clears sessions by a
    cutoff (--before) or ALL of them (--all), keeping the live session and
    (unless --include-flagged) tripwire-flagged evidence."""
    from . import retention
    before = None
    if args.before:
        before = _parse_before(args.before)
        if before is None:
            print(f"revertly clear: bad --before {args.before!r} "
                  f"(use a session id, 7d/12h, or YYYY-MM-DD)")
            return 2
    if not args.all and before is None and args.keep is None:
        print("revertly clear: specify --all, --before <id|7d|date>, or "
              "--keep <days>. Nothing cleared.")
        return 2
    items = retention.plan(
        retention.collect(),
        keep_days=args.keep,
        before=before,
        clear_all=args.all,
        include_flagged=args.include_flagged)
    if not items:
        print("revertly clear: nothing matches — store already clean.")
        return 0
    freed = sum(i.size for i in items)
    flagged = [i for i in items if i.flagged]
    print(f"clear plan: {len(items)} session(s), {_human(freed)} to free")
    for i in items[:10]:
        tag = " ⚠flagged" if i.flagged else ""
        print(f"  {i.id}{tag}  {_human(i.size)}  [{i.reason}]")
    if len(items) > 10:
        print(f"  … and {len(items) - 10} more")
    if flagged:
        print(f"  ⚠ {len(flagged)} FLAGGED (evidence) session(s) included")
    if args.dry_run:
        print("(dry-run: nothing changed)")
        return 0
    if not args.yes:
        prompt = (f"PERMANENTLY clear {len(items)} session(s) "
                  f"({_human(freed)})? This cannot be undone. [y/N] ")
        if not _confirm(prompt):
            print("aborted."); return 1
    removed = retention.apply(items)
    print(f"cleared {removed} session(s), freed {_human(freed)}.")
    return 0


def cmd_verify(args) -> int:
    """Audit journal integrity (hash chain) across sessions. This is the
    tamper-detection surface: if any session's journal was edited/truncated
    out of order, the chain breaks and we report exactly where."""
    sids = paths.list_session_ids() if (args.all or not args.session) else [args.session]
    if not sids:
        print("revertly: no sessions to verify"); return 0
    bad = 0
    for sid in sids:
        jp = paths.journal_path(sid)
        okc, reason = Journal.verify_sealed(jp, paths.seal_path(sid))
        sealed = "immutable" if paths.is_immutable(jp) else "mutable"
        if okc:
            print(f"  OK        {sid}  ({sealed})")
        else:
            bad += 1
            print(f"  TAMPERED  {sid}  {reason}  ({sealed})")
    print(f"\nverified {len(sids)} session(s): "
          f"{len(sids) - bad} intact, {bad} tampered")
    return 1 if bad else 0


def cmd_doctor(args) -> int:
    ok = True
    post_install = getattr(args, "install", False)
    # shim in PATH and ahead of the real claude?
    installed, first, which = _shim_path_status()
    shim = os.path.join(paths.bin_dir(), "claude")
    print(f"shim installed: {installed}  ({shim})")
    print(f"`claude` resolves to: {which}")
    if installed and not first:
        if post_install:
            # right after install the profile PATH isn't sourced yet — this is
            # expected, not a failure. Don't scare a fresh installer with WARN.
            print("  ℹ shim not yet first in PATH — open a NEW terminal (or "
                  "`source` your profile) to activate, then re-run doctor")
        else:
            print("  ⚠ shim is NOT first in PATH — runs will bypass revertly")
            ok = False
    # snapshot capability
    from .snapshot import TmutilSnapshotter
    can = TmutilSnapshotter().can_snapshot()
    print(f"APFS snapshots available (tmutil): {can}")
    ok = ok and can
    # watcher import
    try:
        from .watch import PollingWatcher  # noqa
        print("watcher: PollingWatcher OK")
    except Exception as e:
        print(f"watcher: FAIL ({e})"); ok = False
    print(f"store writable: {os.access(paths.revertly_home() or '.', os.W_OK) or 'will-create'}")

    # ── security section ──
    print("── security ──")
    cfg = load_config(paths.config_path())
    risky = cfg.risky_excludes()
    if risky:
        print(f"  ⚠ config exclude is dangerously broad: {risky} — watcher is blinded")
        ok = False
    else:
        print("  exclude scope: OK (not overly broad)")
    if cfg.tripwires_weakened():
        print("  ⚠ sensitive-path tripwires are EMPTY (SELF_TAMPER still active)")
        ok = False
    else:
        print(f"  tripwires: {len(cfg.tripwire_paths)} sensitive patterns armed")
    # journal integrity across all sessions
    sids = paths.list_session_ids()
    tampered = [s for s in sids if not Journal.verify(paths.journal_path(s))[0]]
    if tampered:
        print(f"  ⚠ TAMPERED journals in {len(tampered)} session(s): "
              f"{', '.join(tampered[:5])}  (run: revertly verify --all)")
        ok = False
    else:
        print(f"  journal integrity: all {len(sids)} session(s) intact")
    # incident history
    ilog = paths.incidents_log()
    if os.path.exists(ilog):
        try:
            n = sum(1 for _ in open(ilog))
        except OSError:
            n = "?"
        print(f"  incidents logged: {n}  ({ilog})")

    if post_install:
        print("doctor:", "installed ✅ (open a new terminal to activate)")
        return 0
    print("doctor:", "PASS ✅" if ok else "WARN ⚠")
    return 0 if ok else 1


def cmd_install(args) -> int:
    from . import shim
    launcher = shim.install_launcher()
    shim_path = shim.install_shim()
    print(f"installed launcher: {launcher}")
    print(f"installed shim:     {shim_path}  (wraps the real `claude`)")
    if args.no_profile:
        print(f"\nadd this to your shell profile to finish:\n"
              f'  export PATH="{paths.bin_dir()}:$PATH"')
    else:
        prof, changed = shim.add_path_to_profile()
        if changed:
            print(f"\nupdated PATH in {prof}")
            print("open a new terminal (or `source` it), then: revertly doctor")
        else:
            print(f"\nPATH already configured in {prof}")
    return 0


def cmd_uninstall(args) -> int:
    from . import shim
    if not args.yes:
        extra = " and DELETE the whole session store (~/.revertly)" if args.purge else ""
        resp = input(f"remove revertly shim + launcher{extra}? [y/N] ").strip().lower()
        if resp != "y":
            print("aborted."); return 1
    res = shim.uninstall(purge=args.purge)
    for p in res["removed"]:
        print(f"removed {p}")
    if res.get("profile"):
        prof, changed = res["profile"]
        print(f"PATH line {'removed from' if changed else 'not found in'} {prof}")
    if res["purged"]:
        print("purged session store (~/.revertly)")
    elif not args.purge:
        print("session store kept (use --purge to delete it, or: rm -rf ~/.revertly)")
    print("done. `claude` now runs normally again.")
    return 0


def cmd_shim(args) -> int:
    from .shim import run_wrapped
    return run_wrapped(args.cmd)


# ─────────────────────────── parser ───────────────────────────

_DESCRIPTION = """\
revertly — a seatbelt for AI coding agents (not a cage).

Run Claude Code (or any command) exactly as you always do. revertly sits
underneath it: it snapshots your project *before* the agent starts, records
every file the agent (or anything it runs) creates, changes, deletes, or
renames, and lets you undo any of it — one file, one session, or a whole
`rm -rf`. It also trips an alert the instant an agent touches something
sensitive (SSH keys, .env, shell config, LaunchAgents).

Everything is local — no accounts, no network, nothing leaves your machine.
Today: macOS (APFS) + Claude Code, out of the box.
"""

_EPILOG = """\
common workflows
  see what the agent just did
    revertly last                    summary of the most recent session
    revertly diff                    every change it made, as a unified diff
    revertly log --tripwires         just the sensitive-path hits
  undo something
    revertly restore <file>          put one file back (no session id needed)
    revertly revert                  undo the whole last session (asks first)
    revertly revert <id> <path>…     undo just some paths of a session
    revertly find <name>             which session touched a file, and when
  keep the store in check (pre-image clones pile up)
    revertly status                  disk usage + recent sessions
    revertly clear --before <id>     clear history before a safe point
    revertly gc                      apply the retention policy (age + cap)
  set up & check health
    revertly install                 add the `claude` shim to your PATH
    revertly doctor                  is the net armed and healthy?
    revertly ui                      open the visual control panel

escape hatches
    REVERTLY_DISABLE=1 claude …      run once with no net
    revertly pause / revertly resume disarm / rearm without uninstalling

Run `revertly <command> -h` for details on any command.
The full story and the honest security model are in README.md / THREAT-MODEL.md.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="revertly", description=_DESCRIPTION, epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", metavar="<command>",
                           title="commands")

    sub.add_parser("status", help="armed? disk usage, recent sessions"
                   ).set_defaults(func=cmd_status)
    sub.add_parser("last", help="summary of the most recent session"
                   ).set_defaults(func=cmd_last)

    lg = sub.add_parser("log", help="event-by-event log of a session")
    lg.add_argument("session", nargs="?")
    lg.add_argument("--outside", action="store_true",
                    help="only events outside the project dir")
    lg.add_argument("--tripwires", action="store_true",
                    help="only sensitive-path (tripwire) hits")
    lg.add_argument("--tool", help="filter to one tool, e.g. Edit")
    lg.add_argument("--path", help="filter by path (substring or glob)")
    lg.set_defaults(func=cmd_log)

    fd = sub.add_parser(
        "find", help="search ALL sessions for a path (substring or glob)",
        description="Answer 'what happened to this file, and when?' across every "
                    "session. Prints each matching event with a ready-to-run "
                    "recover command.",
        epilog="examples:\n"
               "  revertly find config.yaml\n"
               "  revertly find '*.env' --op delete\n"
               "  revertly find secrets --since 7d\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    fd.add_argument("pattern", help="path substring, or a glob like '*.env'")
    fd.add_argument("--op", help="filter by op: write|create|delete|rename")
    fd.add_argument("--since", help="only events newer than e.g. 7d or 12h")
    fd.set_defaults(func=cmd_find)

    df = sub.add_parser("diff", help="unified diff of a session: pre-image vs now")
    df.add_argument("session", nargs="?")
    df.add_argument("paths", nargs="*"); df.set_defaults(func=cmd_diff)

    vs = sub.add_parser("versions",
                        help="which sessions can restore this file, and what each did")
    vs.add_argument("path"); vs.set_defaults(func=cmd_versions)

    rv = sub.add_parser(
        "revert", help="undo a session (whole, or just some paths/globs)",
        description="Roll a session's changes back to their pre-image. With no "
                    "session id, the most recent session. Add paths (or globs) to "
                    "revert only those. Every revert is itself a session, so you "
                    "can always undo the undo.",
        epilog="examples:\n"
               "  revertly revert                       # undo the last session\n"
               "  revertly revert --dry-run             # just preview it\n"
               "  revertly revert <id> src/app.py       # only this file\n"
               "  revertly revert <id> '*.py'           # every .py it touched\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rv.add_argument("session", nargs="?", help="session id (default: most recent)")
    rv.add_argument("paths", nargs="*", help="limit to these paths/dirs/globs")
    rv.add_argument("--yes", "-y", action="store_true", help="skip the confirm")
    rv.add_argument("--dry-run", action="store_true", help="preview only")
    rv.add_argument("--force", action="store_true",
                    help="override conflicts (paths changed after the session)")
    rv.set_defaults(func=cmd_revert)

    rs = sub.add_parser(
        "restore", help="restore one file/dir from its newest pre-image",
        description="The quick 'give me this file back' command — finds the newest "
                    "session holding a pre-image of the path and reverts just that "
                    "path. No session id needed.",
        epilog="examples:\n"
               "  revertly restore config/app.yaml\n"
               "  revertly restore src/ --dry-run\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    rs.add_argument("path", help="the file or directory to bring back")
    rs.add_argument("--yes", "-y", action="store_true", help="skip the confirm")
    rs.add_argument("--dry-run", action="store_true", help="preview only")
    rs.add_argument("--force", action="store_true", help="override conflicts")
    rs.set_defaults(func=cmd_restore)

    rm = sub.add_parser("rm",
                        help="PERMANENTLY delete sessions from the store")
    rm.add_argument("sessions", nargs="+")
    rm.add_argument("--yes", "-y", action="store_true")
    rm.add_argument("--force", action="store_true",
                    help="allow deleting tripwire-flagged sessions")
    rm.set_defaults(func=cmd_rm)

    ui = sub.add_parser("ui", help="open the local visual control panel")
    ui.add_argument("--port", type=int, default=0)
    ui.set_defaults(func=cmd_ui)

    sub.add_parser("config", help="show the config file and its location"
                   ).set_defaults(func=cmd_config)
    sub.add_parser("pause", help="disarm revertly until 'resume' (logged)"
                   ).set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="re-arm after 'pause'"
                   ).set_defaults(func=cmd_resume)

    gc = sub.add_parser("gc", help="enforce retention policy (age + disk cap)")
    gc.add_argument("--keep", type=int, default=None,
                    help="keep sessions ≤ N days (default: config retention_days, 30)")
    gc.add_argument("--before", help="also prune before a session id / 7d / YYYY-MM-DD")
    gc.set_defaults(func=cmd_gc)

    cl = sub.add_parser(
        "clear", help="clear stored history at a safe point (frees disk)",
        description="revertly keeps a pre-image clone per session, so the store "
                    "grows. When you're at a safe point (committed / shipped), "
                    "clear the history you no longer need. The live session and "
                    "flagged (evidence) sessions are protected unless you opt in.",
        epilog="examples:\n"
               "  revertly clear --before <session-id>  # everything before that point\n"
               "  revertly clear --before 7d            # older than 7 days\n"
               "  revertly clear --all                  # wipe history, keep revertly\n"
               "  revertly clear --all --dry-run        # see what that would free\n",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    cl.add_argument("--all", action="store_true",
                    help="clear ALL sessions (keeps the live one)")
    cl.add_argument("--before", help="clear before a session id / 7d / YYYY-MM-DD")
    cl.add_argument("--keep", type=int, help="clear everything older than N days")
    cl.add_argument("--include-flagged", action="store_true",
                    help="also clear tripwire-flagged (evidence) sessions")
    cl.add_argument("--dry-run", action="store_true", help="preview only")
    cl.add_argument("--yes", "-y", action="store_true", help="skip the confirm")
    cl.set_defaults(func=cmd_clear)

    vf = sub.add_parser("verify",
                        help="audit journal hash chains for tampering")
    vf.add_argument("session", nargs="?")
    vf.add_argument("--all", action="store_true", help="verify every session")
    vf.set_defaults(func=cmd_verify)

    dr = sub.add_parser("doctor",
                        help="health check: shim in PATH, snapshots, security")
    dr.add_argument("--install", action="store_true",
                    help="post-install mode: don't WARN that PATH isn't sourced yet")
    dr.set_defaults(func=cmd_doctor)

    ins = sub.add_parser("install",
                        help="install the `claude` shim + launcher into PATH")
    ins.add_argument("--no-profile", action="store_true",
                     help="don't touch the shell profile; just print the PATH line")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall",
                        help="remove the shim + launcher (add --purge to wipe history)")
    un.add_argument("--purge", action="store_true",
                    help="also delete the session store (~/.revertly and all history)")
    un.add_argument("--yes", "-y", action="store_true")
    un.set_defaults(func=cmd_uninstall)

    # internal: the shim entrypoint the installed `claude` wrapper calls.
    # No help= -> argparse omits it from the command list (it's not user-facing).
    sh = sub.add_parser("shim")
    sh.add_argument("cmd", nargs=argparse.REMAINDER)
    sh.set_defaults(func=cmd_shim)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        if not argv:
            # bare `revertly`: a short, friendly orientation, not a wall of flags
            print(_DESCRIPTION)
            print("Quick start:")
            print("  revertly install         set up the `claude` shim (once)")
            print("  claude \"…\"                use Claude Code as usual — it's now recorded")
            print("  revertly last            see what it changed")
            print("  revertly restore <file>  put a file back  ·  revertly revert  undo it all")
            print("  revertly ui              visual control panel")
            print()
            print("All commands:  revertly --help      One command:  revertly <cmd> -h")
            return 0
        parser.print_help()
        return 0
    return args.func(args)
