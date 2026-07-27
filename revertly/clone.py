"""Cloner interface + a real copy-on-write backend and a Fake.

A Cloner copies files/trees, using the platform's copy-on-write path when it
exists so the pre-image is near-instant and space-efficient:
  * macOS/APFS  -> `cp -c` (clonefile)
  * Linux       -> `cp --reflink=auto` (reflink on Btrfs/XFS/bcachefs, plain
                   copy elsewhere — always succeeds, CoW where the FS allows)
Bytes must be identical either way; every path falls back to a plain copy and
finally a stdlib copy. The Fake performs real shutil copies so downstream code
sees real files, while recording calls.

Python 3.9, stdlib only.
"""
import abc
import os
import shutil
import subprocess
import sys
from typing import List, Tuple


class Cloner(abc.ABC):
    """Abstract file/tree copier (copy-on-write where available)."""

    @abc.abstractmethod
    def clone_tree(self, src: str, dst: str) -> None:
        """Copy directory tree src -> dst (CoW when possible)."""

    @abc.abstractmethod
    def clone_file(self, src: str, dst: str) -> None:
        """Copy a single file src -> dst (CoW when possible)."""

    @abc.abstractmethod
    def is_cow(self) -> bool:
        """Whether copies are copy-on-write on this filesystem/platform."""


class ClonefileCloner(Cloner):
    """Real backend using the platform's copy-on-write copy (APFS clonefile on
    macOS, reflink on Linux) with a plain-copy fallback."""

    @staticmethod
    def _cow_tree_argv(src: str, dst: str) -> List[str]:
        if sys.platform == "darwin":
            return ["cp", "-Rc", src, dst]                 # APFS clonefile
        return ["cp", "-R", "--reflink=auto", src, dst]    # GNU cp reflink

    @staticmethod
    def _cow_file_argv(src: str, dst: str) -> List[str]:
        if sys.platform == "darwin":
            return ["cp", "-c", src, dst]
        return ["cp", "--reflink=auto", src, dst]

    def clone_tree(self, src: str, dst: str) -> None:
        # Try the CoW copy first. Fall back to a plain -R copy if it fails
        # (old cp without --reflink, or a filesystem that rejects it). Each
        # fallback must start from a CLEAN dst: a partial `cp -R` left behind
        # makes the retry copy src INTO it (cp nests as dst/<srcname>/…) and
        # then copytree raises FileExistsError — corrupting the pre-image.
        if self._run(self._cow_tree_argv(src, dst)):
            return
        self._clear(dst)
        if self._run(["cp", "-R", src, dst]):
            return
        self._clear(dst)
        # Last-resort stdlib copy so bytes still land correctly.
        shutil.copytree(src, dst)

    @staticmethod
    def _clear(path: str) -> None:
        if os.path.lexists(path):
            shutil.rmtree(path, ignore_errors=True)
            if os.path.lexists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def clone_file(self, src: str, dst: str) -> None:
        if not self._run(self._cow_file_argv(src, dst)):
            if not self._run(["cp", src, dst]):
                shutil.copy2(src, dst)

    def is_cow(self) -> bool:
        # Honest "clones are ~free" claim: guaranteed only on APFS. On Linux
        # `--reflink=auto` MAY be CoW (Btrfs/XFS) or a full copy (ext4); we
        # don't promise cheap there, so callers warn about clone cost.
        return sys.platform == "darwin"

    @staticmethod
    def _run(argv: List[str]) -> bool:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True)
        except OSError:
            return False
        return proc.returncode == 0


class FakeCloner(Cloner):
    """Records calls in `.tree_calls`/`.file_calls` and does a real copy."""

    def __init__(self, cow: bool = True):
        self._cow = cow
        self.tree_calls: List[Tuple[str, str]] = []
        self.file_calls: List[Tuple[str, str]] = []

    def clone_tree(self, src: str, dst: str) -> None:
        self.tree_calls.append((src, dst))
        shutil.copytree(src, dst)

    def clone_file(self, src: str, dst: str) -> None:
        self.file_calls.append((src, dst))
        shutil.copy2(src, dst)

    def is_cow(self) -> bool:
        return self._cow
