#!/bin/zsh
# One command: install dependencies, start termdeck, and keep it running.
# Uses pm2 when it's installed, otherwise a launchd LaunchAgent.
# Safe to re-run — it reinstalls over whatever is already there.
#
#   ./install.sh            pm2 if available, else launchd
#   ./install.sh --launchd  force the LaunchAgent
#   ./install.sh --pm2      require pm2
#   ./install.sh --dev      also install the test dependencies
set -e
cd "$(dirname "$0")"

DIR="$PWD"
LABEL=com.carlitos.termdeck
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY=${PYTHON:-python3}

supervisor=auto
dev=no
for arg in "$@"; do
  case "$arg" in
    --pm2)     supervisor=pm2 ;;
    --launchd) supervisor=launchd ;;
    --dev)     dev=yes ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
if [[ "$supervisor" == auto ]]; then
  if command -v pm2 >/dev/null 2>&1; then supervisor=pm2; else supervisor=launchd; fi
fi
if [[ "$supervisor" == pm2 ]] && ! command -v pm2 >/dev/null 2>&1; then
  echo "pm2 isn't on PATH — install it (npm i -g pm2) or use --launchd" >&2
  exit 1
fi

# ---------------------------------------------------------------- dependencies
if [[ ! -x .venv/bin/python ]]; then
  echo "creating .venv with $PY"
  "$PY" -m venv .venv
fi
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt
if [[ "$dev" == yes ]]; then
  ./.venv/bin/python -m pip install --quiet -r requirements-dev.txt
fi
mkdir -p logs

# ------------------------------------------------------------------ supervisor
# Only one of them can hold port 7717, so clear the other one out first.
if [[ "$supervisor" == pm2 ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  pm2 startOrRestart ecosystem.config.js --update-env
  pm2 save >/dev/null
  stop_cmd="pm2 stop termdeck"
  logs_cmd="pm2 logs termdeck"
else
  if command -v pm2 >/dev/null 2>&1; then
    pm2 delete termdeck >/dev/null 2>&1 || true
  fi
  mkdir -p "$HOME/Library/LaunchAgents"
  sed "s|__TERMDECK_DIR__|$DIR|g" "$LABEL.plist.template" > "$PLIST"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  stop_cmd="launchctl bootout gui/\$(id -u)/$LABEL"
  logs_cmd="tail -f $DIR/logs/termdeck.err.log"
fi

# ------------------------------------------------------------------- report in
sleep 1
if curl -sf -o /dev/null --max-time 3 http://127.0.0.1:7717/; then
  echo "termdeck is running under $supervisor:"
  echo "  http://localhost:7717"
  for cli in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
    ip=$("$cli" ip -4 2>/dev/null | head -1) || true
    if [[ "$ip" == 100.* ]]; then
      echo "  http://$ip:7717   (from your phone, with Tailscale on)"
      break
    fi
  done
else
  echo "started under $supervisor, but it isn't answering yet — check logs/termdeck.err.log" >&2
fi

echo
echo "enable iTerm2's API if you haven't: Settings -> General -> Magic -> Enable Python API"
echo "logs: $logs_cmd"
echo "stop: $stop_cmd"
if [[ "$supervisor" == pm2 ]]; then
  echo
  echo "to bring pm2 itself back after a reboot, once: pm2 startup"
  echo "(it prints a command to run; this install already did the pm2 save)"
fi
