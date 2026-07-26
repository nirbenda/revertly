"""revertly.guard — the HARNESS-AGNOSTIC command interception layer (Layer 2).

Hooks see an agent's tool calls, but they are per-agent (Claude has them; Codex
/ Cursor may not). This layer intercepts at the *effect* layer instead: revertly
prepends a dir of intercepting shims for a small set of dangerous commands
(bash/sh/zsh + curl/wget/nc/scp/launchctl/crontab/osascript) to the wrapped
agent's PATH. When ANY agent — or a script it spawns — runs one of those, the
shim runs first, classifies the full command (same patterns as the hook), logs
it, and in `block` mode refuses with a MESSAGE ON STDERR that the agent reads as
command output and can course-correct on. Works for every harness that shells
out; needs no per-agent integration.

Honest ceiling: evadable by calling a command by absolute path (`/usr/bin/curl`)
or by doing the read/network in-process (python `open()`/`urllib`). It is
tamper-RAISING, not a boundary — the real boundary is the kernel (sandbox-exec
opt-in, or the Endpoint-Security build). ALWAYS fail-open: any error runs the
real command, so the guard can never break the agent.
"""
from __future__ import annotations

import os
import stat
import sys
from typing import List, Optional

from revertly import paths

# shells (see the full command string via `-c "..."`) + direct-danger tools
GUARDED = ["bash", "sh", "zsh", "curl", "wget", "nc", "ncat",
           "scp", "sftp", "launchctl", "crontab", "osascript"]

_BLOCK_KINDS = {"SUSPICIOUS", "SELF_TAMPER", "READ"}


def resolve_real(name: str) -> Optional[str]:
    """The real executable for `name`, skipping our cmdbin so a shim never
    resolves to itself."""
    cb = os.path.realpath(paths.cmdbin_dir())
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.realpath(d) == cb:
            continue
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def ensure_cmd_shims() -> List[str]:
    """Create/refresh the intercepting shims for whichever guarded commands are
    present on the system. Returns the command names shimmed."""
    cb = paths.ensure_dir(paths.cmdbin_dir())
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    made = []
    for name in GUARDED:
        real = resolve_real(name)
        if not real:
            continue
        script = f"""#!/bin/bash
# revertly command guard for `{name}` — fail-open by construction.
REAL={_q(real)}
[ -n "$REVERTLY_GUARD_DISABLE" ] && exec "$REAL" "$@"
export PYTHONPATH={_q(repo)}:$PYTHONPATH
if {_q(sys.executable)} -c 'import revertly.guard' 2>/dev/null; then
  exec {_q(sys.executable)} -m revertly.guard {name} -- "$@"
else
  exec "$REAL" "$@"
fi
"""
        p = os.path.join(cb, name)
        with open(p, "w") as f:
            f.write(script)
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        made.append(name)
    return made


def _q(s: str) -> str:
    return '"' + s.replace('"', '\\"') + '"'


def decide(findings, cfg, allow: bool):
    """Pure policy: given findings + config + the REVERTLY_ALLOW escape, return
    (action, finding) where action is 'block' or 'allow'."""
    mode = getattr(cfg, "guard_mode", "alert")
    if mode == "block" and not allow:
        for f in findings:
            if f.kind in _BLOCK_KINDS:
                return "block", f
    return "allow", None


def run(name: str, cmd_args: List[str]) -> int:
    """Guard one invocation. Classifies, logs, and either blocks (returns non-0
    with a message on stderr) or exec's the real command (never returns).
    Fail-open: any error -> exec real."""
    real = resolve_real(name)
    try:
        from revertly import hooks
        from revertly.config import load
        cfg = load(paths.config_path())
        if cfg.guard_mode == "off":
            raise RuntimeError("guard off")
        cmdstr = " ".join([name] + list(cmd_args))
        findings = hooks.classify("Bash", {"command": cmdstr}, cfg)
        if findings:
            hooks.record_findings(findings)
            # escape: REVERTLY_ALLOW in the env OR prefixed on the command
            # (so the agent can act on the block message by re-running it).
            allow = bool(os.environ.get("REVERTLY_ALLOW")) or "REVERTLY_ALLOW" in cmdstr
            action, f = decide(findings, cfg, allow)
            if action == "block":
                print(
                    f"revertly: BLOCKED this command — {f.detail}\n"
                    f"  Flagged as {f.kind}. If it is intentional and safe, re-run it "
                    f"prefixed with REVERTLY_ALLOW=1, or ask the user to run it.",
                    file=sys.stderr)
                return 126
    except Exception:
        pass   # fail-open
    if not real:
        # nothing real to run (shouldn't happen — shim only made for present cmds)
        return 127
    try:
        os.execv(real, [real] + list(cmd_args))
    except OSError:
        return 127
    return 0


def main(argv=None) -> int:
    """Entry for `python -m revertly.guard <name> -- <args…>`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return 0
    name = argv[0]
    rest = argv[argv.index("--") + 1:] if "--" in argv else argv[1:]
    return run(name, rest)


if __name__ == "__main__":
    sys.exit(main())
