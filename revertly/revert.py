"""revertly revert engine — plan()/apply() over a session pre-image.

The crown jewel. Upholds two invariants (TECH-DESIGN.md):

  #3 Non-destructive: apply() ALWAYS captures the current on-disk state of
     every path it is about to touch into a fresh revert-session (with its
     own clone/) BEFORE mutating anything. A revert can therefore itself be
     reverted — there is no revertly operation that loses data.

  #4 Conflict safety: a path that changed *after* the reverted session ended
     is a Conflict. Conflicts are surfaced in plan.conflicts and are NEVER
     silently overwritten by apply(); they are skipped unless force=True.

Model of the world
-------------------
A session dir holds meta.json (SessionMeta, with `cwd` and `ended`) and a
`clone/` directory that is the CoW PRE-IMAGE of `cwd` at session start.
Reverting means comparing that pre-image against the CURRENT cwd:

  * present in clone, differs in cwd  -> MODIFIED -> restore pre-image bytes
  * present in clone, absent from cwd -> DELETED  -> recreate from pre-image
  * absent in clone, present in cwd   -> CREATED  -> delete from cwd
  * identical                         -> no change

Conflict rule (Phase 1, deliberately simple)
--------------------------------------------
A restore/delete target is a Conflict when the current file's mtime is
strictly greater than meta.ended — i.e. the user (or another session) kept
working on that path after this session sealed. That applies to:
  * a MODIFIED restore target whose current mtime > ended, and
  * a CREATED delete target whose current mtime > ended.
DELETED targets (absent from cwd) have no current file to have diverged, so
they never conflict. Conflicts are listed in plan.conflicts and skipped by
apply() unless force=True.

Python 3.9 stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import time
from typing import List, Optional

from revertly import paths
from revertly.model import (
    Change,
    ChangeType,
    Conflict,
    RevertPlan,
    SessionMeta,
)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_content(a: str, b: str) -> bool:
    """True if files a and b have identical content (size then hash)."""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    return _digest(a) == _digest(b)


def _rename_pairs(journal_path: str, since: float = 0.0) -> List[tuple]:
    """(old, new) pairs from a journal's rename events, tolerant reader."""
    pairs: List[tuple] = []
    if not os.path.isfile(journal_path):
        return pairs
    try:
        with open(journal_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if (ev.get("kind") == "fs" and ev.get("op") == "rename"
                        and ev.get("path") and ev.get("path_from")
                        and (ev.get("t") or 0.0) >= since):
                    pairs.append((ev["path_from"], ev["path"]))
    except OSError:
        pass
    return pairs


def _walk_rel(root: str) -> List[str]:
    """Sorted list of REGULAR file paths under root, relative to root.

    Skips FIFOs/sockets/devices (opening a FIFO to hash it blocks forever)
    and symlinks (following them would let the plan escape the project or
    hash a device). Only regular files are revert candidates.
    """
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            full = os.path.join(dirpath, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue  # symlink, FIFO, socket, device — not a revert target
            out.append(os.path.relpath(full, root))
    out.sort()
    return out


class Reverter:
    """Plans and applies a revert for one session directory."""

    def __init__(self, session_dir: str):
        self.session_dir = os.path.abspath(session_dir)
        self.session_id = os.path.basename(self.session_dir.rstrip(os.sep))
        self.meta = self._load_meta()
        self.cwd = os.path.abspath(self.meta.cwd)
        self.clone = self.meta.clone_path or os.path.join(self.session_dir, "clone")
        self.clone = os.path.abspath(self.clone)
        # Conflict cutoff: prefer meta.ended, but a session killed before
        # seal() has ended=None. Falling back to +inf would disable the
        # "modified after end" rule entirely (silently clobbering later
        # work), so fall back to the newest journal timestamp, then started.
        self.ended = (self.meta.ended
                      if self.meta.ended is not None
                      else self._fallback_cutoff())

    def _fallback_cutoff(self) -> float:
        newest = 0.0
        jp = os.path.join(self.session_dir, "journal.jsonl")
        try:
            with open(jp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line).get("t") or 0.0
                    except ValueError:
                        continue
                    newest = max(newest, t)
        except OSError:
            pass
        return newest or (self.meta.started or 0.0)

    def is_revertible(self) -> tuple:
        """Guard against reverting a session whose pre-image never completed.

        An unarmed session (arm failed under 'proceed') can have an empty or
        missing clone; since revert classifies "in cwd, not in clone" as
        CREATED->delete, planning against it would propose deleting the WHOLE
        project. Returns (ok: bool, reason: str).
        """
        if not os.path.isdir(self.clone):
            return False, "no pre-image (clone dir missing) — nothing to revert from"
        if not self.meta.armed:
            has_clone = any(True for _ in _walk_rel(self.clone))
            if not has_clone:
                return (False,
                        "session did not fully arm and its pre-image is empty; "
                        "reverting would propose deleting the whole project")
        return True, ""

    # ── loading ────────────────────────────────────────────────────────
    def _load_meta(self) -> SessionMeta:
        with open(os.path.join(self.session_dir, "meta.json")) as f:
            return SessionMeta.from_json_dict(json.load(f))

    # ── planning ───────────────────────────────────────────────────────
    def plan(self) -> RevertPlan:
        """Full-session revert: classify every differing path."""
        return self._build_plan(path_filter=None)

    def plan_paths(self, paths_arg: List[str]) -> RevertPlan:
        """Revert restricted to the given files/dirs/globs.

        Each entry may be absolute or relative to cwd. A directory includes
        everything beneath it. Entries containing * ? or [ are ALSO tried as
        globs (revertly.search.path_matches — the same matcher `find` uses),
        so `*.py` selects every .py the session touched at any depth — while
        a literal path that merely contains brackets (Next.js `app/[slug]/`)
        still matches exactly via its prefix.

        Rename-aware: if the selected path was moved (this session or a later
        one — journals record `rename` events), every path in its rename
        chain is selected too, so reverting `A` after `A→B→C` restores A AND
        removes C instead of stranding a duplicate.
        """
        from revertly.search import is_glob, path_matches

        prefixes = [self._normalize(p) for p in paths_arg]
        globs = [p for p in paths_arg if is_glob(p)]

        def base(abs_path: str) -> bool:
            for pre in prefixes:
                if abs_path == pre or abs_path.startswith(pre + os.sep):
                    return True
            for g in globs:
                if path_matches(abs_path, g):
                    return True
            return False

        aliases = self._rename_aliases()

        def keep(abs_path: str) -> bool:
            if base(abs_path):
                return True
            # Rename inference from poll snapshots is signature-based (mtime,
            # size) and CAN mis-pair two unrelated same-signature files. So
            # only follow an alias into a delete if the moved file's CURRENT
            # content actually matches the selected path's pre-image — proving
            # it's really the renamed file, not a coincidental collision.
            for a in aliases.get(abs_path, ()):
                if base(a) and self._alias_confirms(selected=a, moved=abs_path):
                    return True
            return False

        return self._build_plan(path_filter=keep)

    def _alias_confirms(self, selected: str, moved: str) -> bool:
        """True if `moved` (a rename-linked path) is really `selected` moved:
        its current bytes equal `selected`'s pre-image in the clone. Guards
        against deleting a coincidental (mtime,size)-collision on a scoped
        revert."""
        try:
            rel = os.path.relpath(selected, self.cwd)
        except ValueError:
            return False
        blob = os.path.join(self.clone, rel)
        if not (os.path.isfile(blob) and os.path.isfile(moved)):
            return False
        return _same_content(blob, moved)

    # ── rename chains ──────────────────────────────────────────────────
    def _rename_aliases(self) -> dict:
        """Map each path to the set of paths it is connected to through
        `rename` journal events — from this session's journal plus any later
        session in the same store (a later move extends the chain).
        Returns {path: {other paths in its chain}}.
        """
        started = self.meta.started or 0.0
        pairs = _rename_pairs(
            os.path.join(self.session_dir, "journal.jsonl"), since=started)
        store = os.path.dirname(self.session_dir.rstrip(os.sep))
        if os.path.abspath(store) == os.path.abspath(paths.sessions_root()):
            for sid in paths.list_session_ids():
                sdir = os.path.abspath(paths.session_dir(sid))
                if sdir == self.session_dir:
                    continue
                pairs += _rename_pairs(paths.journal_path(sid), since=started)
        # union-find the chains
        parent: dict = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for old, new in pairs:
            parent[find(old)] = find(new)
        groups: dict = {}
        for p in parent:
            groups.setdefault(find(p), set()).add(p)
        return {p: grp - {p} for grp in groups.values() for p in grp}

    def _normalize(self, p: str) -> str:
        if not os.path.isabs(p):
            p = os.path.join(self.cwd, p)
        return os.path.abspath(p)

    def _build_plan(self, path_filter) -> RevertPlan:
        plan = RevertPlan(session_id=self.session_id)

        clone_rels = set(_walk_rel(self.clone)) if os.path.isdir(self.clone) else set()
        cwd_rels = set(_walk_rel(self.cwd)) if os.path.isdir(self.cwd) else set()

        for rel in sorted(clone_rels | cwd_rels):
            abs_path = os.path.join(self.cwd, rel)
            if path_filter is not None and not path_filter(abs_path):
                continue

            clone_path = os.path.join(self.clone, rel)
            in_clone = rel in clone_rels
            in_cwd = rel in cwd_rels

            if in_clone and in_cwd:
                if _same_content(clone_path, abs_path):
                    continue  # unchanged
                # MODIFIED — restore pre-image
                change = Change(path=abs_path, change_type=ChangeType.MODIFIED,
                                pre_blob=clone_path)
                if self._diverged(abs_path):
                    plan.conflicts.append(
                        Conflict(path=abs_path,
                                 reason="modified after session ended"))
                plan.restores.append(change)
            elif in_clone and not in_cwd:
                # DELETED — recreate from pre-image. `in_cwd` only counts
                # REGULAR files, so a symlink/dir now occupying this path would
                # be silently clobbered by the restore. If something non-regular
                # is there (a later session replaced the file), flag a conflict.
                change = Change(path=abs_path, change_type=ChangeType.DELETED,
                                pre_blob=clone_path)
                if os.path.lexists(abs_path):
                    plan.conflicts.append(
                        Conflict(path=abs_path,
                                 reason="path now occupied by a symlink/dir "
                                        "(replaced after the session)"))
                plan.restores.append(change)
            else:  # in_cwd and not in_clone
                # CREATED — delete it.
                change = Change(path=abs_path, change_type=ChangeType.CREATED,
                                pre_blob=None)
                if self._diverged(abs_path):
                    plan.conflicts.append(
                        Conflict(path=abs_path,
                                 reason="created file modified after session ended"))
                plan.deletes.append(change)

        return plan

    def _diverged(self, abs_path: str) -> bool:
        """True if abs_path changed after meta.ended — by content (mtime) OR
        by location/metadata (ctime). The ctime check matters for moves:
        `mv` preserves mtime, so a file moved here by a LATER session would
        otherwise be silently deleted by this revert with no conflict flag.
        """
        try:
            st = os.stat(abs_path)
        except OSError:
            return False
        return max(st.st_mtime, st.st_ctime) > self.ended

    # ── applying ───────────────────────────────────────────────────────
    def apply(self, plan: RevertPlan, *, dry_run: bool = False,
              force: bool = False) -> Optional[str]:
        """Apply a revert plan, non-destructively.

        Steps:
          1. Determine which changes are actually actionable (conflicts are
             skipped unless force=True).
          2. Capture the CURRENT bytes of every path we will touch into a new
             revert-session (is_revert=True, reverts_session=<this id>) with
             its own clone/, so this revert can itself be reverted.
          3. Restore MODIFIED/DELETED targets from their pre_blob; delete
             CREATED targets.

        dry_run=True performs no capture and no mutation; it returns the id
        that *would* have been minted (its dir is never created), or None if
        there is nothing to do.

        Returns the new revert-session id, or None if nothing was applied.
        """
        conflict_paths = {c.path for c in plan.conflicts}

        def actionable(change: Change) -> bool:
            if force:
                return True
            return change.path not in conflict_paths

        restores = [c for c in plan.restores if actionable(c)]
        deletes = [c for c in plan.deletes if actionable(c)]

        if not restores and not deletes:
            return None

        revert_id = paths.new_session_id("revert")

        if dry_run:
            return revert_id

        # 2. capture current state of every affected path into revert-session.
        self._capture(revert_id, restores, deletes)

        # 3. mutate — DELETES FIRST. A created file can occupy a path (or
        # block a parent) that a restore needs: session did `rm -rf dir` then
        # wrote a FILE named `dir` -> restoring dir/x before deleting the
        # file crashes; likewise a created dir sitting where a file must be
        # restored. Clearing created paths first makes restores land on
        # clean ground. Individual failures are collected, never abort the
        # rest of the plan.
        for change in deletes:
            try:
                self._delete(change)
            except OSError as exc:
                plan.errors.append("delete %s: %s" % (change.path, exc))
        for change in restores:
            try:
                self._restore(change)
            except OSError as exc:
                plan.errors.append("restore %s: %s" % (change.path, exc))

        # 4. the revert-session's `ended` must be later than every mutation
        # above (ctime-aware conflict detection compares against it), so
        # stamp it NOW — not at capture time.
        self._stamp_ended(revert_id)

        return revert_id

    def _stamp_ended(self, revert_id: str) -> None:
        mp = paths.meta_path(revert_id)
        try:
            with open(mp) as f:
                meta = json.load(f)
            meta["ended"] = time.time()
            with open(mp, "w") as f:
                json.dump(meta, f)
        except (OSError, ValueError):
            pass

    def _capture(self, revert_id: str, restores: List[Change],
                 deletes: List[Change]) -> None:
        """Build the revert-session dir: meta.json + clone/ of current bytes.

        The new clone/ mirrors the reverted session's layout: it is keyed on
        paths relative to cwd, so a Reverter constructed on the revert-session
        will compare that captured pre-image against cwd exactly the same way.
        """
        r_session_dir = paths.session_dir(revert_id)
        r_clone = paths.clone_dir(revert_id)
        paths.ensure_dir(r_clone)

        # Every path the revert touches: capture its CURRENT on-disk bytes.
        # For CREATED files (which currently exist) and MODIFIED files, this
        # records the post-session content so revert-the-revert can restore it.
        # DELETED targets have no current file — nothing to capture, and their
        # absence in the capture clone means revert-the-revert will re-delete.
        for change in list(restores) + list(deletes):
            src = change.path
            if not os.path.isfile(src):
                continue
            rel = os.path.relpath(src, self.cwd)
            dst = os.path.join(r_clone, rel)
            os.makedirs(os.path.dirname(dst) or r_clone, exist_ok=True)
            shutil.copy2(src, dst)

        meta = SessionMeta(
            id=revert_id,
            name="revert of %s" % self.session_id,
            cwd=self.cwd,
            argv=["revertly", "revert", self.session_id],
            started=self.meta.ended if self.meta.ended is not None else 0.0,
            ended=os.path.getmtime(r_clone) if os.path.exists(r_clone) else None,
            clone_path=r_clone,
            armed=True,
            is_revert=True,
            reverts_session=self.session_id,
        )
        # Set ended to "now-ish" via the clone dir mtime so the revert-session's
        # own conflict rule behaves; fall back handled above.
        with open(paths.meta_path(revert_id), "w") as f:
            json.dump(meta.to_json_dict(), f)

    def _restore(self, change: Change) -> None:
        """Restore MODIFIED/DELETED target from its pre-image bytes."""
        assert change.pre_blob is not None
        dst = change.path
        # A symlink (or a dir where the agent replaced our file with one) at
        # dst would make copy2 write THROUGH it — clobbering data outside the
        # project and never actually restoring dst. Remove any non-regular
        # occupant first so the restore lands on the real path.
        if os.path.islink(dst):
            os.unlink(dst)
        elif os.path.isdir(dst):
            raise IsADirectoryError(
                "restore target is (now) a directory: %s" % dst)
        os.makedirs(os.path.dirname(dst) or self.cwd, exist_ok=True)
        shutil.copy2(change.pre_blob, dst)

    def _delete(self, change: Change) -> None:
        """Delete a CREATED target from cwd."""
        try:
            os.remove(change.path)
        except FileNotFoundError:
            pass
        # prune now-empty parent dirs up to (but not including) cwd
        parent = os.path.dirname(change.path)
        while parent and os.path.abspath(parent) != self.cwd:
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
