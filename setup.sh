#!/bin/zsh
# Create the virtualenv termdeck runs from and install its dependencies.
set -e
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
if [[ ! -x .venv/bin/python ]]; then
  echo "creating .venv with $PY"
  "$PY" -m venv .venv
fi

./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r "${1:-requirements.txt}"

echo "ready — start it with ./run.sh"
echo "(iTerm2 -> Settings -> General -> Magic -> Enable Python API must be on)"
