#!/bin/zsh
# Run termdeck in the foreground.
cd "$(dirname "$0")"
exec ./.venv/bin/python server.py
