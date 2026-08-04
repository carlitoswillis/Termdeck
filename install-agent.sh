#!/bin/zsh
# Install (or reinstall) termdeck as a LaunchAgent so it starts at login.
set -e
cd "$(dirname "$0")"

DIR="$PWD"
LABEL=com.carlitos.termdeck
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -x .venv/bin/python ]]; then
  echo "no .venv yet — run ./setup.sh first" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" logs
sed "s|__TERMDECK_DIR__|$DIR|g" "$LABEL.plist.template" > "$PLIST"

# Ignore the error when it isn't loaded yet.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed $PLIST"
echo "stop with: launchctl bootout gui/\$(id -u)/$LABEL"
echo "logs: $DIR/logs/termdeck.log and $DIR/logs/termdeck.err.log"
