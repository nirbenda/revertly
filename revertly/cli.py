"""revertly command-line interface. Phase 1, local only.

Usage:
  claude …                      (via the shim — arms the net automatically)
  revertly status | last | log | diff | versions | revert | ui | config
        | pause | resume | gc | doctor | install | shim
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
from .model import EventKind


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


def _pause_flag() -> str:
    return os.path.join(paths.revertly_home(), "paused")


# ─────────────────────────── commands ───────────────────────────

def cmd_status(args) -> int:
    paused = os.path.exists(_pause_flag())
    print(f"revertly: {'PAUSED' if paused else 'armed (arms on next claude run)'}")
    ids = paths.list_session_ids()
    print(f"sessions: {len(ids)}  store: {paths.revertly_home()}")
    for sid in ids[-5:][::-1]:
        m = _load_meta(sid) or {}
        flag = " [revert]" if m.get("is_revert") else ""
        armed = "armed" if m.get("armed") else "UNARMED"
        print(f"  {sid}{flag}  {m.get('name','?')}  ({armed})")
    return 0


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
    outside = [e for e in fs if e.path and not e.path.startswith(cwd)]
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
        if args.outside and (not e.path or e.path.startswith(cwd)):
            continue
        if args.tool and e.tool != args.tool:
            continue
        ts = time.strftime("%H:%M:%S", time.localtime(e.t))
        print(f"{ts} {e.kind.value:11} {e.op.value if e.op else '':7} {e.path or e.tool or ''}")
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
    target = os.path.abspath(args.path)
    print(f"versions of {target}:")
    for sid in paths.list_session_ids()[::-1]:
        m = _load_meta(sid) or {}
        cwd = m.get("cwd", "")
        if not target.startswith(cwd):
            continue
        rel = os.path.relpath(target, cwd)
        blob = os.path.join(paths.clone_dir(sid), rel)
        if os.path.exists(blob):
            print(f"  {sid}  v0 (pre-image)  {blob}")
    return 0


def cmd_revert(args) -> int:
    sid = _resolve_session(args.session)
    if not sid:
        print("revertly: no sessions"); return 1
    from .revert import Reverter
    r = Reverter(paths.session_dir(sid))
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
    if not args.yes:
        resp = input("proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("aborted."); return 1
    rid = r.apply(plan, force=args.force)
    print(f"reverted. undo this revert with: revertly revert {rid}")
    return 0


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
    print("revertly: paused (claude runs unprotected until 'revertly resume')")
    return 0


def cmd_resume(args) -> int:
    try:
        os.remove(_pause_flag())
    except FileNotFoundError:
        pass
    print("revertly: resumed")
    return 0


def cmd_gc(args) -> int:
    keep_days = args.keep
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for sid in paths.list_session_ids():
        m = _load_meta(sid) or {}
        started = m.get("started", 0)
        trips = any(e.kind in (EventKind.TRIPWIRE, EventKind.SELF_TAMPER)
                    for e in Journal.read(paths.journal_path(sid)))
        if started < cutoff and not trips:  # keep tripwire-flagged sessions longer
            paths.rmtree_force(paths.session_dir(sid))  # clears immutable flags first
            removed += 1
    print(f"revertly gc: removed {removed} session(s) older than {keep_days}d "
          f"(tripwire-flagged kept)")
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
        okc, seq = Journal.verify(jp)
        sealed = "immutable" if paths.is_immutable(jp) else "mutable"
        if okc:
            print(f"  OK        {sid}  ({sealed})")
        else:
            bad += 1
            print(f"  TAMPERED  {sid}  chain breaks at seq {seq}  ({sealed})")
    print(f"\nverified {len(sids)} session(s): "
          f"{len(sids) - bad} intact, {bad} tampered")
    return 1 if bad else 0


def cmd_doctor(args) -> int:
    ok = True
    # shim in PATH and ahead of the real claude?
    which = shutil.which("claude")
    shim = os.path.join(paths.bin_dir(), "claude")
    print(f"shim installed: {os.path.exists(shim)}  ({shim})")
    print(f"`claude` resolves to: {which}")
    if which and os.path.realpath(which) != os.path.realpath(shim) and os.path.exists(shim):
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

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="revertly", description="a seatbelt, not a cage")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("last").set_defaults(func=cmd_last)

    lg = sub.add_parser("log"); lg.add_argument("session", nargs="?")
    lg.add_argument("--outside", action="store_true")
    lg.add_argument("--tripwires", action="store_true")
    lg.add_argument("--tool")
    lg.add_argument("--path")
    lg.set_defaults(func=cmd_log)

    df = sub.add_parser("diff"); df.add_argument("session", nargs="?")
    df.add_argument("paths", nargs="*"); df.set_defaults(func=cmd_diff)

    vs = sub.add_parser("versions"); vs.add_argument("path"); vs.set_defaults(func=cmd_versions)

    rv = sub.add_parser("revert"); rv.add_argument("session", nargs="?")
    rv.add_argument("paths", nargs="*")
    rv.add_argument("--yes", "-y", action="store_true")
    rv.add_argument("--dry-run", action="store_true")
    rv.add_argument("--force", action="store_true")
    rv.set_defaults(func=cmd_revert)

    ui = sub.add_parser("ui"); ui.add_argument("--port", type=int, default=0)
    ui.set_defaults(func=cmd_ui)

    sub.add_parser("config").set_defaults(func=cmd_config)
    sub.add_parser("pause").set_defaults(func=cmd_pause)
    sub.add_parser("resume").set_defaults(func=cmd_resume)

    gc = sub.add_parser("gc"); gc.add_argument("--keep", type=int, default=30)
    gc.set_defaults(func=cmd_gc)

    vf = sub.add_parser("verify")
    vf.add_argument("session", nargs="?")
    vf.add_argument("--all", action="store_true", help="verify every session")
    vf.set_defaults(func=cmd_verify)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    ins = sub.add_parser("install")
    ins.add_argument("--no-profile", action="store_true",
                     help="don't touch the shell profile; just print the PATH line")
    ins.set_defaults(func=cmd_install)

    un = sub.add_parser("uninstall")
    un.add_argument("--purge", action="store_true",
                    help="also delete the session store (~/.revertly and all history)")
    un.add_argument("--yes", "-y", action="store_true")
    un.set_defaults(func=cmd_uninstall)

    sh = sub.add_parser("shim"); sh.add_argument("cmd", nargs=argparse.REMAINDER)
    sh.set_defaults(func=cmd_shim)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)
