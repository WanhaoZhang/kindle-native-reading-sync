#!/bin/sh
# One-shot service-tree counters. No timers, process arguments, logs or credentials.
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$HERE/common.sh"
require_device
job_pid() {
    initctl status "$1" 2>/dev/null | awk '/start\/running/ && $NF ~ /^[0-9]+$/ {print $NF;found=1} END {if (!found) print 0}'
}
collector=$(job_pid kindle-native-sync)
adapter=$(job_pid kindle-native-sync-events)
ticks=$(getconf CLK_TCK 2>/dev/null || :)
case "$ticks" in ''|*[!0-9]*) ticks=0;; esac
uptime=$(awk '{split($1,a,".");if (a[1] ~ /^[0-9]+$/) print a[1];else print 0}' /proc/uptime)
awk -v collector="$collector" -v adapter="$adapter" -v hz="$ticks" -v up="$uptime" '
function selected(p, i) {
    for (i=0; i<128 && p>1; i++) {
        if (p==collector || p==adapter) return 1;
        if (!(p in parent) || parent[p]==p) return 0;
        p=parent[p];
    }
    return 0;
}
{
    p=FILENAME;sub(/\/stat$/,"",p);sub(/^.*\//,"",p);
    if (p !~ /^[0-9]+$/) next;
    sub(/^[0-9]+ \(.*\) /, "");
    if ($2 !~ /^[0-9]+$/ || $12 !~ /^[0-9]+$/ || $13 !~ /^[0-9]+$/ || $20 !~ /^[0-9]+$/) next;
    parent[p]=$2;cpu[p]=$12+$13;started[p]=$20;
}
END {
    printf "{\"schema_version\":1,\"uptime_seconds\":%d,\"clock_ticks_per_second\":%d,\"processes\":[",up,hz;
    count=0;total=0;truncated="false";complete="true";
    for (p in parent) if (selected(p)) {
        if (count>=64) {truncated="true";continue;}
        rss=0;found=0;file="/proc/" p "/status";
        while ((getline line < file)>0) if (line ~ /^VmRSS:[[:space:]]+[0-9]+[[:space:]]+kB/) {split(line,a);rss=a[2];found=1;}
        close(file);
        if (!found) complete="false";
        role=p==collector?"collector":(p==adapter?"event_adapter":"dependent");
        printf "%s{\"pid\":%d,\"parent_pid\":%d,\"start_ticks\":%.0f,\"cpu_ticks\":%.0f,\"rss_kib\":%d,\"role\":\"%s\"}",count?",":"",p,parent[p],started[p],cpu[p],rss,role;
        total+=rss;count++;
    }
    printf "],\"process_count\":%d,\"total_rss_kib\":%d,\"rss_complete\":%s,\"collector_running\":%s,\"adapter_running\":%s,\"truncated\":%s,\"acceptance_created\":false}\n",count,total,complete,(collector in parent)?"true":"false",(adapter in parent)?"true":"false",truncated;
}' /proc/[0-9]*/stat 2>/dev/null
