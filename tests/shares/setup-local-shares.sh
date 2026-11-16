#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 anySCP contributors
#
# Set up LOCAL-ONLY SMB and NFS shares for testing anySCP run from network
# shares (tests/shares/run-share-tests.sh).  Run with sudo rights:
#
#     ./tests/shares/setup-local-shares.sh
#
# What it does (undo everything with ./tests/shares/teardown-local-shares.sh):
#   * apt: samba, cifs-utils, nfs-kernel-server
#   * stops and disables Debian's default smbd/nmbd services (so no SMB
#     server listens on your network) and instead runs a PRIVATE smbd that
#     only listens on 127.0.0.1:4455, with its own config/state in
#     /srv/anyscp-share-test/samba
#   * exports two folders over NFS to 127.0.0.1 only (temporary exports made
#     with exportfs – not written to /etc/exports, gone after a reboot)
#   * mounts them, owned by you:
#       /mnt/anyscp-test/smb      SMB 3.1.1, read-write
#       /mnt/anyscp-test/smb-ro   SMB 3.1.1, read-only
#       /mnt/anyscp-test/nfs      NFS 4.2,   read-write
#       /mnt/anyscp-test/nfs-ro   NFS 4.2,   read-only
#   Mounts and the private smbd do not survive a reboot; re-run this script.

set -euo pipefail

USER_NAME="${SUDO_USER:-$USER}"
USER_UID="$(id -u "$USER_NAME")"
USER_GID="$(id -g "$USER_NAME")"
BASE=/srv/anyscp-share-test
SAMBA="$BASE/samba"
MNT=/mnt/anyscp-test
PORT=4455

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }

if [ "$(id -u)" -ne 0 ]; then
    exec sudo --preserve-env=SUDO_USER "$0" "$@"
fi

step "1/5  Packages"
need=()
command -v smbd >/dev/null || need+=(samba)
command -v mount.cifs >/dev/null || need+=(cifs-utils)
command -v exportfs >/dev/null || need+=(nfs-kernel-server)
if [ ${#need[@]} -gt 0 ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need[@]}"
fi
# Debian starts smbd/nmbd on all interfaces after installing samba: stop that.
for svc in smbd nmbd samba-ad-dc; do
    systemctl disable --now "$svc" >/dev/null 2>&1 || true
done
ok "samba $(smbd --version | cut -d' ' -f2), cifs-utils, nfs-kernel-server; system smbd/nmbd stopped"

step "2/5  Test folders"
mkdir -p "$BASE"/{smb,smb-ro,nfs,nfs-ro} "$SAMBA"/{private,lock,state,cache,pid,log} "$MNT"/{smb,smb-ro,nfs,nfs-ro}
chown "$USER_UID:$USER_GID" "$BASE"/{smb,smb-ro,nfs,nfs-ro} "$MNT"/{smb,smb-ro,nfs,nfs-ro}
ok "$BASE"

step "3/5  Private SMB server on 127.0.0.1:$PORT"
cat > "$SAMBA/smb.conf" <<EOF
[global]
    workgroup = WORKGROUP
    server string = anySCP share tests
    interfaces = 127.0.0.1
    bind interfaces only = yes
    smb ports = $PORT
    disable netbios = yes
    server min protocol = SMB3_11
    server role = standalone server
    security = user
    passdb backend = tdbsam:$SAMBA/private/passdb.tdb
    private dir = $SAMBA/private
    lock directory = $SAMBA/lock
    state directory = $SAMBA/state
    cache directory = $SAMBA/cache
    pid directory = $SAMBA/pid
    ncalrpc dir = $SAMBA/state/ncalrpc
    log file = $SAMBA/log/smbd.log
    max log size = 1000
    load printers = no
    printing = bsd
    printcap name = /dev/null
    disable spoolss = yes
    unix extensions = no
    hosts allow = 127.0.0.1
    hosts deny = ALL
[anyscp-rw]
    path = $BASE/smb
    read only = no
    valid users = $USER_NAME
    force user = $USER_NAME
[anyscp-ro]
    path = $BASE/smb-ro
    read only = yes
    valid users = $USER_NAME
    force user = $USER_NAME
EOF
if [ -f "$SAMBA/pid/smbd.pid" ] && kill -0 "$(cat "$SAMBA/pid/smbd.pid")" 2>/dev/null; then
    kill "$(cat "$SAMBA/pid/smbd.pid")" || true
    sleep 1
fi
PASS_FILE="$SAMBA/credentials"
if [ ! -s "$PASS_FILE" ]; then
    pw="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
    printf 'username=%s\npassword=%s\n' "$USER_NAME" "$pw" > "$PASS_FILE"
    chmod 600 "$PASS_FILE"
fi
pw="$(sed -n 's/^password=//p' "$PASS_FILE")"
printf '%s\n%s\n' "$pw" "$pw" | smbpasswd -c "$SAMBA/smb.conf" -s -a "$USER_NAME" >/dev/null
smbd -D -s "$SAMBA/smb.conf"
sleep 1
ok "smbd running (pid $(cat "$SAMBA/pid/smbd.pid")), shares //127.0.0.1/anyscp-rw and anyscp-ro"

step "4/5  NFS exports to 127.0.0.1"
systemctl start nfs-server >/dev/null 2>&1 || systemctl start nfs-kernel-server
exportfs -o rw,sync,no_subtree_check,all_squash,anonuid="$USER_UID",anongid="$USER_GID" "127.0.0.1:$BASE/nfs"
exportfs -o ro,sync,no_subtree_check,all_squash,anonuid="$USER_UID",anongid="$USER_GID" "127.0.0.1:$BASE/nfs-ro"
ok "$(exportfs | grep -c anyscp-share-test) temporary exports"

step "5/5  Mounting"
domount() {  # type source target options
    if mountpoint -q "$3"; then umount "$3"; fi
    mount -t "$1" "$2" "$3" -o "$4"
    ok "$3  ($1)"
}
# file_mode with x: shares that programs are started from allow execution
cifs_opts="port=$PORT,vers=3.1.1,credentials=$PASS_FILE,uid=$USER_UID,gid=$USER_GID,file_mode=0775,dir_mode=0775,cache=strict"
domount cifs "//127.0.0.1/anyscp-rw" "$MNT/smb"    "$cifs_opts"
domount cifs "//127.0.0.1/anyscp-ro" "$MNT/smb-ro" "$cifs_opts,ro"
domount nfs  "127.0.0.1:$BASE/nfs"    "$MNT/nfs"    "vers=4.2,hard,timeo=600"
domount nfs  "127.0.0.1:$BASE/nfs-ro" "$MNT/nfs-ro" "vers=4.2,hard,timeo=600,ro"

# a sample file in the read-only shares so listings are not empty
for d in smb-ro nfs-ro; do
    echo "read-only test share" > "$BASE/$d/README.txt"
    chown "$USER_UID:$USER_GID" "$BASE/$d/README.txt"
done

step "Done"
echo "    Shares are mounted under $MNT (local only). Tell Claude it finished."
echo "    Remove everything later with: ./tests/shares/teardown-local-shares.sh [--purge]"
