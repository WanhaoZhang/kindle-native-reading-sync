#!/bin/sh
set -eu
: "${KSYNC_ZIG:?Set KSYNC_ZIG to a verified Zig 0.13.0 executable}"
exec "$KSYNC_ZIG" ar "$@"
