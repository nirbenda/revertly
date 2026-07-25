"""revertly.tripwire — classify filesystem ops against sensitive-path globs.

The TripwireEngine turns a (path, op) pair into an optional TripwireHit.
SELF_TAMPER classification (invariant #6) always takes priority over an
ordinary tripwire: touching revertly's own state or the shell rc files means
someone is trying to disable/evade revertly, and must be flagged as such.

Pure matching over Config globs; no I/O beyond path expansion.
"""
from __future__ import annotations

import os
from typing import Optional

from revertly.config import Config, match_glob
from revertly.model import FsOp, Severity, TripwireHit


class TripwireEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def check(self, path: str, op: FsOp) -> Optional[TripwireHit]:
        abspath = os.path.abspath(os.path.expanduser(path))

        # SELF_TAMPER takes priority over ordinary tripwires.
        matched = match_glob(abspath, self.cfg.self_tamper_globs())
        if matched is not None:
            return TripwireHit(path=abspath, op=op, pattern=matched,
                               severity=Severity.ALERT, self_tamper=True)

        matched = match_glob(abspath, self.cfg.tripwire_globs_all())
        if matched is not None:
            return TripwireHit(path=abspath, op=op, pattern=matched,
                               severity=Severity.ALERT, self_tamper=False)

        return None
