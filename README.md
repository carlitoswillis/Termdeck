# termdeck

View and type into your local iTerm2 sessions from your phone (or any device)
over Tailscale. No tmux required — it talks to iTerm2's Python API, so it sees
the exact windows/tabs/panes you already have open, scrollback included.

## One-time setup

1. In iTerm2: **Settings → General → Magic → Enable Python API** (on 3.4 this
   may be under **Settings → API**). Takes effect immediately, no restart.
   If iTerm2 asks whether to allow the connection, approve it.
2. `./setup.sh` — creates `.venv` and installs `requirements.txt`
   (aiohttp + iterm2). Re-run it any time to update dependencies.

The server retries until the API is reachable, so the order doesn't matter.

## URLs

- On the Mac: http://localhost:7717
- On your phone (Tailscale on): http://100.72.206.61:7717
  (or http://<mac-tailscale-name>:7717 if MagicDNS is on)

The server binds only to localhost and the Tailscale interface — never LAN/public.

## Running

- Foreground: `./run.sh`
- As a LaunchAgent (auto-start at login, restarts on crash): `./install-agent.sh`
  — renders `com.carlitos.termdeck.plist.template` with this checkout's path
  into `~/Library/LaunchAgents` and loads it. Safe to re-run after moving the
  repo.
  Stop: `launchctl bootout gui/$(id -u)/com.carlitos.termdeck`
- Logs: `logs/termdeck.log` and `logs/termdeck.err.log`

Only one copy can hold port 7717. If startup says the address is in use, the
LaunchAgent is probably already running — stop it before using `./run.sh`.

## Tests

```
./setup.sh requirements-dev.txt
./.venv/bin/python -m pytest
```

They run against a fake iTerm2 layer, so no Mac or running iTerm2 is needed.

## How it works

- `server.py` — aiohttp server on port 7717.
  - `GET /api/sessions` — all iTerm2 windows/tabs/panes with titles/jobs/paths
  - `GET /api/ws` — websocket: polls the watched session's last 400 lines
    (scrollback + screen) every 400 ms, pushes on change; accepts
    `{type:"input", text}` and `{type:"key", name}` to type into the session
  - `GET /api/scrollback` — older history for "Load earlier"
- `static/index.html` — the phone UI: session list → live terminal view with
  key row (Esc/Tab/arrows/^C…), Send box (appends Enter), font size, wrap,
  load-earlier.

Line reads return `{first, start, total, lines}`. iTerm2 numbers lines from the
start of the session and drops the oldest once scrollback fills up, so `first`
(its `overflow`) is the oldest line still available — not zero — and the UI
stitches new frames onto its history by absolute line number, refetching
anything that scrolled past while it wasn't looking.

Send is delivered via iTerm2's `async_send_text`, exactly as if typed locally.
