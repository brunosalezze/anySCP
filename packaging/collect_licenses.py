# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Collect the licenses of every third-party component bundled in a package.

usage: collect_licenses.py <output-dir> <anySCP repo root> <source>...

Sources:
  conda:<env>                  every package of a conda environment
  conda:<env>:<name>,<name>    only these packages of it
  msys2:<env>[:<runtime>]      the MSYS2 packages listed in <env>/msys2-manifest.json
                               (with a runtime folder: only those with files in it)
  extras:<dir>                 components built from source (<dir>/anyscp-extras.json)

Writes <output-dir>/THIRD-PARTY-LICENSES.txt (component, version, license,
homepage, where to get the source) and copies each component's license files
to <output-dir>/licenses/<component>/, plus anySCP's own LICENSE.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys

LICENSE_FILE = re.compile(
    r"^(licen[cs]e|copying|copyright|notice|unlicense)([._-][^/]*)?$"     # LICENSE, COPYING.LIB, NOTICE.txt
    r"|^[^/]*[._-](licen[cs]e|copying)(\.(txt|md|rst))?$"               # FTL.license, foo-LICENSE.txt
    r"|^license\.terms$|^runtime\.library\.exception[^/]*$", re.I)
NOT_LICENSE = (".py", ".pyc", ".pyi", ".h", ".hpp", ".c", ".html", ".htm", ".svg", ".png", ".dll", ".exe",
               ".pyd", ".a", ".typelib", ".gir", ".mo", ".so", ".js", ".css", ".xml")


def is_license_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return bool(LICENSE_FILE.match(name)) and not name.lower().endswith(NOT_LICENSE)

out, repo = sys.argv[1], sys.argv[2]
sources = sys.argv[3:]
pkgs_root = os.path.join(repo, "build", "mamba", "pkgs")
lic_root = os.path.join(out, "licenses")
os.makedirs(lic_root, exist_ok=True)
shutil.copy(os.path.join(repo, "LICENSE"), os.path.join(lic_root, "anySCP-LICENSE.txt"))

rows = []           # (name, version, license, homepage, recipe, package url)
missing = []
origins = set()


def copy_license_files(name, paths):
    dest = os.path.join(lic_root, name)
    copied = 0
    for path in paths:
        if os.path.isdir(path):
            shutil.copytree(path, dest, dirs_exist_ok=True)
            copied += 1
        elif os.path.isfile(path):
            os.makedirs(dest, exist_ok=True)
            shutil.copy(path, os.path.join(dest, os.path.basename(path)))
            copied += 1
    if not copied:
        missing.append(name)


def conda_package_dir(meta: dict) -> str | None:
    """The extracted package in the cache (paths in conda-meta are absolute
    and go stale when the project folder is renamed or moved)."""
    path = meta.get("extracted_package_dir") or ""
    marker = os.sep + os.path.join("build", "mamba", "pkgs") + os.sep
    if marker in path:
        path = os.path.join(pkgs_root, path.split(marker, 1)[1])
    return path if os.path.isdir(path) else None


def add_conda(env, only=None):
    origins.add("conda")
    for meta_file in sorted(glob.glob(os.path.join(env, "conda-meta", "*.json"))):
        meta = json.load(open(meta_file, encoding="utf-8"))
        name, version = meta["name"], meta["version"]
        if only is not None and name not in only:
            continue
        about, feedstock = {}, name
        pdir = conda_package_dir(meta)
        if pdir:
            about_path = os.path.join(pdir, "info", "about.json")
            if os.path.exists(about_path):
                about = json.load(open(about_path, encoding="utf-8"))
                feedstock = about.get("extra", {}).get("feedstock-name") or name
                feedstock = feedstock.removesuffix("-feedstock")
        paths = [os.path.join(pdir, "info", "licenses")] if pdir else []
        if pdir and not os.path.isdir(paths[0]):
            # Some packages (the GCC runtime libraries) install their license
            # files among their own files instead.
            listing = os.path.join(pdir, "info", "paths.json")
            if os.path.exists(listing):
                paths = [os.path.join(env, entry["_path"])
                         for entry in json.load(open(listing, encoding="utf-8"))["paths"]
                         if is_license_file(entry["_path"])]
        copy_license_files(name, paths)
        rows.append((name, version, meta.get("license") or about.get("license") or "unknown",
                     about.get("home") or about.get("dev_url") or "",
                     f"https://github.com/conda-forge/{feedstock}-feedstock", meta.get("url", "")))


# MSYS2 packages that ship no license file: the same project's conda-forge
# package (in the package cache, any platform) has the texts.
CONDA_NAMES = {"python-gobject": "pygobject", "gobject-introspection-runtime": "gobject-introspection"}
# ... or, for a plain GNU license, its standard text (from a package that has it)
STANDARD_TEXTS = [
    ("LGPL-2.1", ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 2.1")),
    ("LGPL-2.0", ("GNU LIBRARY GENERAL PUBLIC LICENSE", "Version 2")),
    ("LGPL-3.0", ("GNU LESSER GENERAL PUBLIC LICENSE", "Version 3")),
    ("GPL-2.0", ("GNU GENERAL PUBLIC LICENSE", "Version 2")),
    ("GPL-3.0", ("GNU GENERAL PUBLIC LICENSE", "Version 3")),
]
substituted = []


def conda_cached_licenses(name):
    for path in sorted(glob.glob(os.path.join(pkgs_root, "*", "*", "*", "*", f"{name}-[0-9]*", "info", "licenses"))):
        if os.path.isdir(path):
            return path
    return None


def standard_text(env, spdx):
    for prefix, (title, version) in STANDARD_TEXTS:
        if spdx.startswith(prefix):
            for path in sorted(glob.glob(os.path.join(env, "share", "licenses", "*", "*"))):
                try:
                    head = open(path, encoding="utf-8", errors="replace").read(400)
                except OSError:
                    continue
                if title in head and version in head and head.lstrip().startswith(title):
                    return path, prefix
    return None, None


def add_msys2(env, runtime=None):
    origins.add("msys2")
    manifest = json.load(open(os.path.join(env, "msys2-manifest.json"), encoding="utf-8"))
    files = json.load(open(os.path.join(env, "msys2-files.json"), encoding="utf-8")) if runtime else {}
    for pkg in manifest["packages"]:
        name = pkg["name"]
        if runtime and not any(os.path.exists(os.path.join(runtime, f)) for f in files.get(name, [])
                               if not f.startswith("share/licenses/")):
            continue                    # left out of the package entirely
        paths = [os.path.join(env, "share", "licenses", name)]
        paths += [os.path.join(env, p) for p in pkg.get("license_files", [])
                  if not p.startswith(f"share/licenses/{name}/")]
        if not any(os.path.exists(p) for p in paths):
            spdx = pkg["license"].removeprefix("spdx:")
            other = conda_cached_licenses(CONDA_NAMES.get(name, name))
            if other:
                paths = [other]
                substituted.append(f"{name}: from conda-forge's {CONDA_NAMES.get(name, name)} package")
            else:
                text, kind = standard_text(env, spdx)
                if text:
                    dest = os.path.join(lic_root, name)
                    os.makedirs(dest, exist_ok=True)
                    shutil.copy(text, os.path.join(dest, f"{kind}.txt"))
                    paths = []
                    substituted.append(f"{name}: the standard {kind} text")
        if paths:
            copy_license_files(name, paths)
        rows.append((name, pkg["version"], pkg["license"].removeprefix("spdx:"), pkg["homepage"],
                     pkg["recipe"], pkg["url"]))


def add_extras(folder):
    origins.add("extras")
    for item in json.load(open(os.path.join(folder, "anyscp-extras.json"), encoding="utf-8")):
        copy_license_files(item["name"], [os.path.join(folder, item["license_dir"])])
        rows.append((item["name"], item["version"], item["license"], item["homepage"],
                     item["recipe"], item["url"]))


for source in sources:
    kind, _, rest = source.partition(":")
    if kind == "conda":
        env, _, names = rest.partition(":")
        add_conda(env, set(names.split(",")) if names else None)
    elif kind == "msys2":
        env, _, runtime = rest.partition(":")
        add_msys2(env, runtime or None)
    elif kind == "extras":
        add_extras(rest)
    else:
        sys.exit(f"unknown source {source!r}")

names = {r[0] for r in rows}
where = []
if "conda" in origins:
    where.append(" * conda-forge (https://conda-forge.org) builds its components from the\n"
                 "   upstream release named in its recipe (\"feedstock\", linked below); the\n"
                 "   exact conda package used is given by the package URL.")
if "msys2" in origins:
    where.append(" * MSYS2 (https://www.msys2.org) builds its components from the upstream\n"
                 "   release named in its recipe (linked below); the exact package used is\n"
                 "   given by the package URL.")
if "extras" in origins:
    where.append(" * Components marked \"built from source\" were compiled from the unmodified\n"
                 "   upstream release at the given URL by the script named, which is part of\n"
                 "   anySCP's source code.")
notes = [" * FreeType is used under the FreeType License (FTL), one of its two options."]
if substituted:
    notes.append(" * These packages ship no license file of their own; the license texts in\n"
                 "   their folders are:\n" + "\n".join(f"     - {s}" for s in substituted))
if names & {"nettle", "gmp", "libgmp"}:
    notes.append(" * GNU nettle and GMP are used under the LGPL-3.0-or-later option.")
if names & {"ucrt", "vc14_runtime", "vc"}:
    notes.append(" * The Microsoft Visual C++ runtime and Universal CRT DLLs in the Windows\n"
                 "   build are redistributable files under Microsoft's license terms.")

with open(os.path.join(out, "THIRD-PARTY-LICENSES.txt"), "w", encoding="utf-8") as fh:
    fh.write("""anySCP – third-party components
================================

anySCP itself is free software under the GNU General Public License,
version 3 or (at your option) any later version; see licenses/anySCP-LICENSE.txt.
Its source code is included in this package (the "anyscp" Python package).

This package also contains the unmodified open source components listed
below.  Each component's license texts are in licenses/<component>/.

Where they come from, and where to get their source code:
""" + "\n".join(where) + """

The corresponding source code of the GPL/LGPL components can be obtained
from those recipes and upstream projects.

Notes on specific licenses:
""" + "\n".join(notes) + "\n\n")
    width = max(len(r[0]) for r in rows)
    for name, version, lic, home, recipe, url in rows:
        fh.write(f"{name:<{width}}  {version:<14} {lic}\n")
        if home:
            fh.write(f"{'':<{width}}  homepage:  {home}\n")
        fh.write(f"{'':<{width}}  recipe:    {recipe}\n")
        if url:
            fh.write(f"{'':<{width}}  {'source:   ' if recipe.startswith('built from source') else 'package:  '} {url}\n")
        fh.write("\n")

print(f"    licenses: {len(rows)} components"
      + (f" ({len(missing)} without license files: {', '.join(missing[:6])}{'…' if len(missing) > 6 else ''})"
         if missing else ""))

# These packages have no license file of their own (parts split off another
# package, or public domain); every other component must have its texts.  On
# CI a missing one fails the build, so no package ships without them unnoticed.
NO_FILES_EXPECTED = {"libfreetype", "libfreetype6", "libsqlite", "libgcc-ng"}
unexpected = [name for name in missing if name not in NO_FILES_EXPECTED]
if unexpected and os.environ.get("ANYSCP_CI"):
    sys.exit(f"ERROR: no license files found for {len(unexpected)} components: {', '.join(unexpected)}")
