# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Platform helpers.

anySCP handles every path – local and remote – as a POSIX-style string so
that the panes, breadcrumbs, transfer queue and sync code are identical on
all platforms.  On Windows, local paths are mapped like this:

    ``/``                  "This PC": the list of drives
    ``/C:``                ``C:\\``
    ``/C:/Users/me``       ``C:\\Users\\me``
    ``//server/share/x``   ``\\\\server\\share\\x`` (UNC)

On Linux and other POSIX systems the mapping is the identity.
"""

from __future__ import annotations

import os
import string
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def _is_internal_windows(path: str) -> bool:
    return path == "/" or path.startswith("//") or (
        len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha())


def to_native(path: str) -> str:
    """Internal (POSIX-style) local path → path for the OS."""
    if not IS_WINDOWS:
        return path
    if path in ("", "/"):
        return ""                       # the drive list has no OS path
    if path.startswith("//"):
        return path.replace("/", "\\")
    if _is_internal_windows(path):
        drive, rest = path[1:3], path[3:]
        return drive.upper() + "\\" + rest.lstrip("/").replace("/", "\\")
    return path.replace("/", "\\")


def to_internal(native: str) -> str:
    """OS path → internal POSIX-style path."""
    if not IS_WINDOWS:
        return native
    if not native:
        return "/"
    if _is_internal_windows(native.replace("\\", "/")) and "\\" not in native:
        return native.rstrip("/") or "/"
    p = os.path.abspath(native).replace("\\", "/")
    if p.startswith("//"):
        return p.rstrip("/")
    if len(p) >= 2 and p[1] == ":":
        rest = p[2:].rstrip("/")
        return "/" + p[0].upper() + ":" + rest
    return p


def local_path(path: str) -> str:
    """Accept a local path in either form and return the internal form."""
    if not path:
        return path
    if IS_WINDOWS:
        return to_internal(path)
    return os.path.expanduser(path)


def is_unc(path: str) -> bool:
    return path.startswith("//") and len(path) > 2


def unc_root(path: str) -> str | None:
    """``//server/share`` for a UNC path, else None."""
    if not is_unc(path):
        return None
    parts = path[2:].split("/")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return "//" + parts[0] + "/" + parts[1]


def parent_dir(path: str) -> str:
    """Parent of an internal path.  The parent of a share (``//server/share``)
    is the drive list: Windows cannot list a server's shares as a folder."""
    import posixpath
    if is_unc(path):
        root = unc_root(path)
        if root is None or path.rstrip("/") == root:
            return "/"
        return posixpath.dirname(path.rstrip("/"))
    return posixpath.dirname(path.rstrip("/")) or "/"


def safe_cwd() -> str | None:
    """A local working directory for child processes: cmd.exe refuses UNC
    working directories (the app may have been started from a share)."""
    if not IS_WINDOWS:
        return None
    return os.environ.get("SystemRoot") or os.path.expanduser("~")


def home_dir() -> str:
    return to_internal(os.path.expanduser("~"))


_MAC_KEYS = {"Ctrl": "\u2318", "Alt": "\u2325", "Shift": "\u21e7"}     # ⌘ ⌥ ⇧
_MAC_ORDER = "\u2303\u2325\u21e7\u2318"                                 # ⌃ ⌥ ⇧ ⌘, Apple's order


def _mac_shortcut(match) -> str:
    *mods, key = match.group(0).split("+")
    return "".join(sorted((_MAC_KEYS[m] for m in mods), key=_MAC_ORDER.index)) + key


def shortcut_label(text: str) -> str:
    """Shortcuts in a text the way this platform writes them: on macOS,
    where ⌘ does what Ctrl does elsewhere, "Ctrl+Shift+C" is "⇧⌘C"."""
    if not IS_MACOS:
        return text
    import re
    return re.sub(r"\b(?:(?:Ctrl|Alt|Shift)\+)+[^\s+/),]+", _mac_shortcut, text)


def tilde(path: str) -> str:
    """A local path for display, with the home folder shown as "~"."""
    home = home_dir().rstrip("/")
    if home and (path == home or path.startswith(home + "/")):
        return "~" + path[len(home):]
    return path


def list_drives() -> list[str]:
    """Drive roots on Windows, e.g. ``["C:", "D:"]``."""
    if not IS_WINDOWS:
        return []
    try:
        drives = os.listdrives()            # Python ≥ 3.12
        return sorted({d[:2].upper() for d in drives})
    except (AttributeError, OSError):
        return [f"{c}:" for c in string.ascii_uppercase if os.path.exists(f"{c}:\\")]


def is_hidden_native(native: str, st) -> bool:
    """Windows "hidden" attribute (dot files are handled by name)."""
    if not IS_WINDOWS:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & 0x2)                 # FILE_ATTRIBUTE_HIDDEN


def open_external_terminal(host: str, port: int, user: str, key_file: str = "") -> str | None:
    """Open the system terminal running ``ssh``.  Returns an error message
    or ``None`` on success."""
    import shutil
    import subprocess
    target = f"{user}@{host}" if user else host
    ssh_args = ["ssh", "-p", str(port)]
    if key_file:
        ssh_args += ["-i", os.path.expanduser(key_file)]
    ssh_args.append(target)
    try:
        if IS_WINDOWS:
            if not shutil.which("ssh"):
                return "The OpenSSH client is not installed (Settings → Apps → Optional features → OpenSSH Client)."
            if shutil.which("wt"):
                subprocess.Popen(["wt", "new-tab", "--title", target] + ssh_args, env=host_env(),
                                 cwd=os.path.expanduser("~"))
            else:
                subprocess.Popen(["cmd.exe", "/c", "start", target] + ssh_args, env=host_env(),
                                 cwd=os.path.expanduser("~"))
            return None
        if not shutil.which("ssh"):
            return "The OpenSSH client (ssh) is not installed."
        if IS_MACOS:
            # Terminal runs a .command file it is given; the script removes
            # itself before it becomes ssh.
            import shlex
            import tempfile
            fd, script = tempfile.mkstemp(prefix="anySCP-ssh-", suffix=".command")
            with os.fdopen(fd, "w") as fh:
                fh.write('#!/bin/sh\nrm -f "$0"\nexec ' + " ".join(shlex.quote(a) for a in ssh_args) + "\n")
            os.chmod(script, 0o700)
            subprocess.Popen(["open", "-a", "Terminal", script], env=host_env())
            return None
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"), ("kgx", "--"),
                           ("ptyxis", "--"), ("konsole", "-e"), ("xfce4-terminal", "-x"),
                           ("alacritty", "-e"), ("kitty", ""), ("foot", ""), ("xterm", "-e")):
            if shutil.which(term):
                subprocess.Popen([term] + ([flag] if flag else []) + ssh_args, env=host_env(),
                                 start_new_session=True)
                return None
        return "No terminal emulator was found."
    except OSError as exc:
        return str(exc)


# --------------------------------------------------------------------------
# Child processes of a bundled build (AppImage / Windows .exe)
# --------------------------------------------------------------------------
#
# The launchers point PATH, PYTHONHOME, GI_TYPELIB_PATH, XDG_DATA_DIRS,
# GDK_PIXBUF_MODULE_FILE, … at the bundled runtime.  Programs we start (the
# default app for a file, a terminal) must not inherit that, or a host GTK
# app would load our libraries.  The launchers save every value they change
# as ANYSCP_ORIG_<NAME> and list previously-unset names in ANYSCP_UNSET_VARS.

def is_bundled() -> bool:
    return "ANYSCP_UNSET_VARS" in os.environ or any(k.startswith("ANYSCP_ORIG_") for k in os.environ)


def host_env() -> dict:
    """The environment as it was before the bundle's launcher changed it."""
    env = dict(os.environ)
    if not is_bundled():
        return env
    for key, value in list(env.items()):
        if key.startswith("ANYSCP_ORIG_"):
            env[key[len("ANYSCP_ORIG_"):]] = value
            del env[key]
    for name in env.pop("ANYSCP_UNSET_VARS", "").replace(";", ":").split(":"):
        if name:
            env.pop(name, None)
    for key in ("ANYSCP_LAUNCHER_DIR", "APPDIR", "APPIMAGE", "ARGV0", "OWD"):
        env.pop(key, None)
    return env


def open_path(native: str, reveal: bool = False) -> bool:
    """Open a file with its default application (or show it in the file
    manager).  Returns False when the caller should use Gtk.FileLauncher
    instead (unbundled Linux, where GTK handles it best)."""
    import shutil
    import subprocess
    if IS_WINDOWS:
        if reveal:
            subprocess.Popen(["explorer", "/select," + native], env=host_env(), cwd=safe_cwd())
        else:
            subprocess.Popen(["cmd", "/c", "start", "", native], env=host_env(), cwd=safe_cwd(),
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    if IS_MACOS:
        subprocess.Popen(["open", "-R", native] if reveal else ["open", native], env=host_env())
        return True
    if not is_bundled():
        return False
    target = os.path.dirname(native) if reveal else native
    opener = shutil.which("xdg-open", path=host_env().get("PATH")) or shutil.which("gio")
    if opener is None:
        return False
    args = [opener, target] if opener.endswith("xdg-open") else [opener, "open", target]
    subprocess.Popen(args, env=host_env(), start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
