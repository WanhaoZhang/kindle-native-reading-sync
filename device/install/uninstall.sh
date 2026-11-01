#!/bin/sh
# Default keeps all private ledgers and evidence. No wildcard recursive root deletion.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
require_device
umask 077;mkdir -p "$ROOT";: > "$ROOT/disabled"
stop kindle-native-sync-events >/dev/null 2>&1 || :
stop kindle-native-sync >/dev/null 2>&1 || :
root_rw
trap 'root_ro' EXIT
trap 'root_ro; exit 1' HUP INT TERM
rm -f /etc/upstart/kindle-native-sync.conf /etc/upstart/kindle-native-sync-events.conf
root_ro
trap - EXIT HUP INT TERM
initctl reload-configuration
for name in ksync-collector ksync-worker ksync-event-adapter;do rm -f "$ROOT/bin/$name";done
printf 'Disabled and removed startup jobs and executables. Private ledger/auth/config remain for safe recovery; not automatically deleted.\n'
