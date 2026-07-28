"""Best-effort desktop notification, cross-platform. Never raises or blocks.

macOS uses `osascript` (built in); Linux uses `notify-send` (libnotify, the
standard on Ubuntu/GNOME/KDE) when present. Suppressed by REVERTLY_NO_NOTIFY=1
(set in tests and CI). If no notifier is available the call is a silent no-op —
the incident log and stderr line are the durable record either way.

CRITICAL: osascript is invoked by its ABSOLUTE real path (/usr/bin/osascript),
never as a bare "osascript" resolved via PATH. `osascript` is a guarded command,
so during a session the agent's PATH has revertly's cmdbin shim first — calling
"osascript" here would hit that shim, be flagged SUSPICIOUS, fire another
notification, and recurse into a notification storm. The absolute path bypasses
the shim and breaks the loop.
"""
import os
import shutil
import subprocess
import sys

# The real macOS osascript. Fixed OS location; bypasses any PATH shim.
_OSASCRIPT = "/usr/bin/osascript"


def desktop(title: str, body: str) -> None:
    if os.environ.get("REVERTLY_NO_NOTIFY"):
        return
    try:
        full_title = f"revertly: {title}"
        safe_body = str(body).replace('"', "'")[:200]
        if sys.platform == "darwin" and os.path.exists(_OSASCRIPT):
            subprocess.Popen(
                [_OSASCRIPT, "-e",
                 f'display notification "{safe_body}" with title "{full_title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform != "darwin" and shutil.which("notify-send"):  # Linux (libnotify)
            subprocess.Popen(
                ["notify-send", full_title, safe_body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
