#!/bin/sh
# Read-only root compatibility probe; stdout is sanitized JSON.
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
free=$(space_kib);case "$free" in ''|*[!0-9]*) free=0;; esac
supported=false
if [ "$firmware" = 5.12.2.1.1 ] && [ "$prefix" = B0D4 ] && [ "$machine" = armv7l ] && [ "$is_root" = true ] && [ "$upstart" = true ] && [ "$lipc" = true ] && [ "$gzip" = true ] && [ "$free" -ge 65536 ]; then supported=true;fi
printf '{"schema_version":1,"profile":"%s","firmware":"%s","model_prefix":"%s","machine":"%s","kernel":"%s","libc":"%s","root":%s,"upstart":%s,"lipc":%s,"gzip":%s,"var_local_free_kib":%s,"supported":%s}\n' "$PROFILE" "$firmware" "$prefix" "$machine" "$kernel" "$libc" "$is_root" "$upstart" "$lipc" "$gzip" "$free" "$supported"
