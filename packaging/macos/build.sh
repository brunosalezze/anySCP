#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Build anySCP.app and its disk image for macOS on Apple Silicon:
#
#   packaging/macos/build.sh
#
# Runs on a Mac with Apple Silicon (such as GitHub's macos-15 runners) with
# the Xcode command line tools (clang, codesign, iconutil, hdiutil).  It
# downloads micromamba and the conda-forge runtime (Python, GTK 4, libadwaita,
# PyGObject, GtkSourceView, Paramiko, keyring) into build/macos and makes
#   build/macos/anySCP.app                  signed ad hoc (no Apple Developer ID)
#   dist/anySCP-<version>-macos-arm64.dmg
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/packaging/macos"
WORK="$ROOT/build/macos"
DIST="$ROOT/dist"
export MAMBA_ROOT_PREFIX="$ROOT/build/mamba"    # (collect_licenses.py reads its package cache)
MM="$WORK/tools/micromamba"
ENV="$WORK/env"
APP="$WORK/anySCP.app"
MINOS=11.0                                      # what conda-forge's libadwaita needs
# The runtime of the other builds (packaging/setup-build-deps.sh), with keyring
# (the macOS Keychain) instead of libsecret.  conda-forge has no VTE for macOS:
# terminals open in Terminal.app.
PKGS=(
    "python=3.12" pygobject pycairo gtk4 libadwaita librsvg gdk-pixbuf
    adwaita-icon-theme hicolor-icon-theme "paramiko>=3.4,<4" cryptography bcrypt pynacl
    "gtksourceview>=5,<6" keyring
)

[ "$(uname -s)" = Darwin ] || { echo "This builds the macOS app, on a Mac." >&2; exit 1; }
[ "$(uname -m)" = arm64 ] || { echo "anySCP for macOS is built for Apple Silicon only." >&2; exit 1; }
for tool in clang codesign iconutil hdiutil install_name_tool otool ditto; do
    command -v "$tool" >/dev/null \
        || { echo "$tool is missing: install the Xcode command line tools (xcode-select --install)." >&2; exit 1; }
done
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/anyscp/__init__.py")
DMG="$DIST/anySCP-$VERSION-macos-arm64.dmg"
mkdir -p "$WORK/tools" "$DIST"

# ---------------------------------------------------------------- runtime
if [ ! -x "$MM" ]; then
    echo "==> Downloading micromamba"
    curl -fsSL --retry 3 https://micro.mamba.pm/api/micromamba/osx-arm64/latest \
        | tar -xj -C "$WORK/tools" --strip-components=1 bin/micromamba
fi
if [ ! -x "$ENV/bin/python3" ]; then
    echo "==> Creating the runtime (conda-forge, osx-arm64)"
    "$MM" create -y -q -p "$ENV" -c conda-forge --override-channels "${PKGS[@]}"
fi
PYVER=$(cd "$ENV/lib" && for d in python3.*; do [ -f "$d/os.py" ] && echo "${d#python}"; done | head -1)
echo "==> anySCP $VERSION for macOS (Apple Silicon), Python $PYVER"

# ---------------------------------------------------------------- app bundle
echo "==> Copying and pruning the runtime"
rm -rf "$APP"
R="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$R"
ditto "$ENV" "$R"
PRUNE=(
    conda-meta include man bin libexec .mambarc etc/profile.d
    share/man share/doc share/info share/gtk-doc share/gir-1.0 share/locale share/aclocal
    share/pkgconfig share/bash-completion share/zsh 'share/gettext*' 'share/vala*' share/installed-tests
    share/thumbnailers share/applications share/terminfo share/tabset share/gdb share/metainfo share/dbus-1
    share/xdg-terminals share/glib-2.0/codegen share/glib-2.0/gdb share/glib-2.0/valgrind share/gtk-4.0/valgrind
    lib/pkgconfig lib/cmake 'lib/*.a' 'lib/*.la' 'lib/tcl8*' 'lib/tk8*' 'lib/libtcl*' 'lib/libtk*'
    'lib/itcl*' 'lib/thread2*' lib/gettext lib/gobject-introspection
    "lib/python$PYVER/test" "lib/python$PYVER/idlelib" "lib/python$PYVER/tkinter"
    "lib/python$PYVER/turtledemo" "lib/python$PYVER/ensurepip" "lib/python$PYVER/config-$PYVER-darwin"
    "lib/python$PYVER/lib-dynload/_tkinter*"
    "lib/python$PYVER/site-packages/pip" "lib/python$PYVER/site-packages/pip-*"
    "lib/python$PYVER/site-packages/setuptools*" "lib/python$PYVER/site-packages/wheel*"
    "lib/python$PYVER/site-packages/_distutils_hack" "lib/python$PYVER/site-packages/distutils-precedence.pth"
)
(cd "$R" && for p in "${PRUNE[@]}"; do rm -rf $p; done)      # (unquoted: the patterns expand)
find "$R" \( -name __pycache__ -o -name '*.pyc' -o -name icon-theme.cache -o -name gschemas.compiled \) \
    -prune -exec rm -rf {} +
"$ENV/bin/glib-compile-schemas" "$R/share/glib-2.0/schemas"
# gdk-pixbuf's loaders, with paths relative to the runtime
PIXBUF=lib/gdk-pixbuf-2.0/2.10.0
GDK_PIXBUF_MODULEDIR="$ENV/$PIXBUF/loaders" "$ENV/bin/gdk-pixbuf-query-loaders" \
    | sed "s|\"$ENV/|\"|" > "$R/$PIXBUF/loaders.cache"

echo "==> Adding anySCP"
SITE="$R/lib/python$PYVER/site-packages"
rm -rf "$SITE/anyscp"
ditto "$ROOT/anyscp" "$SITE/anyscp"
find "$SITE/anyscp" -name __pycache__ -prune -exec rm -rf {} +
# (unchecked: Python never rewrites them, which would break the bundle's signature)
"$ENV/bin/python3" -m compileall -q -j0 --invalidation-mode unchecked-hash "$R/lib/python$PYVER" >/dev/null || true

# Licenses of anySCP and of every bundled component
"$ENV/bin/python3" "$ROOT/packaging/collect_licenses.py" "$R/share/doc/anyscp" "$ROOT" "conda:$ENV"

echo "==> Icon, Info.plist and the launcher"
ICONSET="$WORK/anySCP.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
SVG="$ROOT/anyscp/data/icons/hicolor/scalable/apps/io.github.anyscp.AnySCP.svg"
for size in 16 32 128 256 512; do
    "$ENV/bin/rsvg-convert" -w "$size" -h "$size" "$SVG" -o "$ICONSET/icon_${size}x${size}.png"
    "$ENV/bin/rsvg-convert" -w $((size * 2)) -h $((size * 2)) "$SVG" -o "$ICONSET/icon_${size}x${size}@2x.png"
done
iconutil -c icns "$ICONSET" -o "$R/anySCP.icns"
rm -rf "$ICONSET"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@MINOS@/$MINOS/g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

LAUNCHER="$APP/Contents/MacOS/anySCP"
clang -O2 -Wall -Wextra -arch arm64 -mmacosx-version-min="$MINOS" -I "$ENV/include/python$PYVER" \
    "$HERE/launcher.c" -L "$ENV/lib" -lpython"$PYVER" -Wl,-rpath,@executable_path/../Resources/lib \
    -o "$LAUNCHER"
# libpython must be found through the rpath, not at the build machine's path
LIBPY_ID=$(otool -D "$ENV/lib/libpython$PYVER.dylib" | tail -1)
[ "$LIBPY_ID" = "@rpath/libpython$PYVER.dylib" ] \
    || install_name_tool -change "$LIBPY_ID" "@rpath/libpython$PYVER.dylib" "$LAUNCHER"
otool -L "$LAUNCHER" | grep -q "@rpath/libpython$PYVER.dylib" \
    || { echo "the launcher does not load libpython through its rpath" >&2; exit 1; }

# ---------------------------------------------------------------- signature
# Apple Silicon runs only signed code; without an Apple Developer ID the
# signature is an ad hoc one (people allow the app once, see the README).
echo "==> Signing (ad hoc)"
find "$R" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 \
    | xargs -0 codesign --force --sign - 2>&1 | grep -v "replacing existing signature" || true
codesign --force --sign - "$APP"
codesign --verify --strict "$APP"
echo "    runtime size: $(du -sh "$APP" | cut -f1)"

# ---------------------------------------------------------------- disk image
echo "==> Making the disk image"
STAGE="$WORK/dmg"
rm -rf "$STAGE" "$DMG" && mkdir -p "$STAGE"
ditto "$APP" "$STAGE/anySCP.app"
ln -s /Applications "$STAGE/Applications"
for attempt in 1 2 3; do              # (hdiutil now and then finds its device busy)
    hdiutil create -quiet -volname "anySCP $VERSION" -srcfolder "$STAGE" -fs HFS+ -format UDZO "$DMG" && break
    echo "    hdiutil failed (attempt $attempt), trying again"
    sleep 10
done
rm -rf "$STAGE"
[ -f "$DMG" ] || { echo "hdiutil could not make the disk image" >&2; exit 1; }
echo "==> Done: $DMG ($(du -h "$DMG" | cut -f1))"
