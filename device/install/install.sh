#!/bin/sh
# Root on the Kindle only. A transaction never modifies auth/config/ledger/data.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
. "$HERE/transaction.sh"
require_device
PACKAGE=${1:-$(dirname "$HERE")}
[ -d "$JOBS" ] && command -v initctl >/dev/null 2>&1 || { echo 'Upstart unavailable.' >&2;exit 1; }
[ ! -e "$ROOT/disabled" ] || { echo 'Collection is explicitly disabled; enable it before requesting installation.' >&2;exit 1; }
free=$(space_kib)
ksync_storage_budget "$PACKAGE" "$free" || {
    printf 'Private storage budget blocked: %s. Existing data were not changed.\n' "$storage_budget_blocked" >&2
    exit 1
}
[ "$raw_queue_over_limit" = false ] || { echo 'Existing raw queue exceeds this profile; preserved for explicit review, not deleted.' >&2;exit 1; }
[ "$free" -ge "$required_install_free_kib" ] || {
    printf 'Insufficient /var/local space: available=%s KiB, required=%s KiB (runtime reserve plus full staging/recovery peak).\n' "$free" "$required_install_free_kib" >&2
    exit 1
}
[ ! -L "$PACKAGE" ] || exit 1
for path in "$STAGE" "$ROOT/bin.next" "$ROOT/bin.previous" "$ROOT/.uninstall-backup";do
    [ ! -e "$path" ] && [ ! -L "$path" ] || { echo 'Unreviewed project staging/rollback residue exists.' >&2;exit 1; }
done
(cd "$PACKAGE" && sha256sum -c SHA256SUMS) >/dev/null || { echo 'Package checksum verification failed.' >&2;exit 1; }
for name in ksync-collector ksync-worker ksync-event-adapter;do
    f="$PACKAGE/bin/$name"
    [ -f "$f" ] && [ ! -L "$f" ] || exit 1
    magic=$(dd if="$f" bs=1 count=7 2>/dev/null | od -An -tx1 | tr -d ' \n')
    machine=$(dd if="$f" bs=1 skip=18 count=2 2>/dev/null | od -An -tx1 | tr -d ' \n')
    [ "$magic" = 7f454c46010101 ] && [ "$machine" = 2800 ] || { echo 'Package contains a non-ARM32 binary.' >&2;exit 1; }
done
begin_transaction
mkdir "$STAGE" "$STAGE/bin" "$STAGE/install" "$STAGE/upstart"
cp "$PACKAGE/bin/ksync-collector" "$PACKAGE/bin/ksync-worker" "$PACKAGE/bin/ksync-event-adapter" "$STAGE/bin/"
chmod 700 "$STAGE/bin/"*
cp "$PACKAGE/install/"*.sh "$STAGE/install/"
chmod 700 "$STAGE/install/"*.sh
cp "$PACKAGE/manifest.json" "$STAGE/package-manifest.json"
cp "$PACKAGE/upstart/kindle-native-sync.conf" "$PACKAGE/upstart/kindle-native-sync-events.conf" "$STAGE/upstart/"
if [ -f "$PACKAGE/ca-bundle.crt" ];then cp "$PACKAGE/ca-bundle.crt" "$STAGE/ca-bundle.crt";chmod 600 "$STAGE/ca-bundle.crt";fi
# Nothing active has been replaced before this point; the complete old set exists.
root_rw
services_touched=1
stop_our_jobs
files_changed=1
# Remove and persist active acceptance BEFORE replacing any binary. This is not
# automatic power-loss recovery, but prevents new binaries using old acceptance.
rm -f "$ROOT/validation-device.json"
sync
rm -rf "$ROOT/bin" "$ROOT/install"
mv "$STAGE/bin" "$ROOT/bin"
mv "$STAGE/install" "$ROOT/install"
mv "$STAGE/package-manifest.json" "$ROOT/package-manifest.json"
if [ -f "$STAGE/ca-bundle.crt" ];then mv "$STAGE/ca-bundle.crt" "$ROOT/ca-bundle.crt";fi
# New binaries must earn fresh acceptance; old proof remains in the old set.
for job in "$COLLECTOR_JOB" "$EVENTS_JOB";do
    cp "$STAGE/upstart/$job.conf" "$JOBS/.$job.conf.new"
    chmod 644 "$JOBS/.$job.conf.new"
    mv "$JOBS/.$job.conf.new" "$JOBS/$job.conf"
done
root_ro
initctl reload-configuration
ensure_running "$COLLECTOR_JOB"
ensure_running "$EVENTS_JOB"
job_running "$COLLECTOR_JOB" && job_running "$EVENTS_JOB"
rm -rf "$STAGE"
# An empty first-install backup has no previous components to restore.
if [ "$old_components" = 0 ];then rm -rf "$BACKUP";fi
rm -rf "$LOCK"
transaction_active=0
printf 'Installed: both project jobs are running in collection-only mode. New device validation is required; private ledger/auth/config retained.\n'
