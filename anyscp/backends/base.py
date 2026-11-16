# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Protocol-independent file system interface.

Every backend (local disk, SFTP, SCP, FTP/FTPS, WebDAV) implements
:class:`FileSystem`.  All paths are POSIX style ("/" separated, absolute).
Methods are blocking and are always called from worker threads, never from
the GTK main loop.  Backends must translate native exceptions into the
:class:`FSError` hierarchy below so the UI can present them uniformly.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import BinaryIO, Protocol


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

@dataclass
class FileInfo:
    name: str
    path: str
    is_dir: bool = False
    is_link: bool = False
    size: int = 0
    mtime: float | None = None      # POSIX timestamp (UTC)
    mode: int | None = None         # permission bits only (& 0o7777)
    owner: str = ""
    group: str = ""
    link_target: str | None = None
    attr_hidden: bool = False       # Windows "hidden" attribute

    @property
    def hidden(self) -> bool:
        return self.name.startswith(".") or self.attr_hidden


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class FSError(Exception):
    """Base class for every error a backend raises."""


class NotFound(FSError):
    pass


class PermissionDenied(FSError):
    pass


class AlreadyExists(FSError):
    pass


class NotSupported(FSError):
    pass


class AuthFailed(FSError):
    pass


class ConnectionLost(FSError):
    """The connection dropped; the session must reconnect."""


class Cancelled(Exception):
    """User aborted an operation (e.g. dismissed a password prompt)."""


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

CAP_CHMOD = "chmod"
CAP_CHOWN = "chown"
CAP_UTIME = "utime"
CAP_SYMLINK = "symlink"
CAP_EXEC = "exec"        # run a single command, get output
CAP_SHELL = "shell"      # interactive terminal
CAP_RESUME = "resume"    # open_read/open_write with offset > 0


# --------------------------------------------------------------------------
# UI callbacks used during connect
# --------------------------------------------------------------------------

class Prompter(Protocol):
    """Blocking UI callbacks.  Called from worker threads.

    Every method may return ``None`` when the user cancels.
    """

    def ask_password(self, title: str, message: str, remember: bool = True) -> str | None:
        """remember=False: not the site's own password (a jump host's, a proxy's)."""

    def ask_text(self, title: str, message: str, default: str = "") -> str | None: ...

    def ask_interactive(self, title: str, instructions: str,
                        prompts: list[tuple[str, bool]]) -> list[str] | None:
        """Keyboard-interactive auth. ``prompts`` is ``[(text, echo), ...]``."""

    def verify_host_key(self, host: str, port: int, key_type: str,
                        fingerprint: str, changed: bool) -> str:
        """Return ``"save"``, ``"once"`` or ``"reject"``."""

    def verify_certificate(self, host: str, port: int, fingerprint: str,
                           subject: str, issuer: str, error: str) -> str:
        """TLS certificate failed verification. ``"save"``/``"once"``/``"reject"``."""

    def log(self, message: str) -> None: ...


class NullPrompter:
    """Non-interactive prompter (tests, background connections)."""

    def ask_password(self, title, message, remember=True):
        return None

    def ask_text(self, title, message, default=""):
        return None

    def ask_interactive(self, title, instructions, prompts, remember=True):
        return None

    def verify_host_key(self, host, port, key_type, fingerprint, changed):
        return "reject"

    def verify_certificate(self, host, port, fingerprint, subject, issuer, error):
        return "reject"

    def log(self, message):
        pass


# --------------------------------------------------------------------------
# File system
# --------------------------------------------------------------------------

class FileSystem:
    """Abstract file system.  See module docstring for threading rules."""

    protocol: str = ""
    is_local: bool = False
    capabilities: frozenset[str] = frozenset()

    def __init__(self, site: dict | None = None, prompter: Prompter | None = None):
        self.site = dict(site or {})
        self.prompter = prompter or NullPrompter()

    # -- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def clone(self) -> "FileSystem":
        """Return a new, *connected* instance usable concurrently with this
        one (for background transfers).  Must not prompt the user again
        if it can avoid it (reuse credentials captured during connect)."""
        raise NotSupported("clone")

    def keepalive(self) -> None:
        """Called periodically on idle connections."""

    def has(self, cap: str) -> bool:
        return cap in self.capabilities

    # -- paths -------------------------------------------------------------
    def home(self) -> str:
        return "/"

    def realpath(self, path: str) -> str:
        return posixpath.normpath(path)

    @staticmethod
    def join(*parts: str) -> str:
        return posixpath.join(*parts)

    @staticmethod
    def dirname(path: str) -> str:
        return posixpath.dirname(path.rstrip("/")) or "/"

    @staticmethod
    def basename(path: str) -> str:
        return posixpath.basename(path.rstrip("/"))

    # -- queries -----------------------------------------------------------
    def listdir(self, path: str) -> list[FileInfo]:
        """Directory entries without ``.`` and ``..``.  Symlinks to
        directories have ``is_dir=True`` and ``is_link=True``."""
        raise NotImplementedError

    def stat(self, path: str) -> FileInfo:
        """Info about ``path`` (raise :class:`NotFound` if missing)."""
        raise NotImplementedError

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except NotFound:
            return False

    def disk_free(self, path: str) -> int | None:
        return None

    # -- modifications -----------------------------------------------------
    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def remove(self, path: str) -> None:
        """Delete a file or symlink."""
        raise NotImplementedError

    def rmdir(self, path: str) -> None:
        """Delete an *empty* directory."""
        raise NotImplementedError

    def rename(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def chmod(self, path: str, mode: int) -> None:
        raise NotSupported("Changing permissions is not supported by this protocol")

    def chown(self, path: str, owner: str | None, group: str | None) -> None:
        raise NotSupported("Changing ownership is not supported by this protocol")

    def utime(self, path: str, mtime: float) -> None:
        raise NotSupported("Setting timestamps is not supported by this protocol")

    def symlink(self, target: str, link_path: str) -> None:
        raise NotSupported("Symbolic links are not supported by this protocol")

    def readlink(self, path: str) -> str | None:
        return None

    # -- data --------------------------------------------------------------
    def open_read(self, path: str, offset: int = 0) -> BinaryIO:
        """Return an object with ``read(n) -> bytes`` and ``close()``."""
        raise NotImplementedError

    def open_write(self, path: str, offset: int = 0, size: int | None = None) -> BinaryIO:
        """Return an object with ``write(data)`` and ``close()``.

        ``offset > 0`` resumes (appends at ``offset``); ``size`` is the total
        number of bytes that will be written when known (SCP and WebDAV need
        it up front)."""
        raise NotImplementedError

    # -- extras ------------------------------------------------------------
    def exec(self, command: str) -> tuple[int, str, str]:
        raise NotSupported("Running commands is not supported by this protocol")

    def info(self) -> list[tuple[str, str]]:
        """Human readable (label, value) pairs for the session info dialog."""
        return [("Protocol", self.protocol.upper())]

    # -- helpers -----------------------------------------------------------
    def walk(self, top: str):
        """Yield ``(dirpath, dirs, files)`` like :func:`os.walk` (top-down).
        Does not descend into symlinked directories."""
        entries = self.listdir(top)
        dirs = [e for e in entries if e.is_dir]
        files = [e for e in entries if not e.is_dir]
        yield top, dirs, files
        for d in dirs:
            if d.is_link:
                continue
            yield from self.walk(d.path)

    def remove_tree(self, path: str, cancel=None, progress=None) -> None:
        """Recursively delete ``path`` (file or directory)."""
        info = self.stat_nofollow(path)
        if not info.is_dir or info.is_link:
            self.remove(path)
            if progress:
                progress(path)
            return
        for entry in self.listdir(path):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self.remove_tree(entry.path, cancel, progress)
        self.rmdir(path)
        if progress:
            progress(path)

    def stat_nofollow(self, path: str) -> FileInfo:
        """Like :meth:`stat` but reports symlinks as links.  Backends whose
        :meth:`stat` already sets ``is_link`` can rely on this default."""
        return self.stat(path)
