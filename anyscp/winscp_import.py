# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Import stored sessions from a WinSCP.ini file.

Supports the INI storage format (``[Sessions\\name]`` sections).  Passwords
obfuscated with WinSCP's default (non master-password) scheme are decoded;
passwords protected by a master password cannot be recovered and are
skipped.
"""

from __future__ import annotations

import configparser
from urllib.parse import unquote

from .backends.proxy import DEFAULT_PORTS as PROXY_PORTS
from .config import JUMP_DEFAULTS, default_port, jump_secret_id, new_jump, new_site, secrets

# TFSProtocol in WinSCP's SessionData.h
FS_PROTOCOLS = {0: "scp", 1: "sftp", 2: "sftp", 5: "ftp", 6: "webdav", 7: "s3"}
# TFtps: ftpsNone, ftpsImplicit, ftpsExplicitSsl, ftpsExplicitTls
FTPS = {0: "none", 1: "implicit", 2: "explicit", 3: "explicit"}

_HEX = "0123456789ABCDEF"
_MAGIC = 0xA3
_FLAG = 0xFF


def decrypt_password(encoded: str, key: str) -> str | None:
    """Reverse WinSCP's simple password obfuscation.

    Ported for interoperability from WinSCP's ``source/core/Security.cpp``
    (``DecryptPassword`` / ``SimpleDecryptNextChar``), Copyright (C) Martin
    Přikryl, licensed under the GNU GPL version 3 – compatible with this
    project's GPL-3.0-or-later license.  The protocol numbers and INI key
    names used below are WinSCP's file format (``SessionData.h``/``.cpp``).
    """
    data = encoded.strip().upper()
    if not data or any(c not in _HEX for c in data):
        return None
    pos = 0

    def next_byte() -> int:
        nonlocal pos
        if pos + 1 >= len(data):
            return 0
        hi, lo = _HEX.index(data[pos]), _HEX.index(data[pos + 1])
        pos += 2
        return ~(((hi << 4) + lo) ^ _MAGIC) & 0xFF

    flag = next_byte()
    if flag == _FLAG:
        version = next_byte()
        if version == 0x00:
            length = next_byte()
        elif version == 0x02:
            length = (next_byte() << 8) + next_byte()
        else:
            return None
    else:
        length = flag
    shift = next_byte()
    pos += shift * 2
    raw = bytes(next_byte() for _ in range(length))
    if flag == _FLAG:
        k = key.encode("utf-8")
        if not raw.startswith(k):
            return None
        raw = raw[len(k):]
    return raw.decode("utf-8", "replace")


def encrypt_password(password: str, key: str, shift: int = 0) -> str:
    """Inverse of :func:`decrypt_password` (used by tests)."""
    def enc(b: int) -> str:
        v = (~b ^ _MAGIC) & 0xFF
        return _HEX[v >> 4] + _HEX[v & 0xF]
    payload = (key + password).encode("utf-8")
    out = enc(_FLAG) + enc(0x00) + enc(len(payload)) + enc(shift) + "".join(enc(0) for _ in range(shift))
    out += "".join(enc(b) for b in payload)
    return out


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "on", "true", "yes")


def _int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_text(text: str) -> tuple[list[dict], list[str]]:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # keep key case
    parser.read_string(text)
    master = any(_bool(parser.get(sec, "UseMasterPassword", fallback=None), False)
                 for sec in parser.sections() if sec.lower() in ("configuration\\security",))
    sites: list[dict] = []
    warnings: list[str] = []
    skipped_s3 = 0
    for section in parser.sections():
        if not section.lower().startswith("sessions\\"):
            continue
        raw_name = unquote(section.split("\\", 1)[1])
        if raw_name.lower() == "default settings":
            continue
        sec = parser[section]
        host = sec.get("HostName", "").strip()
        if not host:
            continue
        proto = FS_PROTOCOLS.get(_int(sec.get("FSProtocol"), 2), "sftp")
        if proto == "s3":
            skipped_s3 += 1
            continue
        folder, _sep, name = raw_name.rpartition("/")
        site = new_site(name=name or host, folder=folder.replace("/", " / "), protocol=proto,
                        host=host, user=sec.get("UserName", "").strip())
        ftps = FTPS.get(_int(sec.get("Ftps"), 0), "none")
        if proto == "ftp":
            site["ftp_encryption"] = ftps
            site["ftp_passive"] = _bool(sec.get("FtpPasvMode"), True)
        elif proto == "webdav":
            site["tls"] = ftps != "none"
        site["port"] = _int(sec.get("PortNumber"), default_port(site))
        site["remote_dir"] = unquote(sec.get("RemoteDirectory", "") or "")
        site["compression"] = _bool(sec.get("Compression"), False)
        site["notes"] = unquote(sec.get("Note", "") or "")
        keyfile = unquote(sec.get("PublicKeyFile", "") or "")
        if keyfile:
            warnings.append(f"{site['name']}: key file “{keyfile}” is a Windows path; "
                            "choose the key again after converting it to OpenSSH format.")
            site["auth"] = "auto"
        color = _int(sec.get("Color"), 0)
        if color:
            # WinSCP stores a Windows COLORREF (0x00BBGGRR)
            r, g, b = color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF
            site["color"] = f"#{r:02x}{g:02x}{b:02x}"
        method = _int(sec.get("ProxyMethod"), 0)
        proxy = {1: "socks4", 2: "socks5", 3: "http"}.get(method)
        if proxy:
            site["proxy_type"] = proxy
            site["proxy_host"] = unquote(sec.get("ProxyHost", "") or "").strip()
            site["proxy_port"] = _int(sec.get("ProxyPort"), PROXY_PORTS[proxy])
            site["proxy_user"] = unquote(sec.get("ProxyUsername", "") or "").strip()
            if site["proxy_user"]:
                warnings.append(f"{site['name']}: enter the proxy password in the site's settings "
                                "(it is not imported).")
        elif method in (4, 5):
            warnings.append(f"{site['name']}: its {'Telnet' if method == 4 else 'local command'} proxy "
                            "is not supported and was left out.")
        if _bool(sec.get("Tunnel"), False):
            tunnel = sec.get("TunnelHostName", "").strip()
            if proto in ("sftp", "scp") and tunnel:
                user = unquote(sec.get("TunnelUserName", "") or "").strip()
                port = _int(sec.get("TunnelPortNumber"), 22)
                hop = new_jump(host=tunnel, port=port, user=user)
                # WinSCP's proxy carries the connection to the tunnel's host
                for key in ("proxy_type", "proxy_host", "proxy_port", "proxy_user"):
                    hop[key] = site.pop(key, JUMP_DEFAULTS[key])
                tunnel_password = sec.get("TunnelPassword")
                if tunnel_password and not master:
                    tunnel_password = decrypt_password(tunnel_password, user + tunnel)
                    if tunnel_password:
                        hop["_password"] = tunnel_password
                site["jumps"] = [hop]
            elif tunnel:
                warnings.append(f"{site['name']}: SSH tunnels work for SFTP and SCP sites only; "
                                "its tunnel was left out.")
        encoded = sec.get("Password")
        if encoded and not master:
            password = decrypt_password(encoded, site["user"] + host)
            if password:
                site["_password"] = password
        sites.append(site)
    if master:
        warnings.append("Passwords are protected by a WinSCP master password and were not imported.")
    if skipped_s3:
        warnings.append(f"{skipped_s3} Amazon S3 site(s) were skipped (S3 is not supported yet).")
    return sites, warnings


def parse_file(path: str) -> tuple[list[dict], list[str]]:
    with open(path, "rb") as fh:
        data = fh.read()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = data.decode(encoding)
            if "[" in text:
                break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Could not decode the file")
    return parse_text(text)


def store_sites(store, sites: list[dict], with_passwords: bool) -> int:
    count = 0
    existing = {(s.get("host"), s.get("user"), s.get("port"), s.get("name")) for s in store.sites}
    for site in sites:
        key = (site.get("host"), site.get("user"), site.get("port"), site.get("name"))
        if key in existing:
            continue
        password = site.pop("_password", None)
        site["save_password"] = bool(with_passwords and password)
        hop_passwords = {}
        for hop in site.get("jumps") or []:
            hop_passwords[hop["id"]] = hop.pop("_password", None)
            hop["save_password"] = bool(with_passwords and hop_passwords[hop["id"]])
        saved = store.upsert(site)
        if with_passwords and password:
            secrets.store(saved["id"], saved["name"], password)
        for hop_id, hop_password in hop_passwords.items():
            if with_passwords and hop_password:
                secrets.store(jump_secret_id(saved["id"], hop_id), f"{saved['name']} (jump host)", hop_password)
        count += 1
    return count
