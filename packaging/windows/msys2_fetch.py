# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Download a Windows runtime from MSYS2 on Linux (no pacman needed).

usage: msys2_fetch.py [--dry-run] <repo> <dest-dir> <cache-dir> <package>...

<repo> is an MSYS2 environment such as ``clangarm64`` (Windows on ARM) or
``clang64`` (x64).  Package names are given without the environment prefix
("gtk4", "python-gobject").  The script reads the repository database,
resolves the dependencies, downloads the packages into <cache-dir> (checking
their SHA-256), and unpacks them into <dest-dir>, which then looks like the
environment's root folder: bin\\, lib\\, share\\, ...

conda-forge, the source of the other anySCP runtimes, has no GTK for
Windows on ARM; MSYS2 does.  <dest-dir>/msys2-manifest.json lists what was
installed and each package's license files (for the license overview), and
<dest-dir>/msys2-files.json each package's files.

Unlike pacman, this runs no install hooks: the caches they would make (GLib
schemas, gdk-pixbuf loaders, icons, fonts) are made by the build instead.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

MIRRORS = ("https://repo.msys2.org/mingw", "https://mirror.msys2.org/mingw")
PREFIXES = {
    "clangarm64": "mingw-w64-clang-aarch64-",
    "clang64": "mingw-w64-clang-x86_64-",
    "ucrt64": "mingw-w64-ucrt-x86_64-",
}
USER_AGENT = "anyscp-build"
LICENSE_FILE = re.compile(
    r"^(licen[cs]e|copying|copyright|notice|unlicense)([._-][^/]*)?$"     # LICENSE, COPYING.LIB, NOTICE.txt
    r"|^[^/]*[._-](licen[cs]e|copying)(\.(txt|md|rst))?$"               # FTL.license, foo-LICENSE.txt
    r"|^license\.terms$|^runtime\.library\.exception[^/]*$", re.I)
NOT_LICENSE = (".py", ".pyc", ".pyi", ".h", ".hpp", ".c", ".html", ".htm", ".svg", ".png", ".dll", ".exe",
               ".pyd", ".a", ".typelib", ".gir", ".mo", ".so", ".js", ".css", ".xml")


def is_license_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return bool(LICENSE_FILE.match(name)) and not name.lower().endswith(NOT_LICENSE)


def fetch(url: str, dest: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    os.replace(tmp, dest)


def fetch_any(repo: str, name: str, dest: str) -> None:
    last = None
    for attempt in range(3):
        for mirror in MIRRORS:
            try:
                fetch(f"{mirror}/{repo}/{name}", dest)
                return
            except OSError as exc:      # URLError, timeouts, connection resets
                last = exc
    raise SystemExit(f"could not download {name}: {last}")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_db(repo: str, cache: str) -> dict[str, dict]:
    """Package name -> its ``desc`` fields (lists of lines)."""
    db = os.path.join(cache, f"{repo}.db")
    fetch_any(repo, f"{repo}.db", db)
    packages = {}
    with tempfile.TemporaryDirectory(dir=cache) as tmp:
        subprocess.run(["tar", "-xf", db, "-C", tmp], check=True)   # zstd or gzip
        for entry in os.listdir(tmp):
            desc = os.path.join(tmp, entry, "desc")
            if not os.path.isfile(desc):
                continue
            fields, key = {}, None
            for line in open(desc, encoding="utf-8").read().splitlines():
                if line.startswith("%") and line.endswith("%"):
                    key = line.strip("%")
                    fields[key] = []
                elif line and key:
                    fields[key].append(line)
            packages[fields["NAME"][0]] = fields
    return packages


def resolve(packages: dict[str, dict], wanted: list[str]) -> list[str]:
    provides = {}
    for name, fields in packages.items():
        for p in fields.get("PROVIDES", []):
            provides.setdefault(re.split(r"[<>=]", p)[0], name)
    done, todo = [], list(wanted)
    while todo:
        name = todo.pop()
        if name not in packages:
            if name not in provides:
                raise SystemExit(f"MSYS2 has no package (or provider) named {name}")
            name = provides[name]
        if name in done:
            continue
        done.append(name)
        todo.extend(re.split(r"[<>=]", d)[0] for d in packages[name].get("DEPENDS", []))
    return sorted(done)


def main() -> None:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]
    if len(args) < 4 or args[0] not in PREFIXES:
        raise SystemExit(__doc__)
    repo, dest, cache = args[0], os.path.abspath(args[1]), os.path.abspath(args[2])
    prefix = PREFIXES[repo]
    cache = os.path.join(cache, repo)
    os.makedirs(cache, exist_ok=True)

    packages = read_db(repo, cache)
    names = resolve(packages, [prefix + p for p in args[3:]])
    total = sum(int(packages[n]["CSIZE"][0]) for n in names)
    print(f"    {repo}: {len(names)} packages, {total / 1e6:.0f} MB to download at most")
    if dry:
        for n in names:
            f = packages[n]
            print(f"      {n[len(prefix):]:<34} {f['VERSION'][0]:<22} {int(f['ISIZE'][0]) / 1e6:7.1f} MB")
        return

    def get(name: str) -> str:
        fields = packages[name]
        path = os.path.join(cache, fields["FILENAME"][0])
        want = fields["SHA256SUM"][0]
        if not (os.path.isfile(path) and sha256(path) == want):
            fetch_any(repo, fields["FILENAME"][0], path)
            if sha256(path) != want:
                os.unlink(path)
                raise SystemExit(f"checksum mismatch for {fields['FILENAME'][0]}")
        return path

    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        files = dict(zip(names, pool.map(get, names)))

    # Unpack next to the destination and swap, so an interrupted run leaves
    # either the old runtime or none.
    staging = dest + ".partial"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    manifest, all_files = [], {}
    for name in names:
        # Every package holds one top-level folder named after the repo, plus
        # pacman's own dot-files, which --strip-components drops.
        subprocess.run(["tar", "-xf", files[name], "-C", staging, "--strip-components=1",
                        "--no-same-owner", "--no-same-permissions"], check=True)
        listing = subprocess.run(["tar", "-tf", files[name]], check=True, capture_output=True,
                                 text=True).stdout.splitlines()
        files_here = sorted(rel for rel in (line.split("/", 1)[1] for line in listing if "/" in line)
                            if rel and not rel.endswith("/"))
        all_files[name[len(prefix):]] = files_here
        license_files = [rel for rel in files_here if is_license_file(rel) or "/licenses/" in f"/{rel}"]
        f = packages[name]
        manifest.append({
            "name": name[len(prefix):], "package": name, "version": f["VERSION"][0],
            "license": " AND ".join(f.get("LICENSE", [])) or "unknown",
            "homepage": (f.get("URL") or [""])[0],
            "recipe": f"https://packages.msys2.org/base/{(f.get('BASE') or [name])[0]}",
            "url": f"{MIRRORS[0]}/{repo}/{f['FILENAME'][0]}", "sha256": f["SHA256SUM"][0],
            "license_files": license_files,
        })
    with open(os.path.join(staging, "msys2-manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"repo": repo, "packages": manifest}, fh, indent=1)
    with open(os.path.join(staging, "msys2-files.json"), "w", encoding="utf-8") as fh:
        json.dump(all_files, fh)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(staging, dest)
    print(f"    {repo}: unpacked into {dest}")


if __name__ == "__main__":
    main()
