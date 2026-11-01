#!/bin/zsh
set -e
TASK_DIR="${0:A:h}"
exec "$TASK_DIR/.venv-bridge/bin/python" "$TASK_DIR/local_app.py" --open-browser
