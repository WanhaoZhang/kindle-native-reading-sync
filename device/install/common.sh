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

# PW2 private-partition budget, all units KiB. Keep in sync with device worker/
# collector guards: raw 4 MiB, DB 4 MiB, journal 4 MiB + 64 KiB, system 20 MiB.
KSYNC_STORAGE_PROFILE=pw2-private-v1
KSYNC_RAW_MAX_KIB=4096
KSYNC_LEDGER_MAX_KIB=4096
KSYNC_JOURNAL_MAX_KIB=4096
KSYNC_JOURNAL_OVERHEAD_KIB=64
KSYNC_SYSTEM_RESERVE_KIB=20480
KSYNC_METADATA_MARGIN_KIB=1024

ksync_sum_kib() {
    ksync_sum_total=0
    for ksync_entry in "$@";do
        [ ! -L "$ksync_entry" ] || return 1
        if [ -e "$ksync_entry" ];then
            ksync_entry_kib=$(du -sk "$ksync_entry" 2>/dev/null | awk 'NR==1 {print $1}')
            case "$ksync_entry_kib" in ''|*[!0-9]*) return 1;; esac
            [ "$ksync_entry_kib" -le 2147483647 ] || return 1
            ksync_sum_total=$((ksync_sum_total + ksync_entry_kib))
        fi
    done
    printf '%s\n' "$ksync_sum_total"
}

ksync_storage_budget() {
    # PACKAGE may be a USB distribution or installed ROOT (current-version estimate).
    # FREE is optional so a probe can use the exact df sample it reports.
    storage_budget_valid=false
    required_runtime_free_kib=0
    required_install_free_kib=0
    retained_backup_kib=0
    install_peak_extra_kib=0
    ledger_current_bytes=0
    raw_queue_over_limit=false
    storage_budget_blocked=measurement_unavailable
    storage_free_kib=${2:-$(space_kib)}
    case "$storage_free_kib" in ''|*[!0-9]*) return 1;; esac
    [ "$storage_free_kib" -le 2147483647 ] || return 1
    ksync_package=${1:-$ROOT}
    ksync_raw_used=$(ksync_sum_kib "$ROOT/events") || return 1
    ksync_ledger_used=$(ksync_sum_kib "$ROOT/ledger.sqlite" "$ROOT/ledger.sqlite-wal" "$ROOT/ledger.sqlite-shm") || return 1
    if [ -f "$ROOT/ledger.sqlite" ];then
        ledger_current_bytes=$(wc -c < "$ROOT/ledger.sqlite" | tr -d '[:space:]')
        case "$ledger_current_bytes" in ''|*[!0-9]*) return 1;; esac
        if [ "$ledger_current_bytes" -gt "$((KSYNC_LEDGER_MAX_KIB * 1024))" ];then
            storage_budget_blocked=ledger_over_limit
            return 1
        fi
    fi
    # Existing oversize journals are retained, never pruned to fit a new profile.
    # du is deliberately conservative about raw directory allocation/metadata.
    if [ "$ksync_raw_used" -gt "$KSYNC_RAW_MAX_KIB" ];then raw_queue_over_limit=true;fi
    ksync_raw_left=$((KSYNC_RAW_MAX_KIB - ksync_raw_used))
    [ "$ksync_raw_left" -ge 0 ] || ksync_raw_left=0
    ksync_ledger_left=$((KSYNC_LEDGER_MAX_KIB - ksync_ledger_used))
    [ "$ksync_ledger_left" -ge 0 ] || ksync_ledger_left=0
    required_runtime_free_kib=$((ksync_raw_left + ksync_ledger_left + KSYNC_JOURNAL_MAX_KIB + KSYNC_JOURNAL_OVERHEAD_KIB + KSYNC_SYSTEM_RESERVE_KIB + KSYNC_METADATA_MARGIN_KIB))
    # New staging needs real allocation for the complete candidate. The old set
    # is copied before stopping services, so include its full additional copy at
    # the transaction peak rather than assuming the active old files are free.
    ksync_new_kib=$(ksync_sum_kib "$ksync_package/bin" "$ksync_package/install" "$ksync_package/manifest.json" "$ksync_package/upstart" "$ksync_package/ca-bundle.crt") || return 1
    if [ ! -f "$ksync_package/manifest.json" ];then
        ksync_current_manifest=$(ksync_sum_kib "$ksync_package/package-manifest.json") || return 1
        ksync_new_kib=$((ksync_new_kib + ksync_current_manifest))
    fi
    ksync_old_kib=$(ksync_sum_kib "$ROOT/bin" "$ROOT/install" "$ROOT/package-manifest.json" "$ROOT/ca-bundle.crt" "$ROOT/validation-device.json") || return 1
    ksync_job_kib=$(ksync_sum_kib /etc/upstart/kindle-native-sync.conf /etc/upstart/kindle-native-sync-events.conf) || return 1
    retained_backup_kib=$(ksync_sum_kib "$ROOT/upgrade.previous") || return 1
    install_peak_extra_kib=$((ksync_new_kib + ksync_old_kib + ksync_job_kib))
    required_install_free_kib=$((required_runtime_free_kib + install_peak_extra_kib))
    # Existing raw/DB/upgrade.previous space is already deducted by df; do not
    # add it again. Retained previous backups still block another transaction.
    storage_budget_valid=true
    storage_budget_blocked=none
    if [ "$raw_queue_over_limit" = true ];then storage_budget_blocked=raw_queue_over_limit;fi
}
