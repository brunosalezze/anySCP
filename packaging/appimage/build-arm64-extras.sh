#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Build GtkSourceView 5 and VTE (GTK 4) for linux-aarch64 into
# build/appimage/extras-aarch64.  conda-forge, where the rest of the arm64
# runtime comes from, has no arm64 build of either.
#
# It also runs the one post-install step of the arm64 runtime environment
# (build/appimage/env-aarch64) that could not run when micromamba installed it
# on the x86-64 build host: gdk-pixbuf's image loader cache.
#
# The build is a plain native one: on an x86-64 host this script re-runs
# itself in an arm64 container (docker + qemu-user), where the conda-forge
# compilers of build/appimage/buildenv-aarch64 run emulated.  That takes
# about 15-30 minutes, once.  packaging/setup-build-deps.sh downloads the
# sources and calls this script.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
. "$ROOT/packaging/appimage/arm64-extras.env"
BENV="$ROOT/build/appimage/buildenv-aarch64"
RENV="$ROOT/build/appimage/env-aarch64"
SRC="$ROOT/build/appimage/extras-src"
WORK="$ROOT/build/appimage/extras-build"
OUT="$ROOT/build/appimage/extras-aarch64"

[ -e "$BENV/bin/meson" ] || { echo "Missing $BENV – run packaging/setup-build-deps.sh first." >&2; exit 1; }
[ -e "$RENV/bin/python3" ] || { echo "Missing $RENV – run packaging/setup-build-deps.sh first." >&2; exit 1; }
for t in "gtksourceview-$GTKSOURCEVIEW_VERSION" "vte-$VTE_VERSION"; do
    [ -f "$SRC/$t.tar.xz" ] || { echo "Missing $SRC/$t.tar.xz – run packaging/setup-build-deps.sh first." >&2; exit 1; }
done

if [ -z "${ANYSCP_EXTRAS_IN_CONTAINER:-}" ]; then
    # Unpack here: the bare container image below has no xz.
    rm -rf "$WORK" "$OUT"
    mkdir -p "$WORK" "$OUT/licenses"
    tar -xf "$SRC/gtksourceview-$GTKSOURCEVIEW_VERSION.tar.xz" -C "$WORK"
    tar -xf "$SRC/vte-$VTE_VERSION.tar.xz" -C "$WORK"
    if [ "$(uname -m)" != aarch64 ]; then
        command -v docker >/dev/null || { echo "docker is needed to build the arm64 libraries on this machine." >&2; exit 1; }
        echo "==> Building in an emulated arm64 container ($ARM64_BUILD_IMAGE)"
        # Same path inside the container: the conda environment is not relocatable.
        exec docker run --rm --platform linux/arm64 -v "$ROOT:$ROOT" -w "$ROOT" \
            -u "$(id -u):$(id -g)" -e HOME=/tmp -e ANYSCP_EXTRAS_IN_CONTAINER=1 "$ARM64_BUILD_IMAGE" \
            "$ROOT/packaging/appimage/build-arm64-extras.sh"
    fi
fi

# ---------------------------------------------------------------- runtime post-install
echo "==> gdk-pixbuf loader cache of $RENV"
PREFIX="$RENV" bash "$RENV/bin/.gdk-pixbuf-post-link.sh"
[ -s "$RENV/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ] \
    || { echo "gdk-pixbuf-query-loaders failed: $(cat "$RENV/.messages.txt" 2>/dev/null)" >&2; exit 1; }

# ---------------------------------------------------------------- toolchain
export CONDA_PREFIX="$BENV"
export PATH="$BENV/bin:/usr/bin:/bin"
set +u
for f in "$BENV"/etc/conda/activate.d/*.sh; do . "$f"; done     # CC, CXX, CFLAGS, LDFLAGS, AR, ...
set -u
export PKG_CONFIG_PATH="$OUT/lib/pkgconfig:$BENV/lib/pkgconfig:$BENV/share/pkgconfig"
export XDG_DATA_DIRS="$OUT/share:$BENV/share"
JOBS="$(nproc)"
echo "==> $("$CC" --version | head -1), $JOBS jobs"

build() {  # name version -- meson options
    local name="$1" version="$2"; shift 2
    echo "==> $name $version"
    meson setup "$WORK/$name-build" "$WORK/$name-$version" \
        --prefix="$OUT" --libdir=lib --buildtype=release \
        --default-library=shared --wrap-mode=nofallback "$@"
    ninja -C "$WORK/$name-build" -j "$JOBS"
    ninja -C "$WORK/$name-build" install >/dev/null
}

build gtksourceview "$GTKSOURCEVIEW_VERSION" \
    -Dintrospection=enabled -Ddocumentation=false -Dvapi=false
build vte "$VTE_VERSION" \
    -D_systemd=false -Dgir=true -Ddocs=false -Dvapi=false -Dgtk3=false \
    -Dfribidi=true -Dicu=true -Dglade=false

# ---------------------------------------------------------------- finish
# Like every library of the runtime: stripped, and looking for its
# dependencies next to itself (the AppImage never sets LD_LIBRARY_PATH).
# (VTE installs its library as plain .so.0, GtkSourceView as .so.0.0.0.)
for lib in "$OUT"/lib/libgtksourceview-5.so* "$OUT"/lib/libvte-2.91-gtk4.so*; do
    [ -L "$lib" ] && continue
    "$STRIP" --strip-unneeded "$lib"
    patchelf --set-rpath '$ORIGIN' "$lib"
done
ls "$OUT/lib/girepository-1.0/GtkSource-5.typelib" "$OUT/lib/girepository-1.0/Vte-3.91.typelib" >/dev/null
# Caches the install step made for these two packages alone (conda-forge's
# packages leave them out too): they would hide the runtime's other icons.
rm -f "$OUT/share/icons/hicolor/icon-theme.cache" "$OUT/share/glib-2.0/schemas/gschemas.compiled"

mkdir -p "$OUT/licenses/gtksourceview" "$OUT/licenses/vte"
cp "$WORK/gtksourceview-$GTKSOURCEVIEW_VERSION/COPYING" "$OUT/licenses/gtksourceview/"
cp "$WORK/vte-$VTE_VERSION"/COPYING.* "$OUT/licenses/vte/"
cat > "$OUT/anyscp-extras.json" <<JSON
[
 {"name": "gtksourceview", "version": "$GTKSOURCEVIEW_VERSION", "license": "LGPL-2.0-or-later",
  "homepage": "https://gitlab.gnome.org/GNOME/gtksourceview",
  "recipe": "built from source by packaging/appimage/build-arm64-extras.sh (meson options as in conda-forge's recipe)",
  "url": "https://download.gnome.org/sources/gtksourceview/${GTKSOURCEVIEW_VERSION%.*}/gtksourceview-$GTKSOURCEVIEW_VERSION.tar.xz",
  "license_dir": "licenses/gtksourceview"},
 {"name": "vte", "version": "$VTE_VERSION", "license": "LGPL-3.0-or-later",
  "homepage": "https://gitlab.gnome.org/GNOME/vte",
  "recipe": "built from source by packaging/appimage/build-arm64-extras.sh (meson options as in conda-forge's recipe)",
  "url": "https://download.gnome.org/sources/vte/${VTE_VERSION%.*}/vte-$VTE_VERSION.tar.xz",
  "license_dir": "licenses/vte"}
]
JSON
rm -rf "$WORK"
echo "==> Done: $OUT ($(du -sh "$OUT" | cut -f1))"
