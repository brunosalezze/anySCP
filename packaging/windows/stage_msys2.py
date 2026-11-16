# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
"""Stage the MSYS2 runtime of the Windows-on-ARM build.

usage: stage_msys2.py <msys2-env> <runtime-dir> <conda-win-64-env>

<msys2-env> was made by msys2_fetch.py.  Copies into <runtime-dir> what
anySCP needs of it: no headers, static or import libraries, documentation,
translations, development tools, Tcl/Tk or GStreamer (GTK's video playback,
unused).  Then keeps only the DLLs something actually loads – Python and its
extension modules, the GObject typelibs, gdk-pixbuf's loaders and whatever
those import – and checks that every DLL they import is there.  Adds
paramiko from the conda-forge (win-64) environment, the version the other
builds have (MSYS2's is a newer major version), and writes gdk-pixbuf's loader
cache, which pacman's install hook would have made.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import struct
import sys

env, rt, conda = (os.path.abspath(a) for a in sys.argv[1:4])
PY = next(d for d in sorted(os.listdir(os.path.join(env, "lib")))
          if re.fullmatch(r"python3\.\d+", d) and os.path.isfile(os.path.join(env, "lib", d, "os.py")))
SITE = f"lib/{PY}/site-packages"

DROP = [  # relative paths; "dir/" = everything below it, otherwise fnmatch ('*' crosses '/')
    "include/", "libexec/", "msys2-*.json", "etc/profile.d/", "etc/pkcs11/",
    "*.a", "*.pdb", "*.pc", "*/__pycache__/*", "*.pyc",
    "lib/pkgconfig/", "lib/cmake/", "lib/terminfo/", "lib/tcl8*", "lib/tk8*", "lib/itcl*", "lib/tdbc*",
    "lib/thread2*", "lib/sqlite3*", "lib/dde1*", "lib/reg1*", "lib/gstreamer-1.0/", "lib/p11-kit/", "lib/pkcs11/",
    "lib/girepository-1.0/GIRepository-2.0.typelib",     # the old libgirepository-1.0 API (PyGObject uses 3.0)
    "lib/*.sh", "lib/*.def", "*.cmake", "lib/*/include/", "lib/engines-3/", "lib/libc++.modules.json",
    "share/libc++/", "share/directx*", "share/graphite2/",
    "lib/girepository-1.0/Gst*", "lib/girepository-1.0/CudaGst*", "lib/girepository-1.0/Nice-*",
    "lib/girepository-1.0/*Demo*",
    f"lib/{PY}/test/", f"lib/{PY}/idlelib/", f"lib/{PY}/tkinter/", f"lib/{PY}/turtledemo/",
    f"lib/{PY}/ensurepip/", f"lib/{PY}/lib2to3/", f"lib/{PY}/config-*", f"lib/{PY}/lib-dynload/_tkinter*",
    f"{SITE}/pip/", f"{SITE}/pip-*", f"{SITE}/setuptools*", f"{SITE}/wheel*", f"{SITE}/_distutils_hack/",
    f"{SITE}/distutils-precedence.pth", f"{SITE}/libxml2*", f"{SITE}/drv_libxml2.py",
    "share/doc/", "share/man/", "share/info/", "share/gtk-doc/", "share/gir-1.0/", "share/locale/",
    "share/aclocal/", "share/pkgconfig/", "share/bash-completion/", "share/zsh/", "share/gettext*",
    "share/vala*", "share/installed-tests/", "share/thumbnailers/", "share/applications/", "share/metainfo/",
    "share/zoneinfo/", "share/terminfo/", "share/tabset/", "share/gdb/", "share/glib-2.0/codegen/",
    "share/glib-2.0/gdb/", "share/glib-2.0/valgrind/", "share/gtk-4.0/valgrind/", "share/licenses/",
    "share/sqlite/", "share/gstreamer-1.0/", "share/gst-*", "share/iso-codes/", "share/xml/iso-codes/",
    "share/dbus-1/", "share/X11/", "share/readline/", "share/tcl*", "share/tk*", "share/p11-kit/",
    "share/gobject-introspection-1.0/", "share/python3*", "share/vulkan/", "share/cmake/", "share/libalpm/",
]
KEEP_EXE = {"python.exe", "pythonw.exe", "gspawn-win64-helper.exe", "gspawn-win64-helper-console.exe"}
SYSTEM_DLLS = {  # imports Windows itself provides (anything api-ms-win-* / ext-ms-* as well)
    "advapi32", "bcrypt", "bcryptprimitives", "cfgmgr32", "comctl32", "comdlg32", "crypt32", "d2d1",
    "d3d11", "d3d12", "d3dcompiler_47", "dbghelp", "dcomp", "dnsapi", "dwmapi", "dwrite", "dxgi", "gdi32",
    "gdiplus", "hid", "imm32", "iphlpapi", "kernel32", "mpr", "msimg32", "msvcrt", "ncrypt", "netapi32",
    "normaliz", "ntdll", "ole32", "oleaut32", "opengl32", "powrprof", "propsys", "psapi", "rpcrt4",
    "secur32", "setupapi", "shell32", "shlwapi", "ucrtbase", "user32", "userenv", "usp10", "uxtheme",
    "version", "windowscodecs", "winhttp", "wininet", "winmm", "winspool.drv", "ws2_32", "wsock32",
    "wtsapi32", "credui", "urlmon", "mfplat", "mf", "mfreadwrite", "ksuser", "avrt", "dsound", "xinput1_4",
    "glu32", "dinput8", "wldap32", "cabinet", "sspicli", "msi", "wevtapi", "authz", "netutils",
    "shcore", "mscms", "wintrust", "dxcore", "d3d9", "msacm32", "oleacc", "uiautomationcore",
}


def dropped(rel: str) -> bool:
    """rel: a file's path, or a folder's path ending in "/"."""
    for pattern in DROP:
        if pattern.endswith("/") and rel.startswith(pattern):
            return True
        if not pattern.endswith("/") and fnmatch.fnmatch(rel, pattern):
            return True
    top, _, rest = rel.partition("/")
    if top == "bin" and rest and "/" not in rest:        # programs: only Python and GLib's helpers
        lower = rest.lower()
        return not (lower.endswith(".dll") or lower in KEEP_EXE)
    return False


# ---------------------------------------------------------------- PE imports

def pe_imports(path: str) -> list[str]:
    """Names of the DLLs a PE file imports, normally or delay-loaded."""
    data = open(path, "rb").read()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError(f"{path}: not a PE file")
    n_sections, opt_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dirs = opt + (112 if magic == 0x20B else 96)
    sections = []
    for i in range(n_sections):
        s = opt + opt_size + 40 * i
        vsize, va, rsize, raw = struct.unpack_from("<IIII", data, s + 8)
        sections.append((va, max(vsize, rsize), raw))

    def offset(rva):
        for va, size, raw in sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"{path}: RVA {rva:#x} outside the sections")

    def cstring(rva):
        o = offset(rva)
        return data[o:data.index(b"\0", o)].decode("ascii", "replace")

    names = []
    imp_rva = struct.unpack_from("<I", data, dirs + 8)[0]               # directory 1: imports
    if imp_rva:
        o = offset(imp_rva)
        while True:
            name_rva = struct.unpack_from("<I", data, o + 12)[0]
            if not name_rva:
                break
            names.append(cstring(name_rva))
            o += 20
    delay_rva = struct.unpack_from("<I", data, dirs + 13 * 8)[0]        # directory 13: delay imports
    if delay_rva:
        o = offset(delay_rva)
        while True:
            name_rva = struct.unpack_from("<I", data, o + 4)[0]
            if not name_rva:
                break
            names.append(cstring(name_rva))
            o += 32
    return names


def typelib_libraries(path: str) -> list[str]:
    data = open(path, "rb").read()
    if not data.startswith(b"GOBJ\nMETADATA\r\n\x1a"):
        raise ValueError(f"{path}: not a typelib")
    o = struct.unpack_from("<I", data, 52)[0]                          # header.shared_library
    return [n for n in data[o:data.index(b"\0", o)].decode().split(",") if n] if o else []


def is_system(name: str) -> bool:
    low = name.lower()
    return low.startswith(("api-ms-win-", "ext-ms-")) or low.removesuffix(".dll") in SYSTEM_DLLS


# ---------------------------------------------------------------- copy
shutil.rmtree(rt, ignore_errors=True)
copied = 0
for dirpath, dirnames, filenames in os.walk(env):
    rel_dir = os.path.relpath(dirpath, env).replace(os.sep, "/")
    rel_dir = "" if rel_dir == "." else rel_dir + "/"
    dirnames[:] = [d for d in dirnames if not dropped(rel_dir + d + "/")]
    for name in filenames:
        rel = rel_dir + name
        if dropped(rel):
            continue
        dest = os.path.join(rt, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(dirpath, name), dest)
        copied += 1

# paramiko of the conda-forge environment (pure Python)
conda_site = next(os.path.join(conda, "lib", d, "site-packages") for d in os.listdir(os.path.join(conda, "lib"))
                  if re.fullmatch(r"python3\.\d+", d))
for name in os.listdir(conda_site):
    if name == "paramiko" or re.fullmatch(r"paramiko-[\d.]+\.dist-info", name):
        shutil.copytree(os.path.join(conda_site, name), os.path.join(rt, SITE, name),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
if not os.path.isdir(os.path.join(rt, SITE, "paramiko")):
    sys.exit(f"paramiko not found in {conda_site}")

# ---------------------------------------------------------------- DLLs
bin_dir = os.path.join(rt, "bin")
available = {n.lower(): n for n in os.listdir(bin_dir) if n.lower().endswith(".dll")}
roots, problems, system_used = [], [], set()
for dirpath, _dirs, files in os.walk(rt):
    for name in files:
        low = name.lower()
        path = os.path.join(dirpath, name)
        if dirpath == bin_dir:
            if low in KEEP_EXE:
                roots.append(path)
        elif low.endswith((".pyd", ".dll", ".exe")):
            roots.append(path)                  # extension modules, pixbuf loaders, modules
typelib_dir = os.path.join(rt, "lib", "girepository-1.0")
for name in sorted(os.listdir(typelib_dir)):
    libs = typelib_libraries(os.path.join(typelib_dir, name))
    absent = [lib for lib in libs if lib.lower() not in available]
    if absent:                                  # (e.g. D-Bus GLib: never installed)
        os.unlink(os.path.join(typelib_dir, name))
        continue
    roots += [os.path.join(bin_dir, available[lib.lower()]) for lib in libs]

needed, todo = set(), list(roots)
while todo:
    path = todo.pop()
    for imp in pe_imports(path):
        low = imp.lower()
        if low in available:
            if low not in needed:
                needed.add(low)
                todo.append(os.path.join(bin_dir, available[low]))
        elif is_system(imp):
            system_used.add(low)
        else:
            problems.append(f"{os.path.relpath(path, rt)} imports {imp}, which is not in the runtime")
removed = 0
for low, name in available.items():
    if low not in needed and not any(os.path.basename(r).lower() == low for r in roots):
        os.unlink(os.path.join(bin_dir, name))
        removed += 1
if problems:
    sys.exit("\n".join(sorted(set(problems))))

# ---------------------------------------------------------------- gdk-pixbuf loader cache
# A loader's description is the same in every build of a gdk-pixbuf version,
# whatever the platform: take them from the conda-forge cache (by loader file
# name), which has all but PNG and JPEG (built into conda-forge's gdk-pixbuf)
# and librsvg's SVG loader (installed after the cache was made).  Those three
# are from gdk-pixbuf's io-png.c and io-jpeg.c and a complete cache made by
# gdk-pixbuf-query-loaders on Linux.
BUILTIN = {
    "png": ['"png" 5 "gdk-pixbuf" "PNG" "LGPL"', '"image/png" ""', '"png" ""', '"\\211PNG\\r\\n\\032\\n" "" 100'],
    "jpeg": ['"jpeg" 5 "gdk-pixbuf" "JPEG" "LGPL"', '"image/jpeg" ""', '"jpeg" "jpe" "jpg" ""',
             '"\\377\\330" "" 100'],
    "svg": ['"svg" 6 "gdk-pixbuf" "Scalable Vector Graphics" "LGPL"',
            '"image/svg+xml" "image/svg" "image/svg-xml" "image/vnd.adobe.svg+xml" "text/xml-svg" '
            '"image/svg+xml-compressed" ""',
            '"svg" "svgz" "svg.gz" ""', '" <svg" "*    " 100', '" <!DOCTYPE svg" "*             " 100'],
}
LOADER = re.compile(r"(?:lib)?pixbufloader[-_](\w+)\.(?:dll|so)", re.I)
described = dict(BUILTIN)
conda_cache = os.path.join(conda, "Library", "lib", "gdk-pixbuf-2.0", "2.10.0", "loaders.cache")
for block in open(conda_cache, encoding="utf-8").read().split("\n\n"):
    lines = [ln for ln in block.splitlines() if ln and not ln.startswith("#")]
    m = LOADER.search(lines[0]) if len(lines) > 1 else None
    if m:
        described.setdefault(m.group(1), lines[1:])
loader_rel = "lib/gdk-pixbuf-2.0/2.10.0/loaders"
entries, undescribed = [], []
for name in sorted(os.listdir(os.path.join(rt, loader_rel))):
    m = LOADER.fullmatch(name)
    if not m:
        continue
    if m.group(1) not in described:
        undescribed.append(name)
        continue
    entries.append("\n".join(['"' + (loader_rel + "/" + name).replace("/", "\\\\") + '"'] + described[m.group(1)]))
with open(os.path.join(rt, loader_rel, "..", "loaders.cache"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("# GdkPixbuf Image Loader Modules file, written by anySCP's stage_msys2.py\n"
             "# (paths relative to the runtime folder)\n\n" + "\n\n".join(entries) + "\n")
if undescribed:
    sys.exit(f"no description for the gdk-pixbuf loaders {undescribed}")

size = sum(os.path.getsize(os.path.join(d, f)) for d, _x, fs in os.walk(rt) for f in fs)
print(f"    staged {PY}, {copied} files, {len(needed)} DLLs ({removed} unused ones left out), "
      f"{len(entries)} image loaders, {size / 1e6:.0f} MB")
print(f"    Windows DLLs used: {', '.join(sorted(system_used))}")
