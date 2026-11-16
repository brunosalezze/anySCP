# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Apply ``~/.ssh/config`` (Host aliases, HostName, User, Port,
IdentityFile, ProxyCommand, ProxyJump) to an SSH site."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".ssh" / "config"


@dataclass
class ResolvedHost:
    hostname: str
    port: int
    user: str
    identity_files: list[str] = field(default_factory=list)
    proxy_command: str | None = None
    jump_hosts: list[str] = field(default_factory=list)     # ProxyJump: [user@]host[:port], in order


def resolve(site: dict, config_path: Path | None = None) -> ResolvedHost:
    """Merge ``site`` with the matching ``~/.ssh/config`` entry.  Values set
    explicitly in the site win; the config fills in the rest."""
    host = (site.get("host") or "").strip()
    port = int(site.get("port") or 22)
    user = (site.get("user") or "").strip()
    resolved = ResolvedHost(hostname=host, port=port, user=user)
    path = config_path or CONFIG_PATH
    if not host or not path.exists():
        return resolved
    try:
        import paramiko
        conf = paramiko.SSHConfig.from_path(str(path)).lookup(host)
    except Exception:  # noqa: BLE001 - a broken config must not block connecting
        return resolved

    resolved.hostname = conf.get("hostname") or host
    if port == 22 and conf.get("port"):
        try:
            resolved.port = int(conf["port"])
        except ValueError:
            pass
    if not user and conf.get("user"):
        resolved.user = conf["user"]
    resolved.identity_files = [os.path.expanduser(p) for p in conf.get("identityfile", [])
                               if os.path.exists(os.path.expanduser(p))]

    proxy_command = conf.get("proxycommand")
    proxy_jump = conf.get("proxyjump")
    if proxy_command and proxy_command.lower() != "none":
        resolved.proxy_command = proxy_command
    elif proxy_jump and proxy_jump.lower() != "none":
        resolved.jump_hosts = [h.strip() for h in proxy_jump.split(",") if h.strip()]
    return resolved


def parse_host_spec(spec: str) -> tuple[str, str, int]:
    """``[ssh://][user@]host[:port]`` (IPv6 addresses in brackets) ->
    (user, host, port); missing parts are "" and 0."""
    spec = spec.strip()
    if spec.lower().startswith("ssh://"):
        spec = spec[6:].rstrip("/")
    user = ""
    if "@" in spec:
        user, spec = spec.rsplit("@", 1)
    port = 0
    if spec.startswith("["):
        host, _sep, rest = spec[1:].partition("]")
        if rest.startswith(":") and rest[1:].isdigit():
            port = int(rest[1:])
    elif spec.count(":") == 1:
        host, _sep, tail = spec.partition(":")
        if tail.isdigit():
            port = int(tail)
        else:
            host = spec
    else:
        host = spec
    return user.strip(), host.strip(), port
