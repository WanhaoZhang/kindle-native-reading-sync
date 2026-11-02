#!/bin/sh
# Read-only root compatibility probe; stdout is sanitized JSON, never a serial.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
firmware=unknown;firmware_ok && firmware=5.12.2.1.1
machine=$(uname -m | tr -cd 'A-Za-z0-9._-')
kernel=$(uname -r | tr -cd 'A-Za-z0-9._-')
prefix=unknown
if [ -r /proc/usid ]; then prefix=$(dd if=/proc/usid bs=1 count=4 2>/dev/null | tr -cd 'A-Za-z0-9'); fi
is_root=false;[ "$(id -u)" = 0 ] && is_root=true
upstart=false;command -v initctl >/dev/null 2>&1 && upstart=true
lipc=false;command -v lipc-get-prop >/dev/null 2>&1 && command -v lipc-wait-event >/dev/null 2>&1 && lipc=true
gzip=false;command -v gzip >/dev/null 2>&1 && gzip=true
libc=unknown
[ ! -f /lib/libc.so.6 ] || libc=$(ls -l /lib/libc.so.6 | sed 's|.* -> ||' | tr -cd 'A-Za-z0-9._-')
# Numeric fields are validated independently; never interpolate df or proc text.
set -- $(df -Pk /var/local | awk 'END {print $2,$3,$4}')
total=${1:-0};used=${2:-0};free=${3:-0}
case "$total" in ''|*[!0-9]*) total=0;; esac
case "$used" in ''|*[!0-9]*) used=0;; esac
case "$free" in ''|*[!0-9]*) free=0;; esac
memory=$(awk '/^MemTotal:/ {if ($2 ~ /^[0-9]+$/) print $2; exit}' /proc/meminfo 2>/dev/null || :)
case "$memory" in ''|*[!0-9]*) memory=0;; esac
clock_unix=$(date +%s 2>/dev/null || :)
case "$clock_unix" in ''|*[!0-9]*) clock_unix=0;; esac
clock_local=$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null | tr -cd '0-9T:+Z-')
clock_utc=$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null | tr -cd '0-9T:+Z-')
# The last '/' mount is the real root mount after the kernel rootfs entry.
root_flags=$(awk '$2=="/" {flags=$4} END {print flags}' /proc/mounts 2>/dev/null || :)
root_mount_state=unknown
case ",$root_flags," in *,ro,*) root_mount_state=read_only;; *,rw,*) root_mount_state=read_write;; esac
# Only fixed public state properties; never read network profiles, SSIDs or secrets.
power_state=$(lipc-get-prop com.lab126.powerd state 2>/dev/null || :)
case "$power_state" in active|screenSaver|readyToSuspend|suspend|suspended|suspending|sleep|resuming|hibernate) :;; *) power_state=unknown;; esac
wifi_state=$(lipc-get-prop com.lab126.wifid cmState 2>/dev/null || :)
case "$wifi_state" in CONNECTED|PENDING|READY|NA|DISCONNECTED|DISABLED|UNAVAILABLE) :;; *) wifi_state=unknown;; esac
usb_drive_mode=$(lipc-get-prop com.lab126.volumd driveModeState 2>/dev/null || :)
case "$usb_drive_mode" in 0|1|2|3|4) :;; *) usb_drive_mode=unknown;; esac
userstore_available=$(lipc-get-prop com.lab126.volumd userstoreIsAvailable 2>/dev/null || :)
case "$userstore_available" in 0|1) :;; *) userstore_available=unknown;; esac
hardware_supported=false
if [ "$firmware" = 5.12.2.1.1 ] && [ "$prefix" = B0D4 ] && [ "$machine" = armv7l ] && [ "$is_root" = true ] && [ "$upstart" = true ] && [ "$lipc" = true ] && [ "$gzip" = true ]; then hardware_supported=true;fi
ksync_storage_budget "$(dirname "$HERE")" "$free" || :
supported=false
installation_supported=false
if [ "$hardware_supported" = true ] && [ "$storage_budget_valid" = true ] && [ "$raw_queue_over_limit" = false ];then
    [ "$free" -lt "$required_runtime_free_kib" ] || supported=true
    [ "$free" -lt "$required_install_free_kib" ] || installation_supported=true
fi
printf '{"schema_version":1,"profile":"%s","firmware":"%s","model_prefix":"%s","machine":"%s","kernel":"%s","libc":"%s","root":%s,"upstart":%s,"lipc":%s,"gzip":%s,"var_local_total_kib":%s,"var_local_used_kib":%s,"var_local_free_kib":%s,"mem_total_kib":%s,"clock_unix":%s,"clock_local":"%s","clock_utc":"%s","root_mount_state":"%s","power_state":"%s","wifi_state":"%s","usb_drive_mode":"%s","userstore_available":"%s","hardware_supported":%s,"supported":%s,"installation_supported":%s,"storage_budget_valid":%s,"storage_budget_blocked":"%s","ledger_current_bytes":%s,"raw_queue_over_limit":%s,"required_runtime_free_kib":%s,"required_install_free_kib":%s,"install_peak_extra_kib":%s,"retained_backup_kib":%s,"storage_profile":{"name":"%s","raw_queue_max_kib":%s,"ledger_max_kib":%s,"sqlite_rollback_max_kib":%s,"sqlite_journal_overhead_kib":%s,"system_reserve_kib":%s,"metadata_margin_kib":%s}}\n' "$PROFILE" "$firmware" "$prefix" "$machine" "$kernel" "$libc" "$is_root" "$upstart" "$lipc" "$gzip" "$total" "$used" "$free" "$memory" "$clock_unix" "$clock_local" "$clock_utc" "$root_mount_state" "$power_state" "$wifi_state" "$usb_drive_mode" "$userstore_available" "$hardware_supported" "$supported" "$installation_supported" "$storage_budget_valid" "$storage_budget_blocked" "$ledger_current_bytes" "$raw_queue_over_limit" "$required_runtime_free_kib" "$required_install_free_kib" "$install_peak_extra_kib" "$retained_backup_kib" "$KSYNC_STORAGE_PROFILE" "$KSYNC_RAW_MAX_KIB" "$KSYNC_LEDGER_MAX_KIB" "$KSYNC_JOURNAL_MAX_KIB" "$KSYNC_JOURNAL_OVERHEAD_KIB" "$KSYNC_SYSTEM_RESERVE_KIB" "$KSYNC_METADATA_MARGIN_KIB"
