# termdeck

View and type into your local iTerm2 sessions from your phone (or any device)
over Tailscale. No tmux required — it talks to iTerm2's Python API, so it sees
the exact windows/tabs/panes you already have open, scrollback included.

## Install

```
./install.sh
```

Creates `.venv`, installs the dependencies, and hands the process to a
supervisor — **pm2** if you have it, otherwise a launchd LaunchAgent — so
termdeck is running now and comes back on its own. It prints the URLs when
it's up. Re-run it any time; it reinstalls over itself, including after moving
the repo somewhere else.

Force either one with `./install.sh --pm2` or `./install.sh --launchd`. Both
want port 7717, so whichever you pick, the installer clears the other out
first. Under pm2 the app is `termdeck` (`pm2 logs termdeck`, `pm2 restart
termdeck`, `pm2 monit`), configured by `ecosystem.config.js`, which derives
every path from its own location. `pm2 save` runs for you; to have pm2 itself
survive a reboot, run `pm2 startup` once and follow the command it prints —
that part needs your privileges, so the installer won't do it behind your back.

The one thing it can't do for you: in iTerm2, turn on **Settings → General →
Magic → Enable Python API** (on 3.4 it may be under **Settings → API**). Takes
effect immediately, no restart, and the server retries until it's reachable, so
the order doesn't matter. Approve the connection if iTerm2 asks.

## Start

```
./run.sh
```

Runs it in the foreground (and installs dependencies on first run, so this
works on its own from a fresh clone). After `./install.sh` you don't need this
— it's already running.

Only one copy can hold port 7717, so if startup says the address is in use,
stop the supervised copy first: `pm2 stop termdeck`, or
`launchctl bootout gui/$(id -u)/com.carlitos.termdeck`. Either way the logs
land in `logs/termdeck.log` and `logs/termdeck.err.log`.

## URLs

- On the Mac: http://localhost:7717
- On your phone (Tailscale on): http://100.72.206.61:7717
  (or http://<mac-tailscale-name>:7717 if MagicDNS is on)

The server binds only to localhost and the Tailscale interface — never LAN/public.

## Tests

```
./install.sh --dev
./.venv/bin/python -m pytest
```

They run against a fake iTerm2 layer, so no Mac or running iTerm2 is needed.

## How it works

- `server.py` — aiohttp server on port 7717.
  - `GET /api/sessions` — all iTerm2 windows/tabs/panes with titles/jobs/paths
  - `GET /api/ws` — websocket: polls the watched session's last 400 lines
    (scrollback + screen) every 400 ms, pushes on change; accepts
    `{type:"input", text}` and `{type:"key", name}` to type into the session,
    and `{type:"activate"}` to bring it to the front on the Mac
  - `GET /api/scrollback` — older history for "Load earlier"
- `static/index.html` — the phone UI: session list → live terminal view with
  key row (Esc/Tab/arrows/^C…), Send box (appends Enter), font size, wrap,
  load-earlier, follow-focus.

## Following your phone's focus

The **⤒** button in the terminal bar (on by default, remembered per device)
makes the Mac follow what you're looking at: open a session on your phone and
that pane is selected, its tab selected, its window raised, and iTerm2 brought
to the foreground. It fires when you deliberately open a session — not when the
page silently reconnects — so putting your phone down doesn't shuffle windows
around. Turn it off and the phone becomes a pure read/write viewer that never
disturbs the Mac; turning it back on brings the current session forward
immediately, which doubles as a one-shot.

Line reads return `{first, start, total, lines}`. iTerm2 numbers lines from the
start of the session and drops the oldest once scrollback fills up, so `first`
(its `overflow`) is the oldest line still available — not zero — and the UI
stitches new frames onto its history by absolute line number, refetching
anything that scrolled past while it wasn't looking.

Send is delivered via iTerm2's `async_send_text`, exactly as if typed locally.
