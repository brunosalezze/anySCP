#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
# Install anySCP for the current user (~/.local).  Usage:
#   ./install.sh            install / update
#   ./install.sh --uninstall
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
APP_ID=io.github.anyscp.AnySCP
SHARE="$PREFIX/share"
LIBDIR="$SHARE/anyscp"

if [ "$1" = "--uninstall" ]; then
    rm -rf "$LIBDIR" "$PREFIX/bin/anyscp" "$SHARE/applications/$APP_ID.desktop" \
        "$SHARE/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$SHARE/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
    echo "anySCP removed (settings in ~/.config/anyscp were kept)."
    exit 0
fi

missing=""
python3 - <<'PY' || missing=1
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5), "libadwaita >= 1.5 required"
import paramiko
PY
if [ -n "$missing" ]; then
    echo "Missing dependencies. On Debian/Ubuntu install them with:"
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-paramiko \\"
    echo "       gir1.2-gtksource-5 gir1.2-vte-3.91 gir1.2-secret-1"
    echo "On Fedora:  sudo dnf install python3-gobject gtk4 libadwaita python3-paramiko gtksourceview5 vte291-gtk4 libsecret"
    exit 1
fi

mkdir -p "$LIBDIR" "$PREFIX/bin" "$SHARE/applications" \
    "$SHARE/icons/hicolor/scalable/apps" "$SHARE/icons/hicolor/symbolic/apps"
rm -rf "$LIBDIR/anyscp"
cp -r "$HERE/anyscp" "$LIBDIR/"
find "$LIBDIR" -name __pycache__ -type d -prune -exec rm -rf {} +

cat > "$PREFIX/bin/anyscp" <<SH
#!/bin/sh
export PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m anyscp "\$@"
SH
chmod +x "$PREFIX/bin/anyscp"

sed "s|^Exec=anyscp|Exec=$PREFIX/bin/anyscp|" "$HERE/data/$APP_ID.desktop" > "$SHARE/applications/$APP_ID.desktop"
cp "$HERE/anyscp/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$SHARE/icons/hicolor/scalable/apps/"
cp "$HERE/anyscp/data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" "$SHARE/icons/hicolor/symbolic/apps/"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$SHARE/icons/hicolor" || true
command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" || true

echo "anySCP installed. Start it from your app menu or run: anyscp"
case ":$PATH:" in *":$PREFIX/bin:"*) ;; *) echo "(Add $PREFIX/bin to your PATH to run 'anyscp' from a terminal.)";; esac
