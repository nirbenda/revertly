"""revertly configuration: defaults, load/save, and glob matching helpers.

TOML is parsed with a tiny stdlib-only reader (Python 3.9 has no tomllib).
We only need a flat-ish subset, so we keep the format simple and forgiving.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field


# Paths whose mutation means "someone is trying to disable/evade revertly".
# These are always active and cannot be removed by user config (Tier-1 policy).
SELF_TAMPER_GLOBS = [
    "~/.revertly/**",
    "~/.revertly",
    "~/.zshrc", "~/.zprofile", "~/.zshenv",
    "~/.bashrc", "~/.bash_profile", "~/.profile",
    "~/.config/fish/**",
]

# Default sensitive-path tripwires (credentials, persistence, system).
# The .env FAMILY is covered, not just the bare name: `.env.local`,
# `.env.production`, `secrets.env`, etc. are exactly where secrets hide.
DEFAULT_TRIPWIRE_GLOBS = [
    "~/.ssh/**", "~/.aws/**", "~/.config/gh/**", "~/.gnupg/**",
    "~/Library/LaunchAgents/**", "~/Library/LaunchDaemons/**",
    "/etc/**", "**/id_rsa*", "**/id_ed25519*", "**/*.pem",
    "**/.env", "**/.env.*", "**/*.env", "**/.npmrc", "**/.pypirc",
]

# Excluded from BOTH the watcher and the clone (see Session.arm clone prune).
#   * .git — reverting git internals (refs/index) can corrupt the repo or
#     un-commit work; every git op would also spam the journal. Leave it to git.
#   * .claude — the agent's own workspace/session state; not ours to revert.
#   * node_modules / venvs / caches — huge, regenerable, not worth cloning.
DEFAULT_EXCLUDE_GLOBS = [
    "~/Library/Caches/**", "~/.revertly/**",
    "**/node_modules/**", "**/.git/**", "**/.claude/**",
    "**/.venv/**", "**/venv/**",
    "**/.DS_Store", "**/__pycache__/**", "**/*.pyc",
]


@dataclass
class Config:
    watch_scope: str = "."               # "." = the session's project dir (cwd)
    exclude: list = field(default_factory=lambda: list(DEFAULT_EXCLUDE_GLOBS))
    tripwire_paths: list = field(default_factory=lambda: list(DEFAULT_TRIPWIRE_GLOBS))
    tripwire_on_write: str = "alert"     # alert | log   (block = Phase 2)
    tripwire_on_read: str = "alert"
    retention_days: int = 30
    max_disk_gb: float = 10.0
    on_arm_failure: str = "ask"          # ask | proceed | abort
    poll_interval: float = 0.5           # PollingWatcher cadence (seconds)

    # ---- glob helpers (operate on absolute, expanded paths) ----

    def _expand(self, globs: list) -> list:
        return [os.path.expanduser(g) for g in globs]

    def is_excluded(self, path: str) -> bool:
        return _any_match(path, self._expand(self.exclude))

    def tripwire_globs_all(self) -> list:
        # SELF_TAMPER globs are always included and always active.
        return self._expand(self.tripwire_paths) + self._expand(SELF_TAMPER_GLOBS)

    def self_tamper_globs(self) -> list:
        # Include the ACTUAL store location, not just the hard-coded
        # ~/.revertly — a custom $REVERTLY_HOME must still be self-protected.
        from revertly import paths
        home = paths.revertly_home()
        dynamic = [home, os.path.join(home, "**")]
        return self._expand(SELF_TAMPER_GLOBS) + dynamic

    # ---- config-weakening detection (security) ----

    def risky_excludes(self) -> list:
        """Exclude globs broad enough to blind the watcher to real activity."""
        broad = {"**", "*", "~", "~/*", "~/**", "/", "/*", "/**",
                 "$HOME", "$HOME/*", "$HOME/**", ""}
        home = os.path.expanduser("~")
        out = []
        for g in self.exclude:
            gs = g.strip()
            base = gs.rstrip("/*").strip()
            if gs in broad or base in ("", "~", home):
                out.append(g)
        return out

    def tripwires_weakened(self) -> bool:
        """True if the user emptied the sensitive-path tripwire set.
        (SELF_TAMPER globs are always active regardless — they can't be removed.)"""
        return len(self.tripwire_paths) == 0


def _any_match(path: str, globs: list) -> bool:
    return match_glob(path, globs) is not None


def match_glob(path: str, globs: list):
    """Return the first glob that matches `path`, or None.

    Supports `**` as "any number of path segments" in addition to fnmatch.
    """
    norm = os.path.normpath(path)
    for g in globs:
        gn = os.path.normpath(g)
        if fnmatch.fnmatch(norm, gn):
            return g
        # `foo/**` (or `**/foo/**`) must also match the DIRECTORY `foo` itself,
        # not only paths beneath it — otherwise the watcher never prunes the
        # dir and descends into node_modules/.git on every poll. Match `norm`
        # against the base (with /** stripped); fnmatch's `**`→`.*` crosses '/'
        # so `**/node_modules` matches `/proj/node_modules`.
        if gn.endswith(os.sep + "**"):
            base = gn[:-3]
            if (norm == base or norm.startswith(base + os.sep)
                    or fnmatch.fnmatch(norm, base)):
                return g
        if "**" in gn:
            # collapse ** to * for a looser secondary attempt
            loose = gn.replace("**", "*")
            if fnmatch.fnmatch(norm, loose):
                return g
    return None


# ─────────────────────── minimal TOML load/save ───────────────────────

def load(path: str) -> Config:
    """Load config from a simple TOML file; missing file → defaults."""
    cfg = Config()
    if not path or not os.path.exists(path):
        return cfg
    section = None
    with open(path, "r") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            _apply(cfg, section, key, _parse_val(val))
    return cfg


def _parse_val(val: str):
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",")]
    if val.startswith('"') or val.startswith("'"):
        return val.strip('"').strip("'")
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(val)
    except ValueError:
        try:
            return float(val)
        except ValueError:
            return val.strip('"').strip("'")


def _apply(cfg: Config, section, key, val):
    mapping = {
        ("watch", "scope"): "watch_scope",
        ("watch", "exclude"): "exclude",
        ("tripwires", "paths"): "tripwire_paths",
        ("tripwires", "on_write"): "tripwire_on_write",
        ("tripwires", "on_read"): "tripwire_on_read",
        ("retention", "sessions_days"): "retention_days",
        ("retention", "max_disk_gb"): "max_disk_gb",
        (None, "on_arm_failure"): "on_arm_failure",
        ("watch", "poll_interval"): "poll_interval",
    }
    attr = mapping.get((section, key))
    if attr:
        setattr(cfg, attr, val)
