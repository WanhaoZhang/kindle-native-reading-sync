#!/usr/bin/env python3
"""Explicit experimental ARM soft-ABI static-musl compiler wrapper; no downloads."""
import os
from pathlib import Path
import sys
zig=os.environ.get('KSYNC_ZIG')
if not zig or not Path(zig).is_file() or not os.access(zig,os.X_OK):
    raise SystemExit('Set KSYNC_ZIG to a verified Zig 0.13.0 executable.')
os.execv(zig,[zig,'cc','-target','arm-linux-musleabi','-mcpu=cortex_a9',*sys.argv[1:]])
