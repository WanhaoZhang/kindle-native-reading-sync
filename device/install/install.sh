#!/bin/sh
# Root on the Kindle only. No jailbreak payload or credential network access.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
require_device
PACKAGE=${1:-$(dirname "$HERE")}
[ -d /etc/upstart ] && command -v initctl >/dev/null 2>&1 || { echo 'Upstart unavailable.' >&2;exit 1; }
free=$(space_kib)
[ "$free" -ge 65536 ] || { echo 'Need at least 64 MiB free in /var/local before installation.' >&2;exit 1; }
[ ! -L "$PACKAGE" ] || exit 1
(cd "$PACKAGE" && sha256sum -c SHA256SUMS) >/dev/null || { echo 'Package checksum verification failed.' >&2;exit 1; }
for name in ksync-collector ksync-worker ksync-event-adapter; do
    f="$PACKAGE/bin/$name"
    [ -f "$f" ] && [ ! -L "$f" ] || exit 1
    magic=$(dd if="$f" bs=1 count=7 2>/dev/null | od -An -tx1 | tr -d ' \n')
    machine=$(dd if="$f" bs=1 skip=18 count=2 2>/dev/null | od -An -tx1 | tr -d ' \n')
    [ "$magic" = 7f454c46010101 ] && [ "$machine" = 2800 ] || { echo 'Package contains a non-ARM32 binary.' >&2;exit 1; }
done
# Stage complete replacement before stopping the old service; keep previous binaries for rollback.
umask 077
mkdir -p "$ROOT/bin.next" "$ROOT/install"
cp "$PACKAGE/bin/ksync-collector" "$PACKAGE/bin/ksync-worker" "$PACKAGE/bin/ksync-event-adapter" "$ROOT/bin.next/"
chmod 700 "$ROOT/bin.next/"*
cp "$PACKAGE/install/"*.sh "$ROOT/install/"
chmod 700 "$ROOT/install/"*.sh
cp "$PACKAGE/manifest.json" "$ROOT/package-manifest.json"
if [ -f "$PACKAGE/ca-bundle.crt" ];then cp "$PACKAGE/ca-bundle.crt" "$ROOT/ca-bundle.crt";chmod 600 "$ROOT/ca-bundle.crt";fi
[ ! -d "$ROOT/bin.previous" ] || { echo 'Previous upgrade backup exists; inspect it before upgrading again.' >&2;exit 1; }
stop kindle-native-sync-events >/dev/null 2>&1 || :
stop kindle-native-sync >/dev/null 2>&1 || :
if [ -d "$ROOT/bin" ];then mv "$ROOT/bin" "$ROOT/bin.previous";fi
mv "$ROOT/bin.next" "$ROOT/bin"
# Collection only is the default. Validation evidence is never manufactured by installer.
root_rw
trap 'root_ro' EXIT
trap 'root_ro; exit 1' HUP INT TERM
cp "$PACKAGE/upstart/kindle-native-sync.conf" /etc/upstart/kindle-native-sync.conf
cp "$PACKAGE/upstart/kindle-native-sync-events.conf" /etc/upstart/kindle-native-sync-events.conf
chmod 644 /etc/upstart/kindle-native-sync*.conf
root_ro
trap - EXIT HUP INT TERM
initctl reload-configuration
start kindle-native-sync
start kindle-native-sync-events
printf 'Installed in collection-only mode. Run probe.sh and worker self-test; uploads require separate validation.\n'
