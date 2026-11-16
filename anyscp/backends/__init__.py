# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""File system backends."""

from __future__ import annotations

from .base import FileSystem, Prompter


def create_fs(site: dict, prompter: Prompter | None = None) -> FileSystem:
    """Instantiate (but do not connect) the backend for ``site``."""
    proto = site.get("protocol", "sftp")
    if proto == "sftp":
        from .sftp import SFTPFileSystem
        return SFTPFileSystem(site, prompter)
    if proto == "scp":
        from .scp import SCPFileSystem
        return SCPFileSystem(site, prompter)
    if proto == "ftp":
        from .ftp import FTPFileSystem
        return FTPFileSystem(site, prompter)
    if proto == "webdav":
        from .webdav import WebDAVFileSystem
        return WebDAVFileSystem(site, prompter)
    if proto == "local":
        from .local import LocalFileSystem
        return LocalFileSystem()
    raise ValueError(f"Unknown protocol {proto!r}")
