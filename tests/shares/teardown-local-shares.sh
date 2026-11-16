#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
#
# Undo tests/shares/setup-local-shares.sh:
#   unmount the test shares, stop the private smbd, remove the temporary NFS
#   exports and the test folders.  With --purge, also uninstall samba,
#   cifs-utils and nfs-kernel-server.
set -uo pipefail

BASE=/srv/anyscp-share-test
MNT=/mnt/anyscp-test

if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

for m in smb smb-ro nfs nfs-ro; do
    mountpoint -q "$MNT/$m" && umount -l "$MNT/$m" && echo "unmounted $MNT/$m"
done
if [ -f "$BASE/samba/pid/smbd.pid" ]; then
    kill "$(cat "$BASE/samba/pid/smbd.pid")" 2>/dev/null && echo "stopped private smbd"
fi
for d in nfs nfs-ro; do
    exportfs -u "127.0.0.1:$BASE/$d" 2>/dev/null && echo "removed NFS export $BASE/$d"
done
if [ -d "$BASE" ]; then
    rm -rf --one-file-system "$BASE" && echo "removed $BASE"
fi
if [ -d "$MNT" ]; then
    rmdir "$MNT"/* "$MNT" 2>/dev/null && echo "removed $MNT"
fi
if [ "${1:-}" = "--purge" ]; then
    systemctl disable --now nfs-server 2>/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get purge -y samba cifs-utils nfs-kernel-server
    apt-get autoremove -y
fi
echo "done"
