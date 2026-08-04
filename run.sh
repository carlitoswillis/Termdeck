#!/bin/zsh
# Run termdeck in the foreground, installing dependencies on first run.
set -e
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "first run: creating .venv"
  ${PYTHON:-python3} -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi

exec ./.venv/bin/python server.py
