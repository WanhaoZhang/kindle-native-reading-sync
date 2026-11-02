#!/bin/sh
# First-time actions from KUAL. Does not jailbreak, read auth, or create acceptance.
set -eu
PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH
PACKAGE=/mnt/us/kindle-native-sync-package
ROOT=/var/local/kindle-native-sync
OUT=/mnt/us/.kindle-native-sync-export
[ "$(id -u)" = 0 ] || { echo 'A verified root launcher is required.' >&2;exit 1; }
[ "$(lipc-get-prop com.lab126.volumd userstoreIsAvailable 2>/dev/null)" = 1 ] || exit 1
[ -d "$PACKAGE/install" ] && [ ! -L "$PACKAGE" ] || { echo 'Copy the verified candidate to /mnt/us/kindle-native-sync-package first.' >&2;exit 1; }
prepare_output() {
    [ ! -L "$OUT" ] || exit 1
    mkdir -p "$OUT"
    [ ! -L "$OUT/$1.partial" ] && [ ! -L "$OUT/$1" ] || exit 1
}
run_candidate() {
    name=$1;shift
    prepare_output "$name"
    [ -f "$PACKAGE/bin/ksync-worker" ] && [ ! -L "$PACKAGE/bin/ksync-worker" ] || exit 1
    (cd "$PACKAGE" && sha256sum -c SHA256SUMS) >/dev/null || { echo 'Candidate checksum failed.' >&2;exit 1; }
    # Core limits affect this test child only; no system-wide crash policy changes.
    if (ulimit -c 0 || exit 125; exec "$PACKAGE/bin/ksync-worker" --root "$ROOT" self-test "$@") > "$OUT/$name.partial" 2>/dev/null;then
        :
    else
        code=$?
        printf '{"schema_version":1,"state":"candidate_process_failed","worker_exit_code":%s,"acceptance_created":false}\n' "$code" > "$OUT/$name.partial"
    fi
    mv "$OUT/$name.partial" "$OUT/$name"
    sync
}
case "${1:-probe}" in
 probe)
    prepare_output probe.json
    sh "$PACKAGE/install/probe.sh" > "$OUT/probe.json.partial"
    mv "$OUT/probe.json.partial" "$OUT/probe.json"
    sync;;
 self-test)
    run_candidate selftest.json;;
 tls-self-test)
    prepare_output tls-selftest.json
    if [ "$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null)" != CONNECTED ];then
        printf '{"schema_version":1,"state":"wifi_not_connected","network_contacted":false,"acceptance_created":false}\n' > "$OUT/tls-selftest.json.partial"
        mv "$OUT/tls-selftest.json.partial" "$OUT/tls-selftest.json"
        sync
        exit 1
    fi
    run_candidate tls-selftest.json --online-self-test;;
 install)
    prepare_output install-result.json
    # Keep the outcome visible after USB reconnection; no account data is read.
    if sh "$PACKAGE/install/install.sh" "$PACKAGE";then
        printf '{"schema_version":1,"state":"installed_collection_only","exit_code":0,"auto_upload_enabled":false}\n' > "$OUT/install-result.json.partial"
    else
        code=$?
        printf '{"schema_version":1,"state":"installation_failed","exit_code":%s,"auto_upload_enabled":false}\n' "$code" > "$OUT/install-result.json.partial"
    fi
    mv "$OUT/install-result.json.partial" "$OUT/install-result.json"
    sync;;
 *) echo 'Usage: bootstrap.sh probe|self-test|tls-self-test|install' >&2;exit 2;;
esac
