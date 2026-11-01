#!/bin/sh
# Run on a development computer. Never copies anything onto a connected Kindle.
set -eu
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$PROJECT"
PROFILE=${KSYNC_C_PROFILE:-niluje-kindlepw2}
if [ "${1:-}" = --experimental-static-musl ];then PROFILE=experimental-static-musl;shift;fi
[ "$#" -eq 0 ] || { echo 'Usage: build.sh [--experimental-static-musl]' >&2;exit 2; }
if [ -n "${KSYNC_ZIG:-}" ];then
    [ "$("$KSYNC_ZIG" version)" = 0.13.0 ] || { echo 'The reproducible fallback requires verified Zig 0.13.0.' >&2;exit 1; }
    if [ "$PROFILE" = experimental-static-musl ];then KSYNC_CC=${KSYNC_CC:-$PROJECT/device/build/zig-soft-cc.py};fi
    KSYNC_MUSL_CC=${KSYNC_MUSL_CC:-$PROJECT/device/build/zig-musl-cc.py}
    KSYNC_MUSL_AR=${KSYNC_MUSL_AR:-$PROJECT/device/build/zig-ar.sh}
fi
CC=${KSYNC_CC:-arm-kindlepw2-linux-gnueabi-gcc}
MUSL_CC=${KSYNC_MUSL_CC:-arm-linux-musleabihf-gcc}
case "$PROFILE" in
 niluje-kindlepw2) ;;
 experimental-static-musl)
   [ -n "${KSYNC_CC:-}" ] || { echo 'Experimental static musl requires an explicit soft-float target KSYNC_CC wrapper.' >&2;exit 1; }
   echo 'EXPERIMENTAL static musl C build: not a NiLuJe build and not device-validated.' >&2;;
 *) echo 'Unknown KSYNC_C_PROFILE.' >&2;exit 1;;
esac
command -v "$CC" >/dev/null 2>&1 || { echo 'Missing target softfp compiler; set KSYNC_CC. No Kindle package built.' >&2;exit 1; }
command -v "$MUSL_CC" >/dev/null 2>&1 || { echo 'Missing ARM hard-float musl C compiler for bundled SQLite; set KSYNC_MUSL_CC to a target compiler/wrapper. No Kindle package built.' >&2;exit 1; }
command -v cargo >/dev/null 2>&1 || { echo 'Rust cargo is required.' >&2;exit 1; }
OUT=${KSYNC_BUILD_DIR:-device/build/out}
[ ! -L "$OUT" ] || exit 1
mkdir -p "$OUT"
FLAGS='-O2 -std=c11 -Wall -Wextra -Werror -march=armv7-a -mtune=cortex-a9 -mfpu=neon -mfloat-abi=softfp -mthumb'
if [ "$PROFILE" = experimental-static-musl ];then
    # Zig's target/CPU are selected by the wrapper. GCC's -mtune spelling
    # is interpreted differently by the Zig driver and must not override it.
    FLAGS='-O2 -std=c11 -Wall -Wextra -Werror'
fi
# Deliberate splitting of a fixed compiler flag list; compiler paths are always quoted.
"$CC" $FLAGS -static -s device/collector/ksync-collector.c -o "$OUT/ksync-collector"
"$CC" $FLAGS -static -s device/install/ksync-event-adapter.c -o "$OUT/ksync-event-adapter"
export CC_armv7_unknown_linux_musleabihf="$MUSL_CC"
if [ -n "${KSYNC_MUSL_AR:-}" ];then export AR_armv7_unknown_linux_musleabihf="$KSYNC_MUSL_AR";fi
export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_LINKER=rust-lld
export CARGO_TARGET_ARMV7_UNKNOWN_LINUX_MUSLEABIHF_RUSTFLAGS='-C link-self-contained=yes -C target-feature=+crt-static'
cargo +1.91.0 build --release --locked --manifest-path device/worker/Cargo.toml --target armv7-unknown-linux-musleabihf
python3 device/build/package.py --collector "$OUT/ksync-collector" --adapter "$OUT/ksync-event-adapter" --worker device/worker/target/armv7-unknown-linux-musleabihf/release/ksync-worker --out "$OUT/package" --c-profile "$PROFILE"
