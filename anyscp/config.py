# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Paths, settings, saved sites and password storage."""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

APP_ID = "io.github.anyscp.AnySCP"
APP_NAME = "anySCP"


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else Path.home() / fallback


def _portable_dir() -> Path | None:
    """Portable mode for the packaged builds (AppImage / Windows .exe).

    The launchers export ANYSCP_LAUNCHER_DIR, the folder containing the
    AppImage or exe.  Settings, saved sites and host keys then live in an
    ``anySCP-data`` folder next to it, which is created on first start.  If
    that place is not writable (read-only medium, Program Files…) the normal
    per-user folders are used instead.  ANYSCP_PORTABLE=0 turns this off."""
    launcher = os.environ.get("ANYSCP_LAUNCHER_DIR")
    if not launcher:
        return None
    candidate = Path(launcher) / "anySCP-data"
    if candidate.is_dir():
        return candidate                      # an existing folder always wins
    if os.environ.get("ANYSCP_PORTABLE", "1").strip().lower() in ("0", "no", "false", "off"):
        return None
    try:
        candidate.mkdir()
        probe = candidate / ".write-test"
        probe.write_text("")
        probe.unlink()
        return candidate
    except OSError:
        try:
            candidate.rmdir()                 # created but not writable
        except OSError:
            pass
        return None


def _default_dirs() -> tuple[Path, Path]:
    portable = _portable_dir()
    if portable is not None:
        return portable / "config", portable / "cache"
    if sys.platform == "win32":
        roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return roaming / "anySCP", local / "anySCP" / "cache"
    return _xdg("XDG_CONFIG_HOME", ".config") / "anyscp", _xdg("XDG_CACHE_HOME", ".cache") / "anyscp"


if os.environ.get("ANYSCP_CONFIG_DIR") and os.environ.get("ANYSCP_CACHE_DIR"):
    _CFG, _CACHE = Path(os.environ["ANYSCP_CONFIG_DIR"]), Path(os.environ["ANYSCP_CACHE_DIR"])
    PORTABLE = False
else:
    _CFG, _CACHE = _default_dirs()
    PORTABLE = _CFG.parent.name == "anySCP-data"
CONFIG_DIR = Path(os.environ.get("ANYSCP_CONFIG_DIR") or _CFG)
CACHE_DIR = Path(os.environ.get("ANYSCP_CACHE_DIR") or _CACHE)

SETTINGS_FILE = CONFIG_DIR / "settings.json"
SITES_FILE = CONFIG_DIR / "sites.json"
KNOWN_HOSTS_FILE = CONFIG_DIR / "known_hosts"
TRUSTED_CERTS_FILE = CONFIG_DIR / "trusted_certs.json"


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------

# Settings may live on a network share (portable mode) used by several
# instances or computers at once.  On Windows, replacing or reading a file
# that another process has open fails with a transient "sharing violation",
# and SMB/NFS add their own short hiccups: retry instead of giving up.
_RETRIES = 12
_RETRY_DELAY = 0.05


def _retrying(fn, *args):
    import time
    for attempt in range(_RETRIES):
        try:
            return fn(*args)
        except PermissionError:
            if attempt == _RETRIES - 1:
                raise
        except OSError as exc:
            # ESTALE/EBUSY/EAGAIN and Windows sharing/lock violations (32/33)
            transient = exc.errno in (11, 16, 116) or getattr(exc, "winerror", None) in (32, 33)
            if not transient or attempt == _RETRIES - 1:
                raise
        time.sleep(_RETRY_DELAY * (attempt + 1))


def _read_text(path: Path) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_json(path: Path, default):
    try:
        text = _retrying(_read_text, path)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except OSError:
        # Unreadable right now (locked, network glitch): use defaults for
        # this session but never touch the file.
        return copy.deepcopy(default)
    try:
        return json.loads(text)
    except ValueError:
        # Really corrupt: keep a backup and start over rather than crash.
        try:
            path.replace(path.with_name(path.name + ".bak"))
        except OSError:
            pass
        return copy.deepcopy(default)


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a unique temporary file and an atomic rename (safe with
    several writers on the same share; retries transient lock errors)."""
    import secrets as _rand
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_rand.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass                      # some network file systems refuse fsync
        _retrying(os.replace, tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------

PROTOCOLS = {
    "sftp": {"label": "SFTP", "port": 22},
    "scp": {"label": "SCP", "port": 22},
    "ftp": {"label": "FTP", "port": 21},
    "webdav": {"label": "WebDAV", "port": 80},
}
PROTOCOL_ORDER = ["sftp", "scp", "ftp", "webdav"]

FTP_ENCRYPTION = ["none", "explicit", "implicit"]
FTP_ENCRYPTION_LABELS = ["No encryption", "TLS/SSL Explicit encryption", "TLS/SSL Implicit encryption"]

SSH_PROTOCOLS = {"sftp", "scp"}


def default_port(site: dict) -> int:
    proto = site.get("protocol", "sftp")
    if proto == "ftp" and site.get("ftp_encryption") == "implicit":
        return 990
    if proto == "webdav" and site.get("tls"):
        return 443
    return PROTOCOLS.get(proto, PROTOCOLS["sftp"])["port"]


SITE_DEFAULTS = {
    "id": "",
    "name": "",
    "folder": "",
    "protocol": "sftp",
    "host": "",
    "port": 22,
    "user": "",
    "save_password": True,
    "auth": "auto",            # auto | password | key | agent
    "key_file": "",
    "remote_dir": "",
    "local_dir": "",
    "compression": False,
    "keepalive": 30,
    "timeout": 15,
    "ftp_encryption": "none",  # none | explicit | implicit
    "ftp_passive": True,
    "tls": False,              # WebDAV over HTTPS
    "color": "",
    "notes": "",
    "bookmarks": [],
    "sync_browsing": False,
    "jump_hosts": "",          # SFTP/SCP: through these SSH servers, in order (saved site names or user@host:port)
    "proxy_type": "none",      # none | socks5 | socks4 | http
    "proxy_host": "",
    "proxy_port": 1080,
    "proxy_user": "",          # (its password: in the password store, under proxy_secret_id())
}


def new_site(**values) -> dict:
    site = copy.deepcopy(SITE_DEFAULTS)
    site.update(values)
    if not site["id"]:
        site["id"] = uuid.uuid4().hex
    return site


def normalize_site(site: dict) -> dict:
    out = copy.deepcopy(SITE_DEFAULTS)
    out.update(site)
    if not out["id"]:
        out["id"] = uuid.uuid4().hex
    try:
        out["port"] = int(out["port"]) or default_port(out)
    except (TypeError, ValueError):
        out["port"] = default_port(out)
    return out


def proxy_secret_id(site_id: str) -> str:
    """Where a site's proxy password is kept in the password store."""
    return f"{site_id}:proxy"


def jump_hops(site: dict, sites: list[dict] | None = None) -> list[dict]:
    """The jump hosts of an SSH site, in order, as sites to log in to.

    ``jump_hosts`` lists, separated by commas, the names of saved sites
    (they log in with their own user, key and remembered password, and bring
    their own jump hosts along) or ``[user@]host[:port]`` (logging in like
    the site itself).  ``sites``: the saved sites (default: sites.json)."""
    from .backends.ssh import hop_site
    if sites is None:
        sites = SiteStore().sites
    by_name = {}
    for saved in sites:
        if saved.get("protocol") in ("sftp", "scp") and saved.get("name"):
            by_name.setdefault(saved["name"].strip().lower(), saved)

    def expand(current: dict, seen: tuple) -> list[dict]:
        hops = []
        for spec in [p.strip() for p in (current.get("jump_hosts") or "").split(",") if p.strip()]:
            saved = by_name.get(spec.lower())
            if saved is None:
                hops.append(hop_site(spec, current))
                continue
            if saved["id"] in seen or len(seen) > 8:
                raise ValueError(f"The jump hosts of “{site_title(current)}” go round in a circle "
                                 f"(through “{saved.get('name')}”)")
            hops += expand(saved, seen + (saved["id"],))
            hop = copy.deepcopy(saved)
            hop["_jump_label"] = saved.get("name")
            if hop.get("save_password") and hop.get("_password") is None:
                hop["_password"] = secrets.lookup(hop["id"])
            hops.append(hop)
        return hops
    return expand(site, (site.get("id"),))


def site_title(site: dict) -> str:
    if site.get("name"):
        return site["name"]
    host = site.get("host", "")
    user = site.get("user", "")
    return f"{user}@{host}" if user else host or "New site"


def site_subtitle(site: dict) -> str:
    proto = PROTOCOLS.get(site.get("protocol"), {}).get("label", "?")
    if site.get("protocol") == "ftp" and site.get("ftp_encryption") != "none":
        proto = "FTPS"
    elif site.get("protocol") == "webdav" and site.get("tls"):
        proto = "WebDAVS"
    target = site.get("host", "")
    if site.get("user"):
        target = f"{site['user']}@{target}"
    if site.get("port") and site.get("port") != default_port(site):
        target += f":{site['port']}"
    return f"{proto} · {target}"


def site_from_url(text: str) -> dict | None:
    """Parse ``[proto://][user[:pass]@]host[:port][/path]`` for Quick Connect."""
    text = text.strip()
    if not text:
        return None
    if "://" not in text:
        text = "sftp://" + text
    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    mapping = {
        "sftp": ("sftp", {}), "ssh": ("sftp", {}), "scp": ("scp", {}),
        "ftp": ("ftp", {"ftp_encryption": "none"}),
        "ftpes": ("ftp", {"ftp_encryption": "explicit"}),
        "ftps": ("ftp", {"ftp_encryption": "implicit"}),
        "dav": ("webdav", {"tls": False}), "http": ("webdav", {"tls": False}),
        "davs": ("webdav", {"tls": True}), "https": ("webdav", {"tls": True}),
    }
    if scheme not in mapping or not parts.hostname:
        return None
    proto, extra = mapping[scheme]
    site = new_site(protocol=proto, host=parts.hostname, **extra)
    site["user"] = unquote(parts.username or "")
    try:
        site["port"] = parts.port or default_port(site)
    except ValueError:
        return None
    if parts.path and parts.path != "/":
        site["remote_dir"] = unquote(parts.path)
    if parts.password:
        site["_password"] = unquote(parts.password)
    site["save_password"] = False
    return site


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "color_scheme": "system",        # system | light | dark
    "show_hidden": False,
    "dirs_first": True,
    "confirm_delete": True,
    "confirm_overwrite": True,
    "confirm_transfer": True,
    "confirm_close_session": True,
    "parallel_transfers": 3,
    "preserve_mtime": True,
    "preserve_permissions": False,
    "auto_show_queue": True,
    "double_click_remote": "edit",   # edit | open | download
    "editor": "internal",            # internal | external
    "editor_font": "Monospace 11",
    "editor_tab_width": 4,
    "editor_wrap": False,
    "size_format": "human",          # human | bytes
    "date_format": "short",          # short | iso | relative
    "show_fkeys_bar": True,
    "show_local_pane": True,
    "local_bookmarks": [],
    "last_local_dir": "",
    "window_width": 1280,
    "window_height": 800,
    "window_maximized": False,
    "pane_position": 0,
    "columns_local": None,
    "columns_remote": None,
}


class Settings:
    """Small observable settings dictionary persisted as JSON."""

    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self._data = copy.deepcopy(DEFAULT_SETTINGS)
        self._data.update(load_json(path, {}))
        self._observers: list = []

    def __getitem__(self, key):
        return self._data.get(key, DEFAULT_SETTINGS.get(key))

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __setitem__(self, key, value):
        if self._data.get(key) == value:
            return
        self._data[key] = value
        # Merge into what is on disk now, so another instance using the same
        # (portable, possibly network) settings file keeps its own changes.
        on_disk = load_json(self.path, None)
        if isinstance(on_disk, dict):
            on_disk[key] = value
            try:
                save_json(self.path, on_disk)
            except OSError:
                pass
        else:
            self.save()
        for cb in list(self._observers):
            cb(key, value)

    def connect(self, callback) -> None:
        self._observers.append(callback)

    def disconnect(self, callback) -> None:
        if callback in self._observers:
            self._observers.remove(callback)

    def save(self) -> None:
        try:
            save_json(self.path, self._data)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------

class SiteStore:
    def __init__(self, path: Path = SITES_FILE):
        self.path = path
        raw = load_json(path, {"sites": []})
        self.sites: list[dict] = [normalize_site(s) for s in raw.get("sites", [])]
        self._observers: list = []

    def connect(self, callback) -> None:
        self._observers.append(callback)

    def _reload(self) -> None:
        """Pick up changes made meanwhile by another instance (e.g. a
        colleague using the same portable copy on a network share)."""
        raw = load_json(self.path, None)
        if raw is not None:
            self.sites = [normalize_site(s) for s in raw.get("sites", [])]

    def _changed(self) -> None:
        self.save()
        for cb in list(self._observers):
            cb()

    def save(self) -> None:
        clean = [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.sites]
        save_json(self.path, {"version": 1, "sites": clean})

    def get(self, site_id: str) -> dict | None:
        return next((s for s in self.sites if s["id"] == site_id), None)

    def upsert(self, site: dict) -> dict:
        self._reload()
        site = normalize_site(site)
        for i, existing in enumerate(self.sites):
            if existing["id"] == site["id"]:
                self.sites[i] = site
                break
        else:
            self.sites.append(site)
        self._changed()
        return site

    def delete(self, site_id: str) -> None:
        self._reload()
        self.sites = [s for s in self.sites if s["id"] != site_id]
        secrets.clear(site_id)
        secrets.clear(proxy_secret_id(site_id))
        self._changed()

    def sorted(self) -> list[dict]:
        return sorted(self.sites, key=lambda s: (s.get("folder", "").lower(), site_title(s).lower()))

    def update_field(self, site_id: str, key: str, value) -> None:
        self._reload()
        site = self.get(site_id)
        if site is not None and site.get(key) != value:
            site[key] = value
            self._changed()


# --------------------------------------------------------------------------
# Passwords (libsecret / GNOME Keyring)
# --------------------------------------------------------------------------

class _WinCredentials:
    """Windows Credential Manager (generic credentials) through ctypes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
        self.ctypes = ctypes
        self.CREDENTIAL = CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CredWrite = adv.CredWriteW
        self.CredWrite.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.CredWrite.restype = wintypes.BOOL
        self.CredRead = adv.CredReadW
        self.CredRead.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.CredRead.restype = wintypes.BOOL
        self.CredDelete = adv.CredDeleteW
        self.CredDelete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.CredDelete.restype = wintypes.BOOL
        self.CredFree = adv.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]

    @staticmethod
    def _target(site_id):
        return f"anySCP:{site_id}"

    def store(self, site_id, label, password):
        ct = self.ctypes
        blob = password.encode("utf-16-le")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else None
        cred = self.CREDENTIAL()
        cred.Type = 1                                  # CRED_TYPE_GENERIC
        cred.TargetName = self._target(site_id)
        cred.Comment = f"anySCP password for {label}"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte)) if buf is not None else None
        cred.Persist = 2                               # CRED_PERSIST_LOCAL_MACHINE
        cred.UserName = label
        return bool(self.CredWrite(ct.byref(cred), 0))

    def lookup(self, site_id):
        ct = self.ctypes
        pcred = ct.POINTER(self.CREDENTIAL)()
        if not self.CredRead(self._target(site_id), 1, 0, ct.byref(pcred)):
            return None
        try:
            size = pcred.contents.CredentialBlobSize
            data = ct.string_at(pcred.contents.CredentialBlob, size) if size else b""
            return data.decode("utf-16-le")
        finally:
            self.CredFree(pcred)

    def clear(self, site_id):
        self.CredDelete(self._target(site_id), 1, 0)


class _Secrets:
    """Password storage: GNOME Keyring / KWallet via libsecret on Linux,
    Credential Manager on Windows.  Falls back to in-memory storage when
    neither is available so the app keeps working."""

    def __init__(self):
        self._lock = threading.Lock()
        self._memory: dict[str, str] = {}
        self._schema = None
        self._secret = None
        self._win = None
        self.available = False
        self.backend_name = "memory"
        if os.environ.get("ANYSCP_NO_KEYRING"):
            return
        if sys.platform == "win32":
            try:
                self._win = _WinCredentials()
                self.available = True
                self.backend_name = "Windows Credential Manager"
            except (OSError, AttributeError):
                self._win = None
            return
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
            self._secret = Secret
            self._schema = Secret.Schema.new(
                APP_ID + ".Password", Secret.SchemaFlags.NONE,
                {"site-id": Secret.SchemaAttributeType.STRING})
            self.available = True
            self.backend_name = "system keyring"
        except (ImportError, ValueError):
            self.available = False

    def store(self, site_id: str, label: str, password: str) -> bool:
        with self._lock:
            self._memory[site_id] = password
        if not self.available:
            return False
        try:
            if self._win is not None:
                return self._win.store(site_id, label, password)
            return bool(self._secret.password_store_sync(
                self._schema, {"site-id": site_id}, self._secret.COLLECTION_DEFAULT,
                f"anySCP password for {label}", password, None))
        except Exception:  # noqa: BLE001 - GLib.Error, D-Bus and Win32 failures
            return False

    def lookup(self, site_id: str) -> str | None:
        with self._lock:
            if site_id in self._memory:
                return self._memory[site_id]
        if not self.available:
            return None
        try:
            if self._win is not None:
                value = self._win.lookup(site_id)
            else:
                value = self._secret.password_lookup_sync(self._schema, {"site-id": site_id}, None)
        except Exception:  # noqa: BLE001
            return None
        if value is not None:
            with self._lock:
                self._memory[site_id] = value
        return value

    def clear(self, site_id: str) -> None:
        with self._lock:
            self._memory.pop(site_id, None)
        if not self.available:
            return
        try:
            if self._win is not None:
                self._win.clear(site_id)
            else:
                self._secret.password_clear_sync(self._schema, {"site-id": site_id}, None)
        except Exception:  # noqa: BLE001
            pass


secrets = _Secrets()
