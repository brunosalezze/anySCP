#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Build dist/anySCP-<version>-<arch>.AppImage:
#
#   packaging/appimage/build.sh            x86_64
#   packaging/appimage/build.sh aarch64    arm64 (64-bit ARM: Raspberry Pi 4/5, Ampere, …)
#
# Uses the conda-forge runtime made by packaging/setup-build-deps.sh (Python,
# GTK 4, libadwaita, PyGObject, GtkSourceView, VTE, paramiko):
# build/appimage/env for x86_64; build/appimage/env-aarch64 plus the
# GtkSourceView and VTE built in build/appimage/extras-aarch64 for arm64.
# The tools the build runs come from the x86_64 runtime, so the arm64 image
# is made on this machine without emulation.  Re-run after changing the app:
# the runtime is copied and pruned again (a few seconds) and the image repacked.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/packaging/appimage"
HOSTENV="$ROOT/build/appimage/env"          # x86_64: runs glib-compile-schemas, rsvg-convert, compileall
TOOLS="$ROOT/build/tools"
DIST="$ROOT/dist"
APP_ID=io.github.anyscp.AnySCP
missing() { echo "Missing $1 – run packaging/setup-build-deps.sh first." >&2; exit 1; }

ARCH="${1:-x86_64}"
case "$ARCH" in
    x86_64|amd64|x64) ARCH=x86_64; ENV="$HOSTENV"; EXTRAS="" ;;
    aarch64|arm64)    ARCH=aarch64; ENV="$ROOT/build/appimage/env-aarch64"; EXTRAS="$ROOT/build/appimage/extras-aarch64" ;;
    *) echo "usage: $0 [x86_64|aarch64]" >&2; exit 2 ;;
esac
APPDIR="$ROOT/build/appimage/AppDir-$ARCH"

[ -x "$HOSTENV/bin/python3" ] || missing "$HOSTENV"
[ -e "$ENV/bin/python3" ] || missing "$ENV"
[ -x "$TOOLS/appimagetool/AppRun" ] || missing "appimagetool"
[ -s "$TOOLS/runtime-$ARCH" ] || missing "$TOOLS/runtime-$ARCH"
[ -s "$ENV/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ] || missing "the gdk-pixbuf loader cache of $ENV"
if [ -n "$EXTRAS" ] && [ ! -f "$EXTRAS/anyscp-extras.json" ]; then
    echo "    (warning: $EXTRAS is missing: no syntax highlighting, no embedded terminal)"
    EXTRAS=""
fi

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/anyscp/__init__.py")
# (the standard library's folder: conda also has a python3.1 -> python3.12 link)
PYVER=$(cd "$ENV/lib" && for d in python3.*; do [ ! -L "$d" ] && [ -f "$d/os.py" ] && echo "${d#python}"; done | head -1)
HOSTPY="$HOSTENV/bin/python$PYVER"
[ -x "$HOSTPY" ] || { echo "The x86_64 runtime has no Python $PYVER to compile the $ARCH bytecode with." >&2; exit 1; }
OUT="$DIST/anySCP-$VERSION-$ARCH.AppImage"
echo "==> anySCP $VERSION, $ARCH, Python $PYVER"

# ---------------------------------------------------------------- runtime
echo "==> Copying and pruning the runtime"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
PRUNE=(
    --exclude='/conda-meta' --exclude='/include' --exclude='/man' --exclude='/bin' --exclude='/libexec' \
    --exclude='/share/man' --exclude='/share/doc' --exclude='/share/info' --exclude='/share/gtk-doc' \
    --exclude='/share/gir-1.0' --exclude='/share/locale' --exclude='/share/aclocal' \
    --exclude='/share/pkgconfig' --exclude='/share/bash-completion' --exclude='/share/zsh' \
    --exclude='/share/gettext*' --exclude='/share/vala*' --exclude='/share/installed-tests' \
    --exclude='/share/thumbnailers' --exclude='/share/applications' --exclude='/share/terminfo' \
    --exclude='/share/zoneinfo' --exclude='/share/tabset' --exclude='/share/gdb' --exclude='/share/cups' \
    --exclude='/share/glib-2.0/codegen' --exclude='/share/glib-2.0/gdb' --exclude='/share/glib-2.0/valgrind' \
    --exclude='/share/gtk-4.0/valgrind' --exclude='/share/metainfo' --exclude='/share/dbus-1' \
    --exclude='/lib/pkgconfig' --exclude='/lib/cmake' --exclude='/lib/*.a' --exclude='/lib/*.la' \
    --exclude='/lib/tcl8*' --exclude='/lib/tk8*' --exclude='/lib/libtcl*' --exclude='/lib/libtk*' \
    --exclude='/lib/itcl*' --exclude='/lib/thread2*' --exclude='/lib/pkcs11' --exclude='/lib/gettext' \
    --exclude='/lib/gobject-introspection' --exclude='/lib/girepository-1.0/*Demo*' \
    --exclude="/lib/python$PYVER/test" --exclude="/lib/python$PYVER/idlelib" \
    --exclude="/lib/python$PYVER/tkinter" --exclude="/lib/python$PYVER/turtledemo" \
    --exclude="/lib/python$PYVER/ensurepip" --exclude="/lib/python$PYVER/lib2to3" \
    --exclude="/lib/python$PYVER/config-$PYVER-*" --exclude="/lib/python$PYVER/lib-dynload/_tkinter*" \
    --exclude="/lib/python$PYVER/site-packages/pip" --exclude="/lib/python$PYVER/site-packages/pip-*" \
    --exclude="/lib/python$PYVER/site-packages/setuptools*" --exclude="/lib/python$PYVER/site-packages/wheel*" \
    --exclude="/lib/python$PYVER/site-packages/_distutils_hack" --exclude="/lib/python$PYVER/site-packages/distutils-precedence.pth" \
    --exclude='__pycache__' --exclude='*.pyc'
    --exclude='/etc/profile.d' --exclude='/share/xdg-terminals' --exclude='/.mambarc'
    --exclude='/*-conda-linux-gnu'            # (python's linker for building extensions)
    --exclude='icon-theme.cache' --exclude='gschemas.compiled'  # stale partial caches
)
rsync -a "${PRUNE[@]}" "$ENV/" "$APPDIR/usr/"
if [ -n "$EXTRAS" ]; then
    # GtkSourceView and VTE, built from source for this architecture
    rsync -a "${PRUNE[@]}" --exclude='/licenses' --exclude='/anyscp-extras.json' --exclude='/lib/*.so' \
        "$EXTRAS/" "$APPDIR/usr/"
fi
cp -a "$ENV/bin/python$PYVER" "$APPDIR/usr/bin/"
ln -sf "python$PYVER" "$APPDIR/usr/bin/python3"

# GSettings schemas (GTK needs its own for the file chooser, colour picker…)
"$HOSTENV/bin/glib-compile-schemas" "$APPDIR/usr/share/glib-2.0/schemas"

# (gdk-pixbuf's loaders.cache uses paths relative to the runtime: kept as is)

# fontconfig: host configuration and fonts first, bundled fonts as fallback;
# a standalone variant for systems that have no fontconfig setup at all
cp "$HERE/anyscp-fonts.conf" "$HERE/anyscp-fonts-standalone.conf" "$APPDIR/usr/etc/fonts/"

# MIME database (file type detection and icons) – conda-forge has none compiled
if [ -f /usr/share/mime/packages/freedesktop.org.xml ] && command -v update-mime-database >/dev/null; then
    cp /usr/share/mime/packages/freedesktop.org.xml "$APPDIR/usr/share/mime/packages/"
    update-mime-database "$APPDIR/usr/share/mime" >/dev/null 2>&1
else
    echo "    (warning: shared-mime-info not found on the build host; file type icons rely on the user's system)"
fi

# ---------------------------------------------------------------- app
echo "==> Adding anySCP"
SITE="$APPDIR/usr/lib/python$PYVER/site-packages"
rsync -a --delete --exclude='__pycache__' "$ROOT/anyscp/" "$SITE/anyscp/"
# (bytecode does not depend on the architecture, only on the Python version)
"$HOSTPY" -m compileall -q -j0 "$APPDIR/usr/lib/python$PYVER" >/dev/null || true

# ---------------------------------------------------------------- AppDir metadata
sed -e "s/@PYVER@/$PYVER/" "$HERE/AppRun" > "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
ICON="$ROOT/anyscp/data/icons/hicolor/scalable/apps/$APP_ID.svg"
cp "$ICON" "$APPDIR/$APP_ID.svg"
"$HOSTENV/bin/rsvg-convert" -w 256 -h 256 "$ICON" -o "$APPDIR/$APP_ID.png"
ln -sf "$APP_ID.png" "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$APPDIR/usr/share/applications"
cp "$ICON" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
sed -e 's/^Exec=.*/Exec=anyscp %U/' -e "/^DBusActivatable/d" \
    "$ROOT/data/$APP_ID.desktop" > "$APPDIR/$APP_ID.desktop"
echo "X-AppImage-Version=$VERSION" >> "$APPDIR/$APP_ID.desktop"
cp "$APPDIR/$APP_ID.desktop" "$APPDIR/usr/share/applications/"

# Licenses of anySCP and of every bundled component
python3 "$ROOT/packaging/collect_licenses.py" "$APPDIR/usr/share/doc/anyscp" "$ROOT" "conda:$ENV" \
    ${EXTRAS:+"extras:$EXTRAS"}

echo "==> Runtime size: $(du -sh "$APPDIR" | cut -f1)"

# ---------------------------------------------------------------- pack
echo "==> Packing $OUT"
mkdir -p "$DIST"
rm -f "$OUT"
ARCH="$ARCH" VERSION="$VERSION" "$TOOLS/appimagetool/AppRun" --no-appstream \
    --runtime-file "$TOOLS/runtime-$ARCH" "$APPDIR" "$OUT" >/dev/null
chmod +x "$OUT"
echo "==> Done: $OUT ($(du -h "$OUT" | cut -f1))"
