#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ is required"'
if ! command -v cargo >/dev/null 2>&1; then
    echo 'Rust/cargo 未找到。请安装 https://rustup.rs/，重新打开终端后再运行。' >&2
    exit 1
fi
python3 -m venv .venv-bridge
.venv-bridge/bin/python -m pip install -r requirements.txt
cargo +1.91.0 build --locked --manifest-path readinglog-cli/Cargo.toml
printf '\n安装完成。运行 ./启动阅读同步.command 打开本地网页。\n'
