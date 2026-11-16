#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Download and install everything needed to build the portable anySCP
# packages (Linux AppImage + Windows .exe, for x86-64 and arm64) on an
# x86-64 Debian/Ubuntu machine.
#
#   ./packaging/setup-build-deps.sh
#
# What it does (safe to re-run; finished steps are skipped):
#   1. apt: gcc-mingw-w64-x86-64 (compiles the small anySCP.exe launcher),
#      wine/wine64 (to test the Windows build on this machine) and
#      qemu-user-static (runs arm64 programs: build and test of the arm64 AppImage)
#   2. downloads into build/tools/: micromamba (conda package manager, a single
#      static binary), appimagetool + AppImage runtimes, miniz (zip library),
#      llvm-mingw (compiles the arm64 anySCP.exe launcher)
#   3. creates the conda-forge environments with Python, GTK 4, libadwaita,
#      PyGObject, GtkSourceView and paramiko:
#        build/appimage/env            linux-64       (payload of the AppImage)
#        build/windows/env             win-64         (payload of the .exe, downloaded on Linux)
#        build/appimage/env-aarch64    linux-aarch64  (payload of the arm64 AppImage)
#        build/appimage/buildenv-aarch64              (the same plus compilers, for step 6)
#   4. downloads the Windows-on-ARM runtime from MSYS2 into build/windows/env-arm64
#      (conda-forge has no GTK for Windows on ARM)
#   5. builds tiny docker images (ubuntu 22.04/24.04 + Xvfb, x86-64 and arm64) used
#      to prove the AppImages run on older distributions without GTK installed
#   6. builds GtkSourceView and VTE for linux-aarch64 from source (conda-forge
#      has neither) and makes gdk-pixbuf's loader cache of the arm64 runtime;
#      in an emulated arm64 container, about 10-30 minutes, once
#
# Everything except the apt packages and docker images stays inside build/.
# Needs roughly 6 GB of disk space; the downloads take a few minutes.
# Without docker, steps 5 and 6 are skipped: the arm64 AppImage then has no
# syntax highlighting in the editor and no embedded terminal.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$ROOT/build"
TOOLS="$BUILD/tools"
export MAMBA_ROOT_PREFIX="$BUILD/mamba"
MM="$TOOLS/bin/micromamba"

PY_VERSION="3.12"
# Runtime packages shared by both platforms (conda-forge names).
COMMON_PKGS=(
    "python=$PY_VERSION"
    pygobject pycairo
    gtk4 libadwaita
    librsvg gdk-pixbuf
    adwaita-icon-theme hicolor-icon-theme
    "paramiko>=3.4,<4" cryptography bcrypt pynacl
)
SOURCEVIEW_PKG="gtksourceview>=5,<6"      # conda-forge: linux-64 and win-64 only
LINUX_ONLY_PKGS=(libsecret)
# linux-aarch64: GtkSourceView and VTE are built from source (step 6).  These
# are the libraries the two need at run time, and what it takes to build them.
ARM_EXTRA_RUNTIME=(gnutls icu lz4-c simdutf fmt pcre2 fribidi libxml2 libexpat)
ARM_BUILD_TOOLS=(gcc_linux-aarch64 gxx_linux-aarch64 meson ninja pkg-config gobject-introspection
                 glib gettext-tools fast_float patchelf libxml2-devel expat zlib)
. "$ROOT/packaging/appimage/arm64-extras.env"
# Windows on ARM: MSYS2 packages (clangarm64), listed in packaging/windows/msys2-packages.txt
mapfile -t MSYS2_PKGS < <(grep -v '^#' "$ROOT/packaging/windows/msys2-packages.txt" | tr -s ' ' '\n' | grep .)
LLVM_MINGW_VERSION=20261104
LLVM_MINGW_SHA256=919380403de87dca3c581c2053230cd298af5e891286628ba0613b7fed370926

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

download() {  # url dest
    local url="$1" dest="$2"
    curl -fL --retry 3 --connect-timeout 20 -o "$dest.part" "$url" || { rm -f "$dest.part"; return 1; }
    mv "$dest.part" "$dest"
}

mkdir -p "$TOOLS/bin" "$BUILD/appimage" "$BUILD/windows" "$MAMBA_ROOT_PREFIX"

# --------------------------------------------------------------------------
step "1/6  System packages (apt)"
need=()
command -v x86_64-w64-mingw32-gcc >/dev/null || need+=(gcc-mingw-w64-x86-64)
command -v wine >/dev/null || need+=(wine)
command -v wine64 >/dev/null || [ -x /usr/lib/wine/wine64 ] || need+=(wine64)
command -v curl >/dev/null || need+=(curl)
command -v unzip >/dev/null || need+=(unzip)
command -v zstd >/dev/null || need+=(zstd)
command -v rsync >/dev/null || need+=(rsync)
[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] || need+=(qemu-user-static)
if [ ${#need[@]} -gt 0 ]; then
    echo "    installing: ${need[*]}"
    sudo apt-get update -qq
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need[@]}"
fi
x86_64-w64-mingw32-gcc --version | head -1 | sed 's/^/    /'
wine --version | sed 's/^/    /'
[ -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ] \
    || warn "arm64 programs cannot run here (no qemu-aarch64 binfmt entry): step 6 and the arm64 tests will fail"
ok "system packages ready"

# --------------------------------------------------------------------------
step "2/6  Build tools"
if [ ! -x "$MM" ]; then
    download "https://micro.mamba.pm/api/micromamba/linux-64/latest" "$TOOLS/micromamba.tar.bz2" \
        || die "could not download micromamba"
    tar -xjf "$TOOLS/micromamba.tar.bz2" -C "$TOOLS" bin/micromamba
    rm -f "$TOOLS/micromamba.tar.bz2"
fi
ok "micromamba $("$MM" --version)"

if [ ! -x "$TOOLS/appimagetool/AppRun" ]; then
    download "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" \
        "$TOOLS/appimagetool-x86_64.AppImage" || die "could not download appimagetool"
    chmod +x "$TOOLS/appimagetool-x86_64.AppImage"
    # Unpack it so it runs without FUSE.
    (cd "$TOOLS" && rm -rf squashfs-root && ./appimagetool-x86_64.AppImage --appimage-extract >/dev/null \
        && rm -rf appimagetool && mv squashfs-root appimagetool)
    rm -f "$TOOLS/appimagetool-x86_64.AppImage"
fi
ok "appimagetool unpacked in build/tools/appimagetool"

if [ ! -s "$TOOLS/runtime-x86_64" ]; then
    download "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64" \
        "$TOOLS/runtime-x86_64" || die "could not download the AppImage runtime"
fi
if [ ! -s "$TOOLS/runtime-aarch64" ]; then
    download "https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-aarch64" \
        "$TOOLS/runtime-aarch64" || die "could not download the arm64 AppImage runtime"
fi
ok "AppImage type2 runtimes (x86_64: $(stat -c %s "$TOOLS/runtime-x86_64") bytes, aarch64: $(stat -c %s "$TOOLS/runtime-aarch64") bytes)"

if [ ! -x "$TOOLS/llvm-mingw/bin/aarch64-w64-mingw32-clang" ]; then
    name="llvm-mingw-$LLVM_MINGW_VERSION-ucrt-ubuntu-22.04-x86_64"
    download "https://github.com/mstorsjo/llvm-mingw/releases/download/$LLVM_MINGW_VERSION/$name.tar.xz" \
        "$TOOLS/llvm-mingw.tar.xz" || die "could not download llvm-mingw"
    echo "$LLVM_MINGW_SHA256  $TOOLS/llvm-mingw.tar.xz" | sha256sum -c --quiet - \
        || die "llvm-mingw: checksum mismatch"
    rm -rf "$TOOLS/llvm-mingw" && mkdir -p "$TOOLS/llvm-mingw"
    tar -xf "$TOOLS/llvm-mingw.tar.xz" -C "$TOOLS/llvm-mingw" --strip-components=1
    rm -f "$TOOLS/llvm-mingw.tar.xz"
fi
ok "llvm-mingw $LLVM_MINGW_VERSION (compiler for the Windows-on-ARM launcher)"

if [ ! -s "$TOOLS/miniz/miniz.c" ]; then
    got=""
    for v in 3.1.0 3.0.2; do
        if download "https://github.com/richgel999/miniz/releases/download/$v/miniz-$v.zip" "$TOOLS/miniz.zip"; then
            got=$v; break
        fi
    done
    [ -n "$got" ] || die "could not download miniz"
    rm -rf "$TOOLS/miniz" && mkdir -p "$TOOLS/miniz"
    unzip -q -o "$TOOLS/miniz.zip" -d "$TOOLS/miniz"
    # some releases nest the files in a folder
    if [ ! -f "$TOOLS/miniz/miniz.c" ]; then
        find "$TOOLS/miniz" -name 'miniz.[ch]' -exec mv {} "$TOOLS/miniz/" \;
    fi
    rm -f "$TOOLS/miniz.zip"
    [ -s "$TOOLS/miniz/miniz.c" ] || die "miniz archive did not contain miniz.c"
fi
ok "miniz (zip library for the self-extracting .exe)"

# --------------------------------------------------------------------------
step "3/6  conda-forge runtimes (GTK 4, libadwaita, PyGObject, Python $PY_VERSION)"

maybe_add_vte() {  # env_prefix
    # VTE (embedded terminal) is optional; only take it if conda-forge's build
    # is the GTK 4 one and does not drag in GTK 3.
    local prefix="$1" plan
    plan=$("$MM" install -y -p "$prefix" -c conda-forge --override-channels --dry-run --json vte 2>/dev/null || true)
    if printf '%s' "$plan" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
names = {p["name"] for p in data.get("actions", {}).get("LINK", [])}
sys.exit(0 if "vte" in names and "gtk3" not in names else 1)'; then
        "$MM" install -y -p "$prefix" -c conda-forge --override-channels vte >/dev/null
        if ls "$prefix"/lib/libvte-2.91-gtk4.so* >/dev/null 2>&1; then
            ok "vte (GTK 4 build) added: embedded terminal available"
        else
            warn "conda-forge vte has no GTK 4 library; removing it (the app falls back to the system terminal)"
            "$MM" remove -y -p "$prefix" vte >/dev/null || true
        fi
    else
        warn "vte for GTK 4 not available from conda-forge; the AppImage will use the system terminal"
    fi
}

LINUX_ENV="$BUILD/appimage/env"
if [ ! -x "$LINUX_ENV/bin/python3" ]; then
    echo "    creating linux-64 environment (this downloads a few hundred MB)…"
    "$MM" create -y -p "$LINUX_ENV" -c conda-forge --override-channels \
        "${COMMON_PKGS[@]}" "$SOURCEVIEW_PKG" "${LINUX_ONLY_PKGS[@]}"
    maybe_add_vte "$LINUX_ENV"
fi
# gdk-pixbuf made its loader cache before librsvg (the SVG loader) was
# installed, and nothing updates it afterwards: make it again.  (With the
# folder given explicitly: the one compiled in is wrong once build/ moves.)
PIXBUF="$LINUX_ENV/lib/gdk-pixbuf-2.0/2.10.0"
if ! grep -q _svg "$PIXBUF/loaders.cache" 2>/dev/null; then
    GDK_PIXBUF_MODULEDIR="$PIXBUF/loaders" "$LINUX_ENV/bin/gdk-pixbuf-query-loaders" > "$PIXBUF/loaders.cache.new" \
        && mv "$PIXBUF/loaders.cache.new" "$PIXBUF/loaders.cache" || die "gdk-pixbuf-query-loaders failed"
fi
# (GI_TYPELIB_PATH: a conda environment does not find its typelibs by itself
# once the project folder has been moved or renamed)
GI_TYPELIB_PATH="$LINUX_ENV/lib/girepository-1.0" "$LINUX_ENV/bin/python3" - <<'PY' || die "linux-64 environment is incomplete"
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: F401  (typelibs + libs load)
import paramiko, cairo  # noqa: F401
print(f"    ✓ linux-64: Python {__import__('sys').version.split()[0]}, GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}, "
      f"libadwaita {Adw.get_major_version()}.{Adw.get_minor_version()}, paramiko {paramiko.__version__}")
PY

WIN_ENV="$BUILD/windows/env"
if [ ! -f "$WIN_ENV/python.exe" ]; then
    echo "    creating win-64 environment (downloaded on Linux; this is a few hundred MB)…"
    CONDA_SUBDIR=win-64 "$MM" create -y -p "$WIN_ENV" --platform win-64 \
        -c conda-forge --override-channels "${COMMON_PKGS[@]}" "$SOURCEVIEW_PKG"
fi
[ -f "$WIN_ENV/python.exe" ] || die "win-64 environment has no python.exe"
ls "$WIN_ENV"/Library/bin/libadwaita-1-0.dll "$WIN_ENV"/Library/bin/gtk-4*.dll >/dev/null 2>&1 \
    || ls "$WIN_ENV"/Library/bin/*adwaita*.dll "$WIN_ENV"/Library/bin/*gtk-4*.dll >/dev/null 2>&1 \
    || die "win-64 environment is missing the GTK 4 / libadwaita DLLs"
ok "win-64: $(ls "$WIN_ENV"/Library/bin/*.dll | wc -l) DLLs, typelibs: $(ls "$WIN_ENV"/Library/lib/girepository-1.0 | wc -l)"

# linux-aarch64.  Its programs cannot run on this machine without emulation,
# so nothing of it is executed here: no link scripts, no bytecode compilation
# (the AppImage build does what is needed with the linux-64 tools).
ARM_ENV="$BUILD/appimage/env-aarch64"
if [ ! -x "$ARM_ENV/bin/python3" ]; then
    echo "    creating linux-aarch64 environment (this downloads about 130 MB)…"
    "$MM" create -y -p "$ARM_ENV" --platform linux-aarch64 --no-pyc --skip-run-link-scripts \
        -c conda-forge --override-channels \
        "${COMMON_PKGS[@]}" "${LINUX_ONLY_PKGS[@]}" "${ARM_EXTRA_RUNTIME[@]}"
fi
ls "$ARM_ENV"/lib/libadwaita-1.so.0 "$ARM_ENV"/lib/libgtk-4.so.1 \
    "$ARM_ENV"/lib/girepository-1.0/Adw-1.typelib >/dev/null 2>&1 \
    || die "linux-aarch64 environment is incomplete"
ok "linux-aarch64: $(ls "$ARM_ENV"/conda-meta/*.json | wc -l) packages"

# The same packages, pinned, plus compilers: what GtkSourceView and VTE are
# built against in step 6.  The C library the compilers link against is the
# oldest one the runtime itself supports, so the two do not raise that floor.
ARM_BENV="$BUILD/appimage/buildenv-aarch64"
if [ ! -x "$ARM_BENV/bin/meson" ]; then
    mapfile -t pins < <(python3 "$ROOT/packaging/appimage/conda_pins.py" "$ARM_ENV" linux-aarch64)
    echo "    creating the linux-aarch64 build environment (compilers: about 200 MB more; ${pins[-1]})…"
    "$MM" create -y -p "$ARM_BENV" --platform linux-aarch64 --no-pyc --skip-run-link-scripts \
        -c conda-forge --override-channels "${pins[@]}" "${ARM_BUILD_TOOLS[@]}"
fi
[ -e "$ARM_BENV/bin/meson" ] || die "linux-aarch64 build environment is incomplete"
ok "linux-aarch64 build environment"

# --------------------------------------------------------------------------
step "4/6  MSYS2 runtime for Windows on ARM (GTK 4, libadwaita, PyGObject, Python)"
WINARM_ENV="$BUILD/windows/env-arm64"
if [ ! -f "$WINARM_ENV/bin/pythonw.exe" ]; then
    python3 "$ROOT/packaging/windows/msys2_fetch.py" clangarm64 "$WINARM_ENV" "$BUILD/msys2" "${MSYS2_PKGS[@]}"
fi
ls "$WINARM_ENV"/bin/pythonw.exe "$WINARM_ENV"/bin/libadwaita-1-0.dll "$WINARM_ENV"/bin/libgtk-4-1.dll \
    "$WINARM_ENV"/lib/girepository-1.0/Adw-1.typelib >/dev/null 2>&1 \
    || die "the Windows-on-ARM runtime is incomplete"
WINARM_PYVER=$(cd "$WINARM_ENV/lib" && ls -d python3.* | head -1)     # python3.14
ok "clangarm64: $(ls "$WINARM_ENV"/bin/*.dll | wc -l) DLLs, ${WINARM_PYVER/python/Python }"

# A Linux Python of the same version: precompiles the bytecode of that runtime.
HOSTPY="$TOOLS/hostpy-${WINARM_PYVER#python}"
if [ ! -x "$HOSTPY/bin/python3" ]; then
    "$MM" create -y -p "$HOSTPY" -c conda-forge --override-channels "python=${WINARM_PYVER#python}" >/dev/null
fi
ok "host $("$HOSTPY/bin/python3" --version) (bytecode compiler for the Windows-on-ARM runtime)"

# --------------------------------------------------------------------------
step "5/6  Docker test images (AppImage portability check)"
HAVE_DOCKER=0
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    HAVE_DOCKER=1
    for tag in 22.04 24.04; do
        if docker image inspect "anyscp-test:$tag" >/dev/null 2>&1; then
            ok "anyscp-test:$tag already built"
            continue
        fi
        docker build -q -t "anyscp-test:$tag" - <<EOF >/dev/null
FROM ubuntu:$tag
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xvfb xauth ca-certificates && rm -rf /var/lib/apt/lists/*
EOF
        ok "anyscp-test:$tag (ubuntu $tag + Xvfb, no GTK)"
    done
    # The same for arm64 (emulated).  The images come from the arm64v8
    # repository so that the ubuntu:<tag> images above stay the x86-64 ones.
    for tag in 22.04 24.04; do
        if docker image inspect "anyscp-test:$tag-arm64" >/dev/null 2>&1; then
            ok "anyscp-test:$tag-arm64 already built"
            continue
        fi
        if docker build -q --platform linux/arm64 -t "anyscp-test:$tag-arm64" - <<EOF >/dev/null
FROM arm64v8/ubuntu:$tag
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        xvfb xauth ca-certificates && rm -rf /var/lib/apt/lists/*
EOF
        then ok "anyscp-test:$tag-arm64 (ubuntu $tag + Xvfb, no GTK, arm64)"
        else warn "could not build anyscp-test:$tag-arm64 (is qemu-user-static installed?)"
        fi
    done
else
    warn "docker not usable: skipping (needed for the portability tests and for step 6)"
fi

# --------------------------------------------------------------------------
step "6/6  GtkSourceView $GTKSOURCEVIEW_VERSION and VTE $VTE_VERSION for linux-aarch64"
EXTRAS="$BUILD/appimage/extras-aarch64"
if [ -f "$EXTRAS/anyscp-extras.json" ] && [ -s "$ARM_ENV/lib/gdk-pixbuf-2.0/2.10.0/loaders.cache" ]; then
    ok "already built"
else
    mkdir -p "$BUILD/appimage/extras-src"
    for item in "gtksourceview $GTKSOURCEVIEW_VERSION $GTKSOURCEVIEW_SHA256" "vte $VTE_VERSION $VTE_SHA256"; do
        set -- $item
        tarball="$BUILD/appimage/extras-src/$1-$2.tar.xz"
        if [ ! -s "$tarball" ]; then
            download "https://download.gnome.org/sources/$1/${2%.*}/$1-$2.tar.xz" "$tarball" \
                || die "could not download $1 $2"
        fi
        echo "$3  $tarball" | sha256sum -c --quiet - || die "$1 $2: checksum mismatch"
    done
    ok "sources downloaded and verified"
    if [ "$HAVE_DOCKER" = 1 ]; then
        echo "    building (emulated arm64 compilers: this takes a while)…"
        if "$ROOT/packaging/appimage/build-arm64-extras.sh" > "$BUILD/appimage/extras-build.log" 2>&1; then
            ok "built: $(du -sh "$EXTRAS" | cut -f1) in build/appimage/extras-aarch64"
        else
            warn "the build failed, see build/appimage/extras-build.log"
            warn "the arm64 AppImage will have no syntax highlighting and no embedded terminal"
        fi
    else
        warn "docker not usable: not built (run packaging/appimage/build-arm64-extras.sh on an arm64 machine,"
        warn "or the arm64 AppImage has no syntax highlighting and no embedded terminal)"
    fi
fi

step "Done"
du -sh "$BUILD/tools" "$LINUX_ENV" "$WIN_ENV" "$ARM_ENV" "$ARM_BENV" "$WINARM_ENV" "$MAMBA_ROOT_PREFIX" 2>/dev/null | sed 's/^/    /'
echo "    Everything is in $BUILD (apt packages and docker images aside)."
echo "    Build with packaging/appimage/build.sh [aarch64] and packaging/windows/build.sh [arm64]."
