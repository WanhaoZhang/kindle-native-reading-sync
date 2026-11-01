#!/bin/sh
# First-time actions from KUAL. Does not jailbreak or enable network writes.
set -eu
PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH
PACKAGE=/mnt/us/kindle-native-sync-package
[ "$(id -u)" = 0 ] || { echo 'A verified root launcher is required.' >&2;exit 1; }
[ "$(lipc-get-prop com.lab126.volumd userstoreIsAvailable 2>/dev/null)" = 1 ] || exit 1
[ -d "$PACKAGE/install" ] && [ ! -L "$PACKAGE" ] || { echo 'Copy the verified candidate to /mnt/us/kindle-native-sync-package first.' >&2;exit 1; }
case "${1:-probe}" in
 probe)
    OUT=/mnt/us/.kindle-native-sync-export
    [ ! -L "$OUT" ] || exit 1
    mkdir -p "$OUT"
    [ ! -L "$OUT/probe.json.partial" ] || exit 1
    sh "$PACKAGE/install/probe.sh" > "$OUT/probe.json.partial"
    mv "$OUT/probe.json.partial" "$OUT/probe.json"
    sync;;
 install)
    exec sh "$PACKAGE/install/install.sh" "$PACKAGE";;
 *) echo 'Usage: bootstrap.sh probe|install' >&2;exit 2;;
esac
