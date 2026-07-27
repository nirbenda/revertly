"""Best-effort desktop notification, cross-platform. Never raises or blocks.

macOS uses `osascript` (built in); Linux uses `notify-send` (libnotify, the
standard on Ubuntu/GNOME/KDE) when present. Suppressed by REVERTLY_NO_NOTIFY=1
(set in tests and CI). If no notifier is available the call is a silent no-op —
the incident log and stderr line are the durable record either way.
"""
import os
import shutil
import subprocess
import sys


def desktop(title: str, body: str) -> None:
    if os.environ.get("REVERTLY_NO_NOTIFY"):
        return
    try:
        full_title = f"revertly: {title}"
        safe_body = str(body).replace('"', "'")[:200]
        if sys.platform == "darwin" and shutil.which("osascript"):
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{safe_body}" with title "{full_title}"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):          # Linux (libnotify)
            subprocess.Popen(
                ["notify-send", full_title, safe_body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
