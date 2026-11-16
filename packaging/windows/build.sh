#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Build the portable Windows package of anySCP on Linux, a single file:
#
#   packaging/windows/build.sh [x64]   dist/anySCP-<version>-windows-x64.exe
#   packaging/windows/build.sh arm64   dist/anySCP-<version>-windows-arm64.exe  (Windows on ARM)
#
# The folder it is made from, anySCP.exe next to runtime\, stays in
# build/windows/stage-<arch>/anySCP (the launcher still runs it as it is).
#
# x64 uses the win-64 conda-forge runtime in build/windows/env.  conda-forge
# has no GTK for Windows on ARM, so arm64 uses MSYS2's (clangarm64) in
# build/windows/env-arm64, plus paramiko from the conda-forge one; its
# launcher is compiled with llvm-mingw.  Both are set up by
# packaging/setup-build-deps.sh.
#
#   packaging/windows/build.sh x64-msys2
#
# builds the arm64 package's twin for x64 (MSYS2 clang64; downloaded on first
# use) into build/windows/dist-x64-msys2/: the same runtime layout, launcher
# code path and Python, so that the arm64 package's set-up can be tested under
# Wine on an x86-64 machine.  It is not meant for distribution.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/packaging/windows"
TOOLS="$ROOT/build/tools"
WORK="$ROOT/build/windows"
CONDA_ENV="$WORK/env"
DIST="$ROOT/dist"
# The Linux runtime's tools (glib-compile-schemas, and Python with librsvg
# for the icon), so that nothing GTK-related is needed on the build machine.
LINUX_ENV="$ROOT/build/appimage/env"
missing() { echo "Missing $1 – run packaging/setup-build-deps.sh first." >&2; exit 1; }
[ -x "$LINUX_ENV/bin/python3" ] || missing "$LINUX_ENV"

ARCH="${1:-x64}"
case "$ARCH" in
    x64|amd64|x86_64)
        ARCH=x64; LAYOUT=conda; ENV="$CONDA_ENV"; MANIFEST_ARCH=amd64
        CC=x86_64-w64-mingw32-gcc; WINDRES=x86_64-w64-mingw32-windres ;;
    arm64|aarch64)
        ARCH=arm64; LAYOUT=msys2; ENV="$WORK/env-arm64"; MANIFEST_ARCH=arm64
        CC="$TOOLS/llvm-mingw/bin/aarch64-w64-mingw32-clang"; WINDRES="$TOOLS/llvm-mingw/bin/aarch64-w64-mingw32-windres" ;;
    x64-msys2)
        LAYOUT=msys2; ENV="$WORK/env-x64-msys2"; MANIFEST_ARCH=amd64; DIST="$WORK/dist-x64-msys2"
        CC=x86_64-w64-mingw32-gcc; WINDRES=x86_64-w64-mingw32-windres
        if [ ! -f "$ENV/bin/pythonw.exe" ]; then
            mapfile -t pkgs < <(grep -v '^#' "$HERE/msys2-packages.txt" | tr -s ' ' '\n' | grep .)
            python3 "$HERE/msys2_fetch.py" clang64 "$ENV" "$ROOT/build/msys2" "${pkgs[@]}"
        fi ;;
    *) echo "usage: $0 [x64|arm64|x64-msys2]" >&2; exit 2 ;;
esac
STAGE="$WORK/stage-$ARCH"
RT="$STAGE/anySCP/runtime"

[ -f "$CONDA_ENV/python.exe" ] || missing "$CONDA_ENV"
command -v "$CC" >/dev/null || missing "$CC"
[ -f "$TOOLS/miniz/miniz.c" ] || missing "miniz"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/anyscp/__init__.py")
if [ "$LAYOUT" = conda ]; then
    PYDLL=$(cd "$ENV" && ls python3[0-9]*.dll | head -1)          # e.g. python312.dll
    PYNUM=${PYDLL#python}; PYNUM=${PYNUM%.dll}                  # 312
    PYVER="${PYNUM:0:1}.${PYNUM:1}"                             # 3.12
    PYLIB="$RT/Lib"
    HOSTPY="$ROOT/build/appimage/env/bin/python$PYVER"
else
    [ -f "$ENV/bin/pythonw.exe" ] || missing "$ENV"
    PYVER=$(cd "$ENV/lib" && for d in python3.*; do [ -f "$d/os.py" ] && echo "${d#python}"; done | head -1)
    PYLIB="$RT/lib/python$PYVER"
    HOSTPY="$TOOLS/hostpy-$PYVER/bin/python$PYVER"
    [ -x "$HOSTPY" ] || missing "a Linux Python $PYVER in $TOOLS/hostpy-$PYVER (it precompiles the bytecode)"
fi
echo "==> anySCP $VERSION, Windows $ARCH, $LAYOUT runtime with Python $PYVER"

# ---------------------------------------------------------------- runtime
echo "==> Staging and pruning the runtime"
rm -rf "$STAGE"
mkdir -p "$RT"
if [ "$LAYOUT" = conda ]; then
    rsync -a \
        --exclude='/conda-meta' --exclude='/include' --exclude='/libs' --exclude='/Tools' --exclude='/Scripts' \
        --exclude='/bin' --exclude='/etc' --exclude='/share' --exclude='/fonts' --exclude='/lib' --exclude='/tcl' \
        --exclude='*.pdb' --exclude='*.lib' --exclude='*.a' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='/Library/include' --exclude='/Library/lib/pkgconfig' --exclude='/Library/lib/cmake' \
        --exclude='/Library/mingw-w64' --exclude='/Library/ssl/man' \
        --exclude='/Library/share/doc' --exclude='/Library/share/man' --exclude='/Library/share/info' \
        --exclude='/Library/share/gtk-doc' --exclude='/Library/share/gir-1.0' --exclude='/Library/share/locale' \
        --exclude='/Library/share/aclocal' --exclude='/Library/share/pkgconfig' --exclude='/Library/share/gettext' \
        --exclude='/Library/share/bash-completion' --exclude='/Library/share/metainfo' \
        --exclude='/Library/share/thumbnailers' --exclude='/Library/share/applications' \
        --exclude='/Library/share/installed-tests' --exclude='/Library/share/vala*' --exclude='/Library/share/gdb' \
        --exclude='/Library/share/glib-2.0/codegen' --exclude='/Library/share/glib-2.0/gdb' \
        --exclude='/Library/share/glib-2.0/valgrind' --exclude='/Library/share/gtk-4.0/valgrind' \
        --exclude='/Library/lib/girepository-1.0/*Demo*' \
        --exclude="/Lib/test" --exclude="/Lib/idlelib" --exclude="/Lib/tkinter" --exclude="/Lib/turtledemo" \
        --exclude="/Lib/ensurepip" --exclude="/Lib/lib2to3" \
        --exclude="/Lib/site-packages/pip" --exclude="/Lib/site-packages/pip-*" \
        --exclude="/Lib/site-packages/setuptools*" --exclude="/Lib/site-packages/wheel*" \
        --exclude="/Lib/site-packages/_distutils_hack" --exclude="/Lib/site-packages/distutils-precedence.pth" \
        --exclude='/DLLs/_tkinter.pyd' --exclude='/DLLs/tcl*.dll' --exclude='/DLLs/tk*.dll' \
        "$ENV/" "$RT/"
    # Keep only the helper programs GLib needs at run time.
    find "$RT/Library/bin" -maxdepth 1 -name '*.exe' ! -name 'gspawn-win64-helper*.exe' -delete

    # noarch Python packages were laid out Unix-style by micromamba on Linux
    if [ -d "$ENV/lib/python$PYVER/site-packages" ]; then
        rsync -a --exclude='__pycache__' --exclude='pip' --exclude='pip-*' --exclude='setuptools*' \
            --exclude='wheel*' --exclude='_distutils_hack' --exclude='distutils-precedence.pth' \
            "$ENV/lib/python$PYVER/site-packages/" "$RT/Lib/site-packages/"
    fi
    cp "$HERE/sitecustomize.py" "$RT/Lib/site-packages/"

    # Post-link steps that conda would have run on Windows
    "$LINUX_ENV/bin/glib-compile-schemas" "$RT/Library/share/glib-2.0/schemas"
    # (GTK ≥ 4.20 renders SVG icons itself; gdk-pixbuf's shipped loaders.cache is fine as is.)
else
    # Everything is in a Unix-like tree; Python finds the DLLs next to
    # pythonw.exe, so no sitecustomize is needed.
    python3 "$HERE/stage_msys2.py" "$ENV" "$RT" "$CONDA_ENV"
    "$LINUX_ENV/bin/glib-compile-schemas" "$RT/share/glib-2.0/schemas"   # (pacman's install hook would have)
fi

# ---------------------------------------------------------------- app
echo "==> Adding anySCP"
rsync -a --delete --exclude='__pycache__' "$ROOT/anyscp/" "$PYLIB/site-packages/anyscp/"
# Precompile with a Linux Python of the same version (bytecode is portable);
# unchecked-hash pycs stay valid whatever timestamps extraction produces.
if [ -x "$HOSTPY" ]; then
    "$HOSTPY" -m compileall -q -j0 --invalidation-mode unchecked-hash "$PYLIB" >/dev/null || true
fi
# Licenses of anySCP and of every bundled component
if [ "$LAYOUT" = conda ]; then
    python3 "$ROOT/packaging/collect_licenses.py" "$RT/licenses-info" "$ROOT" "conda:$ENV"
else
    python3 "$ROOT/packaging/collect_licenses.py" "$RT/licenses-info" "$ROOT" "msys2:$ENV:$RT" \
        "conda:$CONDA_ENV:paramiko"
fi
echo "==> Runtime size: $(du -sh "$RT" | cut -f1)"

# ---------------------------------------------------------------- launcher
echo "==> Compiling the launcher ($("$CC" --version | head -1))"
LAUNCH="$WORK/launcher-$ARCH"
mkdir -p "$LAUNCH"
GI_TYPELIB_PATH="$LINUX_ENV/lib/girepository-1.0" "$LINUX_ENV/bin/python3" -s "$HERE/make_ico.py" \
    "$ROOT/anyscp/data/icons/hicolor/scalable/apps/io.github.anyscp.AnySCP.svg" "$LAUNCH/anyscp.ico"
sed -e "s/@ARCH@/$MANIFEST_ARCH/" "$HERE/launcher.manifest" > "$LAUNCH/launcher.manifest"
VER_COMMA=$(echo "$VERSION" | awk -F. '{printf "%d,%d,%d", $1, $2+0, $3+0}')
sed -e "s/@VERSION@/$VERSION/g" -e "s/@VER_COMMA@/$VER_COMMA/g" "$HERE/launcher.rc" > "$LAUNCH/launcher.rc"
(cd "$LAUNCH" && "$WINDRES" launcher.rc -O coff -o launcher.res.o)
"$CC" -O2 -s -municode -mwindows -static -I"$TOOLS/miniz" \
    "$HERE/launcher.c" "$TOOLS/miniz/miniz.c" "$LAUNCH/launcher.res.o" \
    -o "$STAGE/anySCP/anySCP.exe" -lcomctl32
cp "$ROOT/README.md" "$STAGE/anySCP/README.md"
cp "$ROOT/LICENSE" "$STAGE/anySCP/LICENSE.txt"
cp "$RT/licenses-info/THIRD-PARTY-LICENSES.txt" "$STAGE/anySCP/THIRD-PARTY-LICENSES.txt"

# ---------------------------------------------------------------- package
mkdir -p "$DIST"
EXE="$DIST/anySCP-$VERSION-windows-$ARCH.exe"
rm -f "$DIST/anySCP-$VERSION-windows-$ARCH-portable.zip"      # (no longer made)
echo "==> Packing $EXE"
python3 "$HERE/mkzip.py" "$RT" "" "$WORK/payload-$ARCH.zip"
BUILD_ID=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:16])' "$WORK/payload-$ARCH.zip")
python3 - "$STAGE/anySCP/anySCP.exe" "$WORK/payload-$ARCH.zip" "$EXE" "$BUILD_ID" <<'PY'
import struct, sys
launcher, payload, out, build_id = sys.argv[1:5]
exe = open(launcher, "rb").read()
data = open(payload, "rb").read()
with open(out, "wb") as fh:
    fh.write(exe)
    fh.write(data)
    fh.write(struct.pack("<Q", len(exe)) + build_id.encode() + b"ANYSCPZ1")
PY
# The folder version copies its runtime to the same local cache when it is
# started from a network share: give it the same id and its size.
RT_BYTES=$(du -sb "$RT" | cut -f1)
printf '%s\n%s\n' "$BUILD_ID" "$RT_BYTES" > "$RT/anyscp-runtime.id"
rm -f "$WORK/payload-$ARCH.zip"
echo "==> Done:"
ls -lh "$EXE" | awk '{print "    " $5 "  " $9}'
