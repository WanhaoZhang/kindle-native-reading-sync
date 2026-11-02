#!/bin/sh
# Source-only transaction primitives. ROOT/common.sh are loaded by the caller.
# Nothing below reads, replaces or deletes auth/config/ledger/data files.
JOBS=/etc/upstart
COLLECTOR_JOB=kindle-native-sync
EVENTS_JOB=kindle-native-sync-events
COMPONENTS='bin install package-manifest.json ca-bundle.crt validation-device.json'
transaction_active=0
files_changed=0
services_touched=0
old_collector=0
old_events=0
old_components=0
STAGE="$ROOT/.install-stage"
LOCK="$ROOT/.install-transaction"
BACKUP="$ROOT/upgrade.previous"

job_running() {
    status "$1" 2>/dev/null | grep -q 'start/running'
}
ensure_running() {
    if job_running "$1";then return 0;fi
    # Upstart dependency events may have started the adapter already.
    start "$1" >/dev/null 2>&1 || job_running "$1" || return 1
    job_running "$1"
}
stop_our_jobs() {
    stop "$EVENTS_JOB" >/dev/null 2>&1 || :
    stop "$COLLECTOR_JOB" >/dev/null 2>&1 || :
    ! job_running "$EVENTS_JOB" && ! job_running "$COLLECTOR_JOB"
}
check_component_links() {
    for name in $COMPONENTS;do
        [ ! -L "$ROOT/$name" ] || { echo 'Project component must not be a symlink.' >&2;return 1; }
    done
}
backup_old_set() {
    mkdir "$BACKUP" "$BACKUP/jobs"
    for name in $COMPONENTS;do
        if [ -e "$ROOT/$name" ];then
            cp -pR "$ROOT/$name" "$BACKUP/$name"
            old_components=1
        fi
    done
    for job in "$COLLECTOR_JOB" "$EVENTS_JOB";do
        [ ! -L "$JOBS/$job.conf" ] || return 1
        if [ -f "$JOBS/$job.conf" ];then cp -p "$JOBS/$job.conf" "$BACKUP/jobs/$job.conf";old_components=1;fi
    done
    if job_running "$COLLECTOR_JOB";then old_collector=1;fi
    if job_running "$EVENTS_JOB";then old_events=1;fi
    printf 'old_collector=%s\nold_events=%s\n' "$old_collector" "$old_events" > "$BACKUP/service-state.txt"
    : > "$BACKUP/complete"
}
restore_old_set() {
    # Called with errexit disabled; every failure preserves backup+lock for review.
    recovered=1
    if [ "$services_touched" = 1 ];then
        stop_our_jobs || { root_ro || :;echo 'Cannot stop project jobs; leaving recovery set untouched.' >&2;return 1; }
    fi
    if [ "$files_changed" = 1 ];then
        [ -f "$BACKUP/complete" ] || { root_ro || :;echo 'Incomplete recovery set; refusing guessed recovery.' >&2;return 1; }
        root_rw || recovered=0
        if [ "$recovered" = 1 ];then
            for name in $COMPONENTS;do
                rm -rf "$ROOT/$name" || recovered=0
                if [ -e "$BACKUP/$name" ];then cp -pR "$BACKUP/$name" "$ROOT/$name" || recovered=0;fi
            done
            for job in "$COLLECTOR_JOB" "$EVENTS_JOB";do
                rm -f "$JOBS/$job.conf" "$JOBS/.$job.conf.new" || recovered=0
                if [ -f "$BACKUP/jobs/$job.conf" ];then cp -p "$BACKUP/jobs/$job.conf" "$JOBS/$job.conf" || recovered=0;fi
            done
        fi
    fi
    root_ro || recovered=0
    if [ "$services_touched" = 1 ] && [ "$recovered" = 1 ];then
        initctl reload-configuration >/dev/null 2>&1 || recovered=0
        if [ "$old_collector" = 1 ];then ensure_running "$COLLECTOR_JOB" || recovered=0;fi
        if [ "$old_events" = 1 ];then ensure_running "$EVENTS_JOB" || recovered=0
        else stop "$EVENTS_JOB" >/dev/null 2>&1 || :;fi
        if [ "$old_collector" = 0 ];then stop "$COLLECTOR_JOB" >/dev/null 2>&1 || :;fi
        if [ "$old_collector" = 1 ];then job_running "$COLLECTOR_JOB" || recovered=0
        else ! job_running "$COLLECTOR_JOB" || recovered=0;fi
        if [ "$old_events" = 1 ];then job_running "$EVENTS_JOB" || recovered=0
        else ! job_running "$EVENTS_JOB" || recovered=0;fi
    fi
    if [ "$recovered" = 1 ];then
        rm -rf "$STAGE" "$BACKUP" "$LOCK"
        transaction_active=0
        echo 'Previous project components and service state restored; private data unchanged.' >&2
        return 0
    fi
    echo 'Recovery incomplete. Backup and transaction marker retained; do not retry blindly.' >&2
    return 1
}
finish_transaction() {
    code=$?
    trap - EXIT HUP INT TERM
    if [ "$transaction_active" = 1 ];then
        set +e
        restore_old_set || code=1
    fi
    exit "$code"
}
begin_transaction() {
    umask 077
    mkdir -p "$ROOT"
    [ ! -e "$LOCK" ] && [ ! -L "$LOCK" ] || { echo 'Unresolved project transaction exists.' >&2;return 1; }
    [ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ] || { echo 'Existing recovery set must be reviewed before another transaction.' >&2;return 1; }
    check_component_links
    mkdir "$LOCK"
    transaction_active=1
    trap finish_transaction EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    backup_old_set
    # Persist the complete recovery copy before any active component is changed.
    sync
}
remove_old_temporaries() {
    # Exact project-owned reserved names only. Never remove ROOT itself.
    rm -rf "$ROOT/bin.next" "$ROOT/bin.previous" "$ROOT/.install-stage"
}
