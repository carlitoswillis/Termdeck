#!/bin/zsh
# One command: install dependencies, start termdeck, and keep it starting at
# login. Safe to re-run — it reinstalls over whatever is already there.
#
#   ./install.sh          runtime only
#   ./install.sh --dev    also install the test dependencies
set -e
cd "$(dirname "$0")"

DIR="$PWD"
LABEL=com.carlitos.termdeck
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY=${PYTHON:-python3}

if [[ ! -x .venv/bin/python ]]; then
  echo "creating .venv with $PY"
  "$PY" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
if [[ "$1" == "--dev" ]]; then
  ./.venv/bin/python -m pip install --quiet -r requirements-dev.txt
fi

mkdir -p logs "$HOME/Library/LaunchAgents"
sed "s|__TERMDECK_DIR__|$DIR|g" "$LABEL.plist.template" > "$PLIST"

# Ignore the error when it isn't loaded yet.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

sleep 1
if curl -sf -o /dev/null --max-time 3 http://127.0.0.1:7717/; then
  echo "termdeck is running:"
  echo "  http://localhost:7717"
  for cli in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    ip=$("$cli" ip -4 2>/dev/null | head -1) || true
    if [[ "$ip" == 100.* ]]; then
      echo "  http://$ip:7717   (from your phone, with Tailscale on)"
      break
    fi
  done
else
  echo "started, but it isn't answering yet — check logs/termdeck.err.log" >&2
fi

echo
echo "enable iTerm2's API if you haven't: Settings -> General -> Magic -> Enable Python API"
echo "stop:      launchctl bootout gui/\$(id -u)/$LABEL"
echo "uninstall: launchctl bootout gui/\$(id -u)/$LABEL && rm $PLIST"
