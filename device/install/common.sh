#!/bin/sh
# Sourced by root-only installation/control scripts. Never prints serial numbers.
set -eu
ROOT=/var/local/kindle-native-sync
PROFILE=pw2-5.12.2.1.1
firmware_ok() {
    for f in /etc/prettyversion.txt /etc/prettyversion /etc/version.txt; do
        [ -f "$f" ] || continue
        grep -Eq '^Kindle[[:space:]]+5[.]12[.]2[.]1[.]1([[:space:]]|$)' "$f" && return 0
    done
    return 1
}
require_device() {
    [ "$(id -u)" = 0 ] || { echo 'Root execution is required.' >&2; exit 1; }
    firmware_ok || { echo 'Unsupported firmware; only PW2 5.12.2.1.1 is enabled.' >&2; exit 1; }
    [ "$(uname -m)" = armv7l ] || { echo 'Unsupported machine; require ARMv7.' >&2; exit 1; }
    # Read only four bytes, never read or print the complete serial number.
    [ -r /proc/usid ] || { echo 'Cannot verify PW2 model.' >&2; exit 1; }
    prefix=$(dd if=/proc/usid bs=1 count=4 2>/dev/null)
    [ "$prefix" = B0D4 ] || { echo 'Unsupported model prefix.' >&2; exit 1; }
    [ ! -L "$ROOT" ] || { echo 'Private root must not be a symlink.' >&2; exit 1; }
}
space_kib() { df -Pk /var/local | awk 'END {print $4}'; }
root_rw() { command -v mntroot >/dev/null 2>&1 && mntroot rw >/dev/null; }
root_ro() { command -v mntroot >/dev/null 2>&1 && mntroot ro >/dev/null; }
