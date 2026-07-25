"""Cloner interface + a real APFS clonefile backend and a Fake.

A Cloner copies files/trees. On APFS, `cp -c` requests a copy-on-write
clonefile (near-instant, space-efficient); on non-APFS it falls back to a
regular copy. Bytes must be identical either way. The Fake performs real
shutil copies so downstream code sees real files, while recording calls.

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
    """Real backend using `cp -c` (APFS clonefile) with a plain-copy fallback."""

    def clone_tree(self, src: str, dst: str) -> None:
        # -R recurse, -c request clonefile. Fall back to -R if clonefile fails
        # (e.g. non-APFS volume). Each fallback must start from a CLEAN dst:
        # a partial `cp -Rc` left behind makes the retry copy src INTO it
        # (BSD cp nests as dst/<srcname>/…) and then copytree raises
        # FileExistsError — corrupting the pre-image. Clear dst between tries.
        if self._run(["cp", "-Rc", src, dst]):
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
        if not self._run(["cp", "-c", src, dst]):
            if not self._run(["cp", src, dst]):
                shutil.copy2(src, dst)

    def is_cow(self) -> bool:
        # Best effort: APFS is the default on modern macOS.
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
