#!/bin/sh
# Transactional removal of this project's runtime only; private data is retained.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
. "$HERE/transaction.sh"
require_device
BACKUP="$ROOT/.uninstall-backup"
# Do not overwrite an active or incomplete upgrade's recovery evidence.
[ ! -e "$ROOT/.install-transaction" ] && [ ! -L "$ROOT/.install-transaction" ] || { echo 'Resolve the previous transaction before uninstalling.' >&2;exit 1; }
# Completed historical upgrade backups are removed only after uninstall succeeds.
begin_transaction
root_rw
services_touched=1
stop_our_jobs
files_changed=1
for job in "$COLLECTOR_JOB" "$EVENTS_JOB";do rm -f "$JOBS/$job.conf" "$JOBS/.$job.conf.new";done
root_ro
initctl reload-configuration
! job_running "$COLLECTOR_JOB" && ! job_running "$EVENTS_JOB"
# All rollback-sensitive operations have passed before deleting current executables.
rm -rf "$ROOT/bin" "$ROOT/install"
rm -f "$ROOT/package-manifest.json" "$ROOT/ca-bundle.crt"
remove_old_temporaries
rm -rf "$ROOT/upgrade.previous" "$BACKUP" "$LOCK"
rm -f "$ROOT/disabled"
transaction_active=0
printf 'Removed project jobs/executables/backups/staging. Private auth/config/ledger/data and device evidence remain; disk usage is not fully reclaimed.\n'
