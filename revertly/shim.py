"""The `claude` shim: arm the net, run the real command, seal, summarize.

Fail-safe by construction: if anything about arming goes wrong, we still run
the real command (visibly unprotected) rather than block the user's workflow.
The shim is an ergonomic default, NOT a security boundary (see THREAT-MODEL.md).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from typing import List

from . import paths
from .config import Config, load as load_config
from .session import Session, ArmError


def _repo_root() -> str:
    # parent dir of the revertly package, so the shim can find it on PYTHONPATH
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_real(cmd_name: str) -> str:
    """Find the real binary for cmd_name, skipping our own shim dir."""
    shim = paths.bin_dir()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if os.path.realpath(d) == os.path.realpath(shim):
            continue
        cand = os.path.join(d, cmd_name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return cmd_name  # fall back to bare name


PROFILE_BEGIN = "# >>> revertly >>>"
PROFILE_END = "# <<< revertly <<<"


def _chmod_x(path: str):
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def install_shim(cmd_name: str = "claude") -> str:
    paths.ensure_store()
    paths.ensure_dir(paths.bin_dir())
    real = _find_real(cmd_name)
    shim_path = os.path.join(paths.bin_dir(), cmd_name)
    script = f"""#!/bin/bash
# revertly shim for `{cmd_name}` — arms the safety net, then runs the real thing.
if [ -n "$REVERTLY_DISABLE" ]; then exec {real} "$@"; fi
export PYTHONPATH="{_repo_root()}:$PYTHONPATH"
exec {sys.executable} -m revertly shim -- {real} "$@"
"""
    with open(shim_path, "w") as f:
        f.write(script)
    _chmod_x(shim_path)
    return shim_path


def install_launcher() -> str:
    """Write a `revertly` launcher onto ~/.revertly/bin so `revertly …` works on PATH
    without the caller needing to set PYTHONPATH or use `python3 -m revertly`."""
    paths.ensure_dir(paths.bin_dir())
    launcher = os.path.join(paths.bin_dir(), "revertly")
    script = f"""#!/bin/bash
# revertly launcher (installed) — self-contained entrypoint.
export PYTHONPATH="{_repo_root()}:$PYTHONPATH"
exec {sys.executable} -m revertly "$@"
"""
    with open(launcher, "w") as f:
        f.write(script)
    _chmod_x(launcher)
    return launcher


def detect_profile() -> str:
    shell = os.environ.get("SHELL", "")
    home = os.path.expanduser("~")
    if "zsh" in shell:
        return os.path.join(home, ".zshrc")
    if "bash" in shell:
        # macOS bash logins read .bash_profile; fall back to .bashrc
        for name in (".bash_profile", ".bashrc"):
            p = os.path.join(home, name)
            if os.path.exists(p):
                return p
        return os.path.join(home, ".bash_profile")
    return os.path.join(home, ".profile")


def _profile_block() -> str:
    return (f"{PROFILE_BEGIN}\n"
            f'export PATH="{paths.bin_dir()}:$PATH"\n'
            f"{PROFILE_END}\n")


def add_path_to_profile(profile: str = None) -> tuple:
    """Idempotently add the revertly bin dir to PATH in the shell profile.
    Returns (profile_path, changed:bool)."""
    profile = profile or detect_profile()
    existing = ""
    if os.path.exists(profile):
        with open(profile) as f:
            existing = f.read()
    if PROFILE_BEGIN in existing:
        return profile, False
    sep = "" if existing.endswith("\n") or not existing else "\n"
    with open(profile, "a") as f:
        f.write(sep + "\n" + _profile_block())
    return profile, True


def remove_path_from_profile(profile: str = None) -> tuple:
    """Remove the revertly-managed block from the profile. Returns (profile, changed)."""
    profile = profile or detect_profile()
    if not os.path.exists(profile):
        return profile, False
    with open(profile) as f:
        lines = f.readlines()
    out, skipping, changed = [], False, False
    for ln in lines:
        if ln.strip() == PROFILE_BEGIN:
            skipping, changed = True, True
            continue
        if ln.strip() == PROFILE_END:
            skipping = False
            continue
        if not skipping:
            out.append(ln)
    if changed:
        # collapse a possible leftover blank run where the block was
        text = "".join(out)
        with open(profile, "w") as f:
            f.write(text)
    return profile, changed


def uninstall(purge: bool = False, profile: bool = True) -> dict:
    """Remove shim + launcher (+ optionally PATH line and the whole store)."""
    result = {"removed": [], "profile": None, "purged": False}
    for name in ("claude", "revertly"):
        p = os.path.join(paths.bin_dir(), name)
        if os.path.exists(p):
            os.remove(p)
            result["removed"].append(p)
    if profile:
        prof, changed = remove_path_from_profile()
        result["profile"] = (prof, changed)
    if purge:
        home = paths.revertly_home()
        if os.path.isdir(home):
            paths.rmtree_force(home)  # clears immutable (uchg) flags first
            result["purged"] = True
    return result


def run_wrapped(cmd: List[str]) -> int:
    # argparse REMAINDER may include a leading '--'
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("revertly shim: no command given", file=sys.stderr)
        return 2

    # escape hatches
    if os.environ.get("REVERTLY_DISABLE"):
        return _exec_unprotected(cmd, reason="REVERTLY_DISABLE set")
    if os.path.exists(os.path.join(paths.revertly_home(), "paused")):
        return _exec_unprotected(cmd, reason="revertly is paused")

    cfg = load_config(paths.config_path())
    cwd = os.getcwd()

    try:
        sess = Session(cwd=cwd, argv=cmd, cfg=cfg)
        sess.arm()
    except ArmError as e:
        print(f"revertly ⚠ could not arm safety net: {e}\n"
              f"       running UNPROTECTED (set on_arm_failure=abort to refuse).",
              file=sys.stderr)
        return _exec_unprotected(cmd, reason="arm failed")
    except Exception as e:  # our bug must never block the user
        print(f"revertly ⚠ internal error while arming ({e}); running unprotected.",
              file=sys.stderr)
        return _exec_unprotected(cmd, reason="internal error")

    print(f"revertly: armed session {sess.id} (snapshot+clone+watch active)", file=sys.stderr)
    exit_code = 0
    try:
        exit_code = subprocess.call(cmd)
    except KeyboardInterrupt:
        exit_code = 130
    finally:
        try:
            sess.seal(exit_code)
            print(sess.summary_line(), file=sys.stderr)
        except Exception as e:
            print(f"revertly ⚠ error sealing session: {e}", file=sys.stderr)
    return exit_code


def _exec_unprotected(cmd: List[str], reason: str) -> int:
    print(f"revertly: running without protection ({reason})", file=sys.stderr)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
