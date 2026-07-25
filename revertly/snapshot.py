"""Snapshotter interface + a real tmutil backend and an in-memory Fake.

A Snapshotter takes a point-in-time APFS local snapshot so a session can, in
principle, be restored to the exact filesystem state at arm-time. Real backends
shell out to `tmutil`; the Fake is fully in-memory for hermetic tests.

Python 3.9, stdlib only.
"""
import abc
import re
import shutil
import subprocess
from typing import List, Optional


# tmutil prints e.g. "Created local snapshot with date: 2026-07-25-103000"
_SNAPSHOT_RE = re.compile(r"Created local snapshot with date:\s*(\S+)")


def parse_snapshot_name(stdout: str) -> Optional[str]:
    """Extract the snapshot date/identifier from `tmutil localsnapshot` output.

    Returns the identifier string (e.g. "2026-07-25-103000") or None if the
    output does not contain a recognizable snapshot line.
    """
    if not stdout:
        return None
    m = _SNAPSHOT_RE.search(stdout)
    if m:
        return m.group(1)
    return None


class Snapshotter(abc.ABC):
    """Abstract point-in-time filesystem snapshot backend."""

    @abc.abstractmethod
    def create(self) -> Optional[str]:
        """Create a snapshot; return its name/identifier, or None on failure."""

    @abc.abstractmethod
    def delete(self, name: str) -> None:
        """Delete the named snapshot. May be privileged and raise on failure."""

    @abc.abstractmethod
    def can_snapshot(self) -> bool:
        """Whether snapshotting is available in this environment."""


class TmutilSnapshotter(Snapshotter):
    """Real backend using macOS `tmutil` local snapshots.

    Construction never fails even if tmutil is missing; `can_snapshot()`
    reports availability so callers can degrade gracefully.
    """

    def create(self) -> Optional[str]:
        if not self.can_snapshot():
            return None
        try:
            proc = subprocess.run(
                ["tmutil", "localsnapshot"],
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if proc.returncode != 0:
            return None
        return parse_snapshot_name(proc.stdout or "")

    def delete(self, name: str) -> None:
        # `tmutil deletelocalsnapshots` is privileged; surface failures clearly.
        try:
            proc = subprocess.run(
                ["tmutil", "deletelocalsnapshots", name],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise RuntimeError(
                "failed to run tmutil deletelocalsnapshots: %s" % exc
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(
                "tmutil deletelocalsnapshots %s failed (rc=%d): %s"
                % (name, proc.returncode, (proc.stderr or "").strip())
            )

    def can_snapshot(self) -> bool:
        return shutil.which("tmutil") is not None


class FakeSnapshotter(Snapshotter):
    """In-memory snapshotter for hermetic tests.

    Records created names in `.created` and deleted names in `.deleted`.
    """

    def __init__(self, can: bool = True):
        self._can = can
        self._counter = 0
        self.created: List[str] = []
        self.deleted: List[str] = []

    def create(self) -> Optional[str]:
        self._counter += 1
        name = "fake-snapshot-%04d" % self._counter
        self.created.append(name)
        return name

    def delete(self, name: str) -> None:
        if name not in self.created:
            raise RuntimeError("unknown snapshot: %s" % name)
        self.created.remove(name)
        self.deleted.append(name)

    def can_snapshot(self) -> bool:
        return self._can
