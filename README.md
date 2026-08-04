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

## Who can reach it

Two locks, because "it only binds to these addresses" is a fact about how it
started, not a rule anything enforces — a subnet router, a port forward or a
proxy in front could all widen that without touching this code.

- **Address allowlist.** Requests are refused unless they come from loopback
  or the tailnet (`100.64.0.0/10`). `--lan` adds your Wi-Fi network and also
  binds the Mac's LAN address, for reaching it without Tailscale. Nothing
  binds a public address, ever.
- **A password**, if you set one:

  ```
  ./.venv/bin/python server.py --set-password
  pm2 restart termdeck
  ```

  You get a login page, once per device, and then a `HttpOnly`,
  `SameSite=Strict` cookie remembers it. Only a salted PBKDF2 hash is stored
  (`.termdeck-password`, gitignored, mode 600) — never the password itself.
  Websockets can't send an `Authorization` header, which is why it's a cookie:
  the handshake is refused without one. `TERMDECK_PASSWORD` in the environment
  works too, for pm2 configs.

  **No password set means no login screen.** Being locked out of the thing you
  reach your Mac with is its own kind of outage, so it never appears
  uninvited — but until you set one, every device on your tailnet can type
  into your terminal, including any node someone else shares in.

  `--no-auth` ignores a password you've set. Deleting `.termdeck-session`
  signs every device out at once without changing the password.

## Surviving sleep

Sleep doesn't kill the process, so a supervisor has nothing to restart — but
two things can quietly break underneath it, and both heal themselves:

- **The tailnet address changes.** The socket bound to the old one still
  exists and nothing can reach it. The server rechecks every 30s and moves the
  binding, logging when it does; that also covers starting before Tailscale is
  up.
- **The iTerm2 API connection drops.** Any failed call resets the connection
  and the next poll reconnects, so the phone reconnects on its own.

## Updating

`git pull` on its own restarts nothing — the running process already holds the
old code. pm2 watches `server.py`, so a pull that changes it restarts termdeck
within a second; `static/` needs no restart because those files are read from
disk per request (just refresh the page). Two things the watcher can't do for
you:

- **`ecosystem.config.js` changed** — pm2 doesn't re-read its own config on
  restart. Re-run `./install.sh` (or `pm2 startOrRestart ecosystem.config.js
  --update-env`).
- **`requirements.txt` changed** — re-run `./install.sh` to install them.

Under launchd there's no watcher at all: `launchctl kickstart -k
gui/$(id -u)/com.carlitos.termdeck` after a pull, or just `./install.sh`.

To make pm2 itself survive a reboot, run these once:

```
pm2 startup      # prints a command — run that command
pm2 save         # ./install.sh already does this
```

`pm2 startup` needs your privileges, which is why the installer prints it
instead of running it. One caveat if node came from nvm: the launchd entry
`pm2 startup` writes points at the node binary in use at that moment, so
switching node versions later can break resurrection. Re-run `pm2 startup`
after switching, or install pm2 on a system node.

## Tests

```
./install.sh --dev
./.venv/bin/python -m pytest
```

They run against a fake iTerm2 layer, so no Mac or running iTerm2 is needed.

## How it works

- `server.py` — aiohttp server on port 7717.
  - `GET /api/sessions` — all iTerm2 windows/tabs/panes with titles/jobs/paths
  - `GET /api/ws` — websocket: pushes the watched session's last 400 lines
    (scrollback + screen) whenever iTerm2 says the screen changed; accepts
    `{type:"input", text}` and `{type:"key", name}` to type into the session,
    and `{type:"activate"}` to bring it to the front on the Mac
  - `GET /api/scrollback` — older history for "Load earlier"
  - `POST /api/new` — `{kind: "tab"|"window", session_id}` opens one and
    returns the session in it
- `static/index.html` — the phone UI: session list (with **+ Tab** /
  **+ Window**) → live terminal view with key row (Esc/Tab/arrows/^C…), Send
  box (appends Enter), font size, wrap, load-earlier, follow-focus.

**+ Tab** opens beside the session you were last watching — the window on your
phone, not whatever iTerm2 has in front on the Mac — and drops you straight
into it. With no windows left open at all, it opens one.

## Typing

Two modes, because a phone wants both:

- **Compose** (default) — type the whole command, fix what autocorrect did to
  it, then Send, which appends Enter. Safer for anything destructive.
- **Live** (the ⌨ button) — every keypress goes straight through as you type,
  for vim, `less`, y/n prompts and REPLs. Send becomes ⏎. It's deliberately
  never remembered between page loads: compose is the safer thing to wake up
  in.

The key row covers what a phone keyboard simply doesn't have — Esc, Tab,
arrows, PgUp/PgDn. **^** and **⌥** are latches rather than fixed combinations:
tap **^**, then type any key on your own keyboard, and it goes as that control
character. That reaches ^A, ^E, ^K, ^X and everything else, not just the four
that fit on buttons. ⌥ does the same for Alt (sends ESC + key, so ⌥f and ⌥b
work for word-wise movement). The latch releases after one key, and tapping a
key-row button clears it.

Under the hood this watches the input field's value change rather than reading
`keydown`, because software keyboards report almost every key as
`Unidentified` — predictive text intercepts them. An invisible sentinel
character sits in the field so Backspace has something to delete and still
registers on an empty line. The layout is driven by `visualViewport`, so the
terminal shrinks above the on-screen keyboard instead of hiding behind it.

## Fitting it to the screen

Every frame carries the pane's real width in cells, and **⤢** sizes the text so
exactly that many columns fit the screen — not a guess from the longest line
that happens to be on screen, so a narrow split doesn't get treated as if it
were 200 columns wide. It re-fits on rotation and window resize, and turning it
on turns Wrap off, since the two answer the same question different ways.

What that works out to for a 120-column pane:

| screen | fitted size | verdict |
| --- | --- | --- |
| desktop, 1280px | 17px | roomier than the 12px default — leave Fit on |
| phone, landscape | 11px | comfortable; the honest answer for TUIs on a phone |
| phone, portrait | 6px (floor) | still overflows — 120 columns don't fit 390px |

No font trick escapes that last row: 120 columns don't fit a phone held
upright. So **⇲ reshapes the pane itself** — it tells iTerm2 to make the
session the size this screen can actually show. A 120×10 pane becomes about
51×41 on a phone, readable at 12px, and TUIs reflow to match instead of being
cropped.

That changes what's on your Mac, so it's careful about it: tap again to put it
back, and the server restores the original shape by itself when the phone
disconnects — a phone dropping into a tunnel shouldn't leave a pane 51 columns
wide. iTerm2 refuses to resize a session that shares a tab with split panes,
or one in a fullscreen window; you get told which.

Otherwise on a phone: **rotate** for anything with a layout, and use **Wrap**
at a comfortable size for reading logs, where line breaks don't matter.
Pinch-zoom is enabled too — the fastest way to read one dense corner without
changing any setting.

## Knowing which pane needs you

With several panes running, the question isn't what exists, it's which one
wants attention. Each card carries a dot:

| dot | meaning |
| --- | --- |
| amber | still running something — the job name is shown |
| green | **finished**: it was running, now it's back at a prompt |
| blue | printed something since you last looked at it |

"Running" is the foreground job not being a shell, which costs nothing extra —
the list already fetched it. "New output" compares the session's total line
count (which only ever goes up) against what your device saw last time, so the
server keeps no per-device state and two phones don't clear each other's
marks. Opening a session clears its dot.

The cursor is drawn where iTerm2 says it is, which matters most in live
keystroke mode — otherwise you're typing into a text dump with no idea where
the characters are going.

## One URL per session

Every session has its own address — `…:7717/#s=<session id>` — so a refresh
puts you back in the session you were watching instead of the picker. Back and
forward work, and the browser tab is named after the session, so several open
at once stay tellable apart. Save one to your home screen and that shortcut
goes straight to that terminal.

Session ids are what's in the URL because they survive; window and tab numbers
shift as you open and close things. If a saved link's session is gone, you land
on the picker with a note saying so, and a session that dies while you're
watching leaves its last output on screen but drops itself from the URL — so
the next refresh doesn't chase a dead pane.

## Following your phone's focus

The **⤒** button in the terminal bar (on by default, remembered per device)
makes the Mac follow what you're looking at: open a session on your phone and
that pane is selected, its tab selected, its window raised, and iTerm2 brought
to the foreground. It fires when you deliberately open a session — not when the
page silently reconnects — so putting your phone down doesn't shuffle windows
around. Turn it off and the phone becomes a pure read/write viewer that never
disturbs the Mac; turning it back on brings the current session forward
immediately, which doubles as a one-shot.

Updates are pushed, not polled: the server subscribes to iTerm2's screen-update
notification and only re-reads when something actually changed, with a 5s
safety re-read in case a notification goes missing and a fall back to 400ms
polling if the subscription can't be set up at all. An idle session costs
nothing, however many devices are watching it.

Lines are rebuilt cell by cell rather than taken from `LineContents.string`.
That string is only every cell's code points joined together, so a cell that
was never written contributes no characters at all — a line a TUI drew by
moving the cursor instead of printing spaces arrives with its gaps closed up,
`foo   bar` as `foobar`. An empty cell is also how the second half of a
double-width character is described, so only the first kind gets a space put
back. `tools/inspect-lines.py` prints what iTerm2 reports for the current
session, cell by cell, when something still looks wrong.

Lines carry colour as style runs — `[cells, fg, bg, flags]`, where a colour is
a 0-255 palette index, `#rrggbb`, or null for the terminal's default. Unstyled
lines are left out of the payload. Terminal output is untrusted, so every
segment is escaped on the way into the DOM.

Line reads return `{first, start, total, cols, rows, lines, styles}`. iTerm2 numbers lines from the
start of the session and drops the oldest once scrollback fills up, so `first`
(its `overflow`) is the oldest line still available — not zero — and the UI
stitches new frames onto its history by absolute line number, refetching
anything that scrolled past while it wasn't looking.

Send is delivered via iTerm2's `async_send_text`, exactly as if typed locally.
