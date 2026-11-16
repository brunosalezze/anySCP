#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Lay out the files of an MSIX package (for the Microsoft Store) from a
# Windows build's folder; makeappx (Windows SDK) then packs the folder.
#
#   make_layout.sh <x64|arm64> [<identity name> <publisher> <publisher display name>]
#
# The identity values come from Partner Center (Product identity); without
# them, the Store app's own (store.env).  Run packaging/windows/build.sh <arch>
# first.  Output: build/msix/<arch>/layout
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HERE="$ROOT/packaging/windows/msix"
. "$HERE/store.env"
ARCH="$1" NAME="${2:-$MSIX_NAME}" PUBLISHER="${3:-$MSIX_PUBLISHER}" DISPLAY="${4:-$MSIX_PUBLISHER_DISPLAY_NAME}"
case "$ARCH" in
    x64) MIN_WINDOWS=10.0.17763.0 ;;            # Windows 10 1809
    arm64) MIN_WINDOWS=10.0.22000.0 ;;          # Windows 11 (the arm64 build needs it)
    *) echo "architecture: x64 or arm64" >&2; exit 1 ;;
esac
STAGE="$ROOT/build/windows/stage-$ARCH/anySCP"
[ -f "$STAGE/anySCP.exe" ] || { echo "Missing $STAGE: run packaging/windows/build.sh $ARCH" >&2; exit 1; }
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/anyscp/__init__.py")
LAYOUT="$ROOT/build/msix/$ARCH/layout"
rm -rf "$LAYOUT" && mkdir -p "$LAYOUT/Assets"
cp -a "$STAGE/." "$LAYOUT/"
# (the Store wants four numbers, the last one 0)
MSIX_VERSION="$VERSION$(printf '.0%.0s' $(seq $((4 - $(tr -cd . <<<"$VERSION" | wc -c) - 1))))"
xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g' <<<"$1"; }
sed -e "s|@NAME@|$(xml "$NAME")|" -e "s|@PUBLISHER@|$(xml "$PUBLISHER")|" \
    -e "s|@PUBLISHER_DISPLAY_NAME@|$(xml "$DISPLAY")|" -e "s|@VERSION@|$MSIX_VERSION|" \
    -e "s|@ARCH@|$ARCH|" -e "s|@MIN_WINDOWS@|$MIN_WINDOWS|" \
    "$HERE/AppxManifest.xml" > "$LAYOUT/AppxManifest.xml"
SVG="$ROOT/anyscp/data/icons/hicolor/scalable/apps/io.github.anyscp.AnySCP.svg"
RSVG="$ROOT/build/appimage/env/bin/rsvg-convert"
for asset in Square44x44Logo:44 Square150x150Logo:150 StoreLogo:50; do
    "$RSVG" -w "${asset#*:}" -h "${asset#*:}" "$SVG" -o "$LAYOUT/Assets/${asset%:*}.png"
done
echo "MSIX layout: $LAYOUT (version $MSIX_VERSION, $(find "$LAYOUT" -type f | wc -l) files)"
