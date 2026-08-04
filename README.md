# termdeck

View and type into your local iTerm2 sessions from your phone (or any device)
over Tailscale. No tmux required — it talks to iTerm2's Python API, so it sees
the exact windows/tabs/panes you already have open, scrollback included.

## One-time setup

1. In iTerm2: **Settings → General → Magic → Enable Python API** (on 3.4 this
   may be under **Settings → API**). Takes effect immediately, no restart.
   If iTerm2 asks whether to allow the connection, approve it.
2. That's it — the server retries until the API is reachable.

## URLs

- On the Mac: http://localhost:7717
- On your phone (Tailscale on): http://100.72.206.61:7717
  (or http://<mac-tailscale-name>:7717 if MagicDNS is on)

The server binds only to localhost and the Tailscale interface — never LAN/public.

## Running

- Foreground: `./run.sh`
- As a LaunchAgent (auto-start, keeps running):
  ```
  cp com.carlitos.termdeck.plist ~/Library/LaunchAgents/
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.carlitos.termdeck.plist
  ```
  Stop: `launchctl bootout gui/$(id -u)/com.carlitos.termdeck`
- Logs: `logs/termdeck.log` and `logs/termdeck.err.log`

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

Send is delivered via iTerm2's `async_send_text`, exactly as if typed locally.
