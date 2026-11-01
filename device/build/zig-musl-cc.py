#!/usr/bin/env python3
"""ARM hard-ABI musl wrapper for cc-rs / bundled SQLite; no host ABI mixing."""
import os
from pathlib import Path
import sys
zig=os.environ.get('KSYNC_ZIG')
if not zig or not Path(zig).is_file() or not os.access(zig,os.X_OK):
    raise SystemExit('Set KSYNC_ZIG to a verified Zig 0.13.0 executable.')
# cc-rs injects a Rust triple unsupported by Zig 0.13. Remove only this known
# equivalent target; retain all other flags and let unexpected targets fail.
args=[a for a in sys.argv[1:] if a!='--target=arm-unknown-linux-musleabihf']
os.execv(zig,[zig,'cc','-target','arm-linux-musleabihf','-mcpu=cortex_a9',*args])
