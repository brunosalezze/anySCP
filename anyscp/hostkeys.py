# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""SSH host key cache and trusted TLS certificates."""

from __future__ import annotations

import base64
import hashlib
import ssl
import threading
from pathlib import Path

from .config import KNOWN_HOSTS_FILE, TRUSTED_CERTS_FILE, atomic_write_text, load_json, save_json

_lock = threading.Lock()


def sha256_fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def md5_fingerprint(blob: bytes) -> str:
    digest = hashlib.md5(blob).hexdigest()  # noqa: S324 - display only
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def host_entry_name(host: str, port: int) -> str:
    return host if int(port) == 22 else f"[{host}]:{port}"


# --------------------------------------------------------------------------
# SSH
# --------------------------------------------------------------------------

def check_host_key(host: str, port: int, key) -> str:
    """Return ``"ok"``, ``"unknown"`` or ``"changed"`` for a paramiko key.

    An exact match in *any* file wins, so a key the user accepted in anySCP
    ("Update Key") is honoured even if ~/.ssh/known_hosts still lists the
    old one."""
    import paramiko

    name = host_entry_name(host, port)
    changed = False
    for path in (KNOWN_HOSTS_FILE, Path.home() / ".ssh" / "known_hosts"):
        if not path.exists():
            continue
        hk = paramiko.HostKeys()
        try:
            hk.load(str(path))
        except (OSError, paramiko.SSHException, ValueError):
            continue
        entry = hk.lookup(name)
        if entry is None or key.get_name() not in entry:
            # A known host offering a key type we have not seen is an
            # unknown key, not a mismatch (OpenSSH treats it the same way).
            continue
        if entry[key.get_name()].asbytes() == key.asbytes():
            return "ok"
        changed = True
    return "changed" if changed else "unknown"


def save_host_key(host: str, port: int, key) -> None:
    """Remember ``key`` in anySCP's own known_hosts (replacing stale keys of
    the same type for that host)."""
    name = host_entry_name(host, port)
    line = f"{name} {key.get_name()} {key.get_base64()}\n"
    with _lock:
        KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lines = []
        if KNOWN_HOSTS_FILE.exists():
            for existing in KNOWN_HOSTS_FILE.read_text().splitlines(keepends=True):
                fields = existing.split()
                if len(fields) >= 2 and fields[0] == name and fields[1] == key.get_name():
                    continue
                lines.append(existing)
        lines.append(line)
        atomic_write_text(KNOWN_HOSTS_FILE, "".join(lines))


# --------------------------------------------------------------------------
# TLS (FTPS, WebDAV over HTTPS)
# --------------------------------------------------------------------------

def cert_fingerprint(der: bytes) -> str:
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def is_cert_trusted(host: str, port: int, fingerprint: str) -> bool:
    data = load_json(TRUSTED_CERTS_FILE, {})
    return fingerprint in data.get(f"{host}:{port}", [])


def trust_cert(host: str, port: int, fingerprint: str) -> None:
    with _lock:
        data = load_json(TRUSTED_CERTS_FILE, {})
        fps = data.setdefault(f"{host}:{port}", [])
        if fingerprint not in fps:
            fps.append(fingerprint)
        save_json(TRUSTED_CERTS_FILE, data)


def describe_cert(der: bytes) -> tuple[str, str]:
    """Best effort (subject, issuer) strings for a DER certificate."""
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(der)
        return cert.subject.rfc4514_string(), cert.issuer.rfc4514_string()
    except Exception:  # noqa: BLE001
        return "", ""


def verify_tls_peer(sock: ssl.SSLSocket, host: str, port: int, prompter,
                    session_trust: set | None = None) -> None:
    """Called right after a TLS handshake done with ``CERT_NONE``.

    Re-validates the peer certificate against the system trust store; if it
    is not valid and not explicitly trusted, asks ``prompter``.  Raises
    :class:`~anyscp.backends.base.AuthFailed` if the user rejects it.
    ``session_trust`` is a set shared by clones of one session for
    certificates accepted "once".
    """
    from .backends.base import AuthFailed

    der = sock.getpeercert(binary_form=True)
    if not der:
        raise AuthFailed("Server did not present a TLS certificate")
    error = _verify_chain(sock, host)
    if error is None:
        return
    fp = cert_fingerprint(der)
    if is_cert_trusted(host, port, fp) or (session_trust is not None and fp in session_trust):
        return
    subject, issuer = describe_cert(der)
    answer = prompter.verify_certificate(host, port, fp, subject, issuer, error)
    if answer == "save":
        trust_cert(host, port, fp)
    elif answer == "once":
        if session_trust is not None:
            session_trust.add(fp)
    else:
        raise AuthFailed("Server certificate was rejected")


def _system_verify(der: bytes, host: str) -> str | None:
    """Verify a leaf certificate against the default CA store and hostname.

    Returns ``None`` when valid or an error string.  Intermediate chains the
    server sent are not available here, so this is used only as a fallback;
    backends should prefer doing a verified handshake first (see
    :func:`tls_connect_with_fallback`)."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID
        import datetime
        cert = x509.load_der_x509_certificate(der)
        now = datetime.datetime.now(datetime.timezone.utc)
        if cert.not_valid_after_utc < now:
            return "The certificate has expired"
        if cert.not_valid_before_utc > now:
            return "The certificate is not yet valid"
        try:
            names = cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        if cert.issuer == cert.subject:
            return "The certificate is self-signed"
        if names and not any(_host_matches(n, host) for n in names):
            return f"The certificate is for {', '.join(names[:3])}, not {host}"
    except Exception as exc:  # noqa: BLE001
        return f"Certificate could not be validated: {exc}"
    return "The certificate is not trusted by the system"


def _host_matches(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower(), host.lower()
    if pattern.startswith("*."):
        return host.split(".", 1)[-1] == pattern[2:]
    return pattern == host


def tls_wrap(raw_sock, host: str, port: int, prompter, session_trust: set | None = None,
             server_hostname: str | None = None) -> ssl.SSLSocket:
    """Wrap ``raw_sock`` in TLS and validate the server certificate.

    The handshake is done without verification so that an untrusted
    certificate does not abort it; the presented chain is then validated
    against the system CA store (with hostname check).  If that fails and
    the certificate is not already trusted, ``prompter`` is asked.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    tls = ctx.wrap_socket(raw_sock, server_hostname=server_hostname or host)
    verify_tls_peer(tls, host, port, prompter, session_trust)
    return tls


def _verify_chain(tls: ssl.SSLSocket, host: str) -> str | None:
    """Validate the peer chain of an unverified TLS socket using the system
    CA store.  Returns ``None`` if valid, otherwise an error message."""
    try:
        chain = tls.get_unverified_chain()  # Python 3.13+
    except AttributeError:
        chain = None
    if not chain:
        der = tls.getpeercert(binary_form=True)
        return _system_verify(der, host) if der else "No certificate"
    try:
        from cryptography import x509
        from cryptography.x509.verification import PolicyBuilder, Store
        certs = [x509.load_der_x509_certificate(c) for c in chain]
        store = Store(_system_roots())
        builder = PolicyBuilder().store(store)
        try:
            import ipaddress
            subject = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            subject = x509.DNSName(host)
        verifier = builder.build_server_verifier(subject)
        verifier.verify(certs[0], certs[1:])
        return None
    except ImportError:
        der = tls.getpeercert(binary_form=True)
        return _system_verify(der, host) if der else "No certificate"
    except Exception as exc:  # noqa: BLE001 - VerificationError, ValueError
        msg = str(exc) or exc.__class__.__name__
        return f"The certificate is not trusted: {msg}"


_roots_cache: list | None = None


def _system_roots() -> list:
    """System CA certificates, loaded one by one so a single malformed
    certificate in the bundle cannot invalidate all of them."""
    global _roots_cache
    if _roots_cache is None:
        import warnings
        from cryptography import x509
        paths = ssl.get_default_verify_paths()
        roots = []
        marker = b"-----END CERTIFICATE-----"
        for candidate in (paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt"):
            if not candidate or not Path(candidate).exists():
                continue
            data = Path(candidate).read_bytes()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for block in data.split(marker):
                    start = block.find(b"-----BEGIN CERTIFICATE-----")
                    if start < 0:
                        continue
                    try:
                        roots.append(x509.load_pem_x509_certificate(block[start:] + marker))
                    except Exception:  # noqa: BLE001
                        continue
            if roots:
                break
        _roots_cache = roots
    return _roots_cache
