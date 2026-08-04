#!/usr/bin/env python3
"""termdeck — view and type into your local iTerm2 sessions from any device
on your Tailscale network.

Requires: iTerm2 with the Python API enabled
(iTerm2 -> Settings -> General -> Magic -> "Enable Python API").

Binds only to localhost and the Tailscale interface — never a public IP.
"""
import argparse
import asyncio
import contextlib
import errno
import hashlib
import hmac
import ipaddress
import getpass
import json
import os
import secrets
import socket
import subprocess
import sys
import traceback
import unicodedata
from pathlib import Path

from aiohttp import web, WSMsgType
import iterm2

PORT = 7717
# Bumped whenever the browser needs a server that has caught up. The page
# checks it and says so, because "pull, but the process is still the old one"
# is invisible otherwise: static files are read per request, Python isn't.
VERSION = 15
TAIL_LINES = 400          # lines of scrollback kept in the live view
POLL_SECONDS = 0.4        # fallback poll interval, when iTerm2 won't push
IDLE_SECONDS = 5          # re-read anyway, in case a notification went missing
MIN_FRAME_SECONDS = 0.12  # ceiling on how often a busy session is re-read
REBIND_SECONDS = 30       # how often to recheck the Tailscale address
STATIC = Path(__file__).parent / "static"
PASSWORD_FILE = Path(__file__).parent / ".termdeck-password"
SESSION_FILE = Path(__file__).parent / ".termdeck-session"
COOKIE = "termdeck"

# Two locks, because binding to specific addresses is a property of how it
# started, not a rule anything enforces. A subnet router, a port forward or a
# reverse proxy in front could all widen the audience without touching a line
# of this file.
LOOPBACK_NETS = ["127.0.0.0/8", "::1/128"]
TAILNET_NETS = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]    # Tailscale's range
LAN_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
            "169.254.0.0/16", "fd00::/8", "fe80::/10"]

KEYMAP = {
    "enter": "\r",
    "tab": "\t",
    "shift-tab": "\x1b[Z",
    "esc": "\x1b",
    "backspace": "\x7f",
    "space": " ",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "pgup": "\x1b[5~",
    "pgdn": "\x1b[6~",
    "ctrl-a": "\x01",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-e": "\x05",
    "ctrl-k": "\x0b",
    "ctrl-l": "\x0c",
    "ctrl-r": "\x12",
    "ctrl-u": "\x15",
    "ctrl-w": "\x17",
    "ctrl-z": "\x1a",
}


class Bridge:
    """Holds one connection to iTerm2's API socket, reconnecting on failure."""

    def __init__(self):
        self.conn = None
        self.app_obj = None
        self.lock = asyncio.Lock()
        # iTerm2 transactions are per-connection and cannot nest, so every
        # read that needs one has to take a turn.
        self.txn_lock = asyncio.Lock()

    async def connection(self):
        async with self.lock:
            if self.conn is None:
                self.conn = await iterm2.Connection.async_create()
            return self.conn

    def reset(self):
        conn, self.conn, self.app_obj = self.conn, None, None
        sock = getattr(conn, "websocket", None)
        if sock is not None:
            # Fire and forget: we only care that the old socket goes away.
            asyncio.ensure_future(_close_quietly(sock))

    async def app(self, refresh=False):
        try:
            conn = await self.connection()
            if self.app_obj is None:
                # async_get_app() refreshes the whole app on every call, so
                # cache it and refresh only when we actually need fresh state.
                self.app_obj = await iterm2.async_get_app(conn)
            elif refresh:
                await self.app_obj.async_refresh()
            return self.app_obj
        except Exception:
            self.reset()
            self.app_obj = await iterm2.async_get_app(await self.connection())
            return self.app_obj


async def _close_quietly(sock):
    try:
        await sock.close()
    except Exception:
        pass


bridge = Bridge()


async def get_session(session_id):
    app = await bridge.app()
    session = app.get_session_by_id(session_id)
    if session is None:
        # Could be a tab opened since the last refresh rather than a dead one.
        app = await bridge.app(refresh=True)
        session = app.get_session_by_id(session_id)
    return session


async def session_var(session, name):
    try:
        return await session.async_get_variable(name)
    except Exception:
        return None


# Names that mean "sitting at a prompt" rather than "running something".
SHELLS = {"zsh", "bash", "sh", "fish", "dash", "tcsh", "csh", "ksh", "login"}


async def session_lines(session):
    """How many lines this session has ever produced. It only goes up, so the
    phone can tell what has printed something since it last looked."""
    try:
        info = await session.async_get_line_info()
        return (info.overflow + info.scrollback_buffer_height
                + info.mutable_area_height)
    except Exception:
        return 0


async def window_order(app):
    """Windows in the order a person would count them on screen: left to
    right, top to bottom. iTerm2's own order is creation order, which stops
    matching the desktop the moment anything is moved."""
    placed = []
    for window in app.terminal_windows:
        try:
            frame = await window.async_get_frame()
            # macOS measures y upward from the bottom, so higher y is further
            # up the screen.
            key = (round(frame.origin.x), -round(frame.origin.y))
        except Exception:
            key = (1 << 30, 0)          # unplaceable: leave it at the end
        placed.append((key, window))
    placed.sort(key=lambda pair: pair[0])
    return [window for _, window in placed]


async def window_title(window):
    """What this window says in its title bar, so it can be told apart from
    the others at a glance."""
    try:
        tab = window.current_tab
        session = tab.current_session if tab else None
        if session:
            return await session_var(session, "autoName") or session.name or ""
    except Exception:
        pass
    return ""


async def list_sessions():
    app = await bridge.app(refresh=True)
    windows = []
    for w, window in enumerate(await window_order(app)):
        tabs = []
        for t, tab in enumerate(window.tabs):
            sessions = []
            for session in tab.sessions:
                title = await session_var(session, "autoName") or session.name or "shell"
                job = await session_var(session, "jobName") or ""
                path = await session_var(session, "path") or ""
                tty = await session_var(session, "tty") or ""
                sessions.append({
                    "id": session.session_id,
                    "title": title,
                    "job": job,
                    "path": path,
                    "tty": tty,
                    "lines": await session_lines(session),
                    # A shell as the foreground job means it's back at the
                    # prompt — whatever was running has finished.
                    "busy": bool(job) and job.lstrip("-") not in SHELLS,
                })
            tabs.append({"index": t, "id": tab.tab_id, "sessions": sessions,
                         "active": tab.tab_id == window.current_tab.tab_id
                         if window.current_tab else False})
        windows.append({"index": w, "id": window.window_id, "tabs": tabs,
                        "title": await window_title(window)})
    return windows


def find_window(app, window_id):
    for window in app.terminal_windows:
        if window.window_id == window_id:
            return window
    return None


def find_tab(app, tab_id):
    for window in app.terminal_windows:
        for tab in window.tabs:
            if tab.tab_id == tab_id:
                return tab
    return None


async def read_lines(session, start=None, count=TAIL_LINES):
    """Read `count` lines beginning at absolute line number `start`.

    iTerm2 numbers lines from the beginning of the session and drops the
    oldest ones once scrollback fills up, so the first line still available
    is `info.overflow` — not zero. Asking for anything below that silently
    returns short. `start=None` means "the last `count` lines".

    The line info and the contents are read in one transaction so the
    session can't scroll out from under us in between.
    """
    # An open transaction blocks every iTerm2 API client, so never let a
    # cancelled watcher (viewer switched sessions, socket dropped) abandon one
    # half-finished — shield the read and let it close itself.
    return await asyncio.shield(asyncio.ensure_future(
        _read_lines(session, start, count)))


BOLD, ITALIC, UNDERLINE, INVERSE, FAINT, STRIKE = 1, 2, 4, 8, 16, 32


def encode_color(color):
    """A colour the browser can use: 0-255 for the palette, #rrggbb for true
    colour, None for 'whatever the terminal's default is'."""
    if color is None:
        return None
    if color.is_rgb:
        rgb = color.rgb
        return f"#{rgb.red:02x}{rgb.green:02x}{rgb.blue:02x}"
    if color.is_standard:
        return color.standard
    return None


def encode_style(style):
    if style is None:
        return [None, None, 0]       # a cell nothing has written to
    flags = (BOLD if style.bold else 0) | (ITALIC if style.italic else 0) \
        | (UNDERLINE if style.underline else 0) | (INVERSE if style.inverse else 0) \
        | (FAINT if style.faint else 0) | (STRIKE if style.strikethrough else 0)
    return [encode_color(style.fg_color), encode_color(style.bg_color), flags]


def is_wide(ch):
    return unicodedata.east_asian_width(ch) in ("W", "F")


def blanked(text):
    """The line's actual content, ignoring however its blanks are spelled."""
    return text.replace(" ", "").replace("\x00", "")


def rebuild_line(line):
    """Rebuild one line from its cells, and its style runs alongside.

    `LineContents.string` is nothing but every cell's code points joined up,
    so a cell that was never written contributes *no characters at all*. A
    line a TUI drew by positioning the cursor rather than printing spaces
    therefore arrives with its gaps closed: "foo   bar" comes back "foobar".
    Walking the cells puts the blanks back.

    An empty cell is also how the second half of a double-width character is
    described, and that one must not become a space or every CJK and emoji
    line shifts right.

    Run lengths come out in code units rather than cells, so they still line
    up with the text where one cell holds two (a combining accent) or none.
    """
    pieces, runs = [], []
    current, count, x = None, 0, 0
    while True:
        try:
            piece = line.string_at(x)
        except IndexError:
            break
        if piece and not piece.strip("\x00"):
            # A blank cell iTerm2 filled with NUL rather than a space. It has
            # a code point, so it survives into `string` — and then renders as
            # nothing at all, running the words either side together.
            piece = " " * len(piece)
        elif piece == "":
            previous = pieces[-1] if pieces else ""
            piece = "" if previous and is_wide(previous[-1]) else " "
        style = line.style_at(x)
        if style is not current:
            if count:
                runs.append([count] + encode_style(current))
            current, count = style, 0
        count += len(piece)
        pieces.append(piece)
        x += 1
    if count:
        runs.append([count] + encode_style(current))

    text = "".join(pieces)
    if blanked(text) != blanked(line.string):
        # The walk didn't reproduce what iTerm2 says this line holds. Trust
        # iTerm2 and lose the styling — never the characters.
        return line.string.replace("\x00", " ").rstrip(), []

    # Every unwritten cell to the right of the text just became a space.
    text = text.replace("\x00", " ")
    trimmed = text.rstrip()
    if len(trimmed) < len(text):
        kept, total = [], 0
        for run in runs:
            if total >= len(trimmed):
                break
            length = min(run[0], len(trimmed) - total)
            kept.append([length] + run[1:])
            total += length
        runs, text = kept, trimmed
    return text, runs


def line_runs(line):
    """Style runs for one line, or None when it's plain from end to end."""
    runs = rebuild_line(line)[1]
    if len(runs) == 1 and runs[0][1:] == [None, None, 0]:
        return None          # plain text: don't pay to say so
    return runs or None


async def cursor_position(session):
    """Where the cursor is, as [absolute line, column].

    cursor_coord is relative to the top of the screen, so it needs the count
    of lines above the screen to line up with everything else here.
    """
    try:
        contents = await session.async_get_screen_contents()
        coord = contents.cursor_coord
        return [contents.number_of_lines_above_screen + coord.y, coord.x]
    except Exception:
        return None            # a cursor is never worth losing the text over


# Rebuilding a line walks every cell of it, which is 20ms for a screenful of
# a wide pane — far too much to repeat 30 times a second. Scrollback can't
# change once it has scrolled off the mutable area, so it's only ever built
# once.
_line_cache = {}
CACHE_LIMIT = 4000


def line_payload(lines, session_id=None, first=0, frozen_below=None):
    """Text plus a sparse map of line index -> style runs.

    `frozen_below` is the first line number that can still change; anything
    above it is history and is remembered rather than rebuilt.
    """
    text, styles = [], {}
    for i, line in enumerate(lines):
        number = first + i
        key = (session_id, number)
        cacheable = (session_id is not None and frozen_below is not None
                     and number < frozen_below)
        raw = line.string
        hit = _line_cache.get(key) if cacheable else None
        if hit and hit[0] == raw:
            content, runs = hit[1], hit[2]
        else:
            try:
                content, runs = rebuild_line(line)
            except Exception:
                # Whatever went wrong, the text still has to arrive.
                content, runs = raw, None
            if cacheable:
                if len(_line_cache) > CACHE_LIMIT:
                    _line_cache.clear()
                _line_cache[key] = (raw, content, runs)
        text.append(content)
        if runs and not (len(runs) == 1 and runs[0][1:] == [None, None, 0]):
            styles[str(i)] = runs
    return text, styles


async def _read_lines(session, start, count):
    conn = await bridge.connection()
    async with bridge.txn_lock:
        async with iterm2.Transaction(conn):
            info = await session.async_get_line_info()
            first = info.overflow
            total = first + info.scrollback_buffer_height + info.mutable_area_height
            count = max(0, count)
            begin = total - count if start is None else start
            begin = max(first, min(begin, total))
            count = min(count, total - begin)
            lines = await session.async_get_contents(begin, count) if count else []
            cursor = await cursor_position(session)
    # The pane's real width in cells, so the phone can size text to fit it
    # rather than guessing from the longest line it happens to have.
    grid = getattr(session, "grid_size", None)
    # Only the mutable area can still change; everything above it is history.
    text, styles = line_payload(lines, getattr(session, "session_id", None),
                                begin, total - info.mutable_area_height)
    return {
        "first": first,
        "start": begin,
        "total": total,
        "cols": getattr(grid, "width", 0) or 0,
        "rows": getattr(grid, "height", 0) or 0,
        "cursor": cursor,
        "lines": text,
        "styles": styles,
    }


def window_for_session(app, session_id):
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id == session_id:
                    return window
    return None


async def new_session(kind, near_session_id=None, window_id=None):
    """Open a tab or a window and hand back the session inside it.

    A new tab goes wherever it was asked to: the phone names the window, so
    "which window did that land in?" is never a guess. Failing that it falls
    back to the window of the session last watched, then iTerm2's current
    window, then a new window when there are none left at all.
    """
    app = await bridge.app(refresh=True)
    window = None
    if kind == "tab":
        window = (find_window(app, window_id) if window_id else None) \
            or (window_for_session(app, near_session_id) if near_session_id
                else None) or app.current_window
    if window is None:
        window = await iterm2.Window.async_create(await bridge.connection())
        if window is None:
            raise RuntimeError("iTerm2 didn't open a window")
        tab = window.tabs[0]
    else:
        tab = await window.async_create_tab()
        if tab is None:
            raise RuntimeError("iTerm2 didn't open a tab")
    return tab.sessions[0]


@contextlib.asynccontextmanager
async def change_signal(session):
    """Hands back a `wait()` that returns when the screen probably changed.

    iTerm2 can push a notification when it does, which beats asking four times
    a second forever. If subscribing fails — older iTerm2, a connection that
    just dropped — fall back to polling rather than going silent.
    """
    streamer = None
    try:
        streamer = session.get_screen_streamer(want_contents=False)
        await streamer.__aenter__()
    except Exception:
        streamer = None

    pending = None

    async def wait_for_push():
        # Keep one outstanding request alive across timeouts: cancelling it
        # would drop any notification that arrived in the meantime.
        nonlocal pending
        if pending is None:
            pending = asyncio.ensure_future(streamer.async_get())
        done, _ = await asyncio.wait({pending}, timeout=IDLE_SECONDS)
        if pending in done:
            finished, pending = pending, None
            finished.result()          # re-raise whatever it hit

    async def wait_by_polling():
        await asyncio.sleep(POLL_SECONDS)

    try:
        yield wait_for_push if streamer else wait_by_polling
    finally:
        if pending:
            pending.cancel()
        if streamer:
            with contextlib.suppress(Exception):
                await streamer.__aexit__(None, None, None)


async def resize_session(session, cols, rows):
    """Reshape the pane itself, so a phone gets a terminal it can actually
    show rather than 120 columns of 6px text.

    iTerm2 refuses this for a session sharing a tab with split panes, and for
    fullscreen windows — the error says which, so the phone can report it.
    """
    cols = max(20, min(500, int(cols)))
    rows = max(5, min(200, int(rows)))
    await session.async_set_grid_size(iterm2.util.Size(cols, rows))


async def activate_session(session):
    """Bring a session to the front on the Mac: select the pane, select its
    tab, raise its window, and put iTerm2 itself in the foreground."""
    await session.async_activate(select_tab=True, order_window_front=True)
    app = await bridge.app()
    # raise_all_windows would stack the other iTerm2 windows back on top of
    # the one we just raised.
    await app.async_activate(raise_all_windows=False)


def log_error(where, exc):
    """Say what went wrong in the log as well as on the phone. A failure the
    phone shows as a dead connection is unreadable from the phone."""
    print(f"{where}: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
    traceback.print_exc()
    sys.stderr.flush()


def error_payload(exc):
    msg = str(exc) or exc.__class__.__name__
    hint = ""
    low = msg.lower()
    if "refused" in low or "no such file" in low or "connect" in low:
        hint = ("Can't reach iTerm2's API. Enable it in iTerm2 -> Settings -> "
                "General -> Magic -> 'Enable Python API', then retry.")
    return {"type": "error", "message": msg, "hint": hint}


# ------------------------------------------------------------------ gatekeeping

def allowed_networks(allow_lan=False):
    names = LOOPBACK_NETS + TAILNET_NETS + (LAN_NETS if allow_lan else [])
    return [ipaddress.ip_network(n) for n in names]


def peer_allowed(remote, nets):
    """Is this source address one we're willing to serve?"""
    if not remote:
        return False
    try:
        ip = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped          # ::ffff:100.1.2.3 is still a tailnet peer
    return any(ip in net for net in nets)


def hash_password(password, salt=None):
    """Salted PBKDF2, so the file on disk isn't the password itself."""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def password_matches(password, stored):
    salt = stored.split("$", 1)[0]
    try:
        candidate = hash_password(password, salt)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, stored)


class Password:
    """The password, if there is one. No password set means no login screen —
    being locked out of the thing you reach your Mac with is its own outage."""

    def __init__(self, plain=None, stored=None):
        self.plain = plain          # from the environment, compared as-is
        self.stored = stored        # salted hash from disk

    @classmethod
    def from_disk(cls):
        stored = None
        if PASSWORD_FILE.exists():
            stored = PASSWORD_FILE.read_text().strip() or None
        return cls(os.environ.get("TERMDECK_PASSWORD") or None, stored)

    @property
    def required(self):
        return bool(self.plain or self.stored)

    def check(self, password):
        if not password:
            return False
        if self.plain and hmac.compare_digest(password, self.plain):
            return True
        return bool(self.stored) and password_matches(password, self.stored)

    @staticmethod
    def save(password):
        PASSWORD_FILE.write_text(hash_password(password) + "\n")
        PASSWORD_FILE.chmod(0o600)


def session_secret():
    """The cookie's value: stable across restarts, so a restart doesn't sign
    every device out. Delete the file to sign them all out at once."""
    if SESSION_FILE.exists():
        existing = SESSION_FILE.read_text().strip()
        if existing:
            return existing
    secret = secrets.token_urlsafe(32)
    SESSION_FILE.write_text(secret + "\n")
    SESSION_FILE.chmod(0o600)
    return secret


LOGIN_PAGE = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>termdeck</title>
<style>
 body {{ background:#101114; color:#d6d7db; font:15px/1.5 -apple-system,system-ui,sans-serif;
        display:flex; min-height:100vh; margin:0; align-items:center; justify-content:center; }}
 form {{ width:min(320px, 86vw); }}
 h1 {{ font-size:16px; letter-spacing:.02em; margin:0 0 14px; }}
 input, button {{ width:100%; font:16px -apple-system,system-ui,sans-serif; padding:11px 12px;
        border-radius:8px; border:1px solid #26272c; background:#17181c; color:#d6d7db; }}
 button {{ margin-top:8px; color:#6ea8fe; font-weight:600; }}
 p {{ color:#ef6a6a; font-size:13px; min-height:18px; margin:8px 0 0; }}
</style>
<form method=post action="/login">
 <h1>termdeck</h1>
 <input type=password name=password autocomplete=current-password autofocus
        placeholder=Password>
 <button type=submit>Unlock</button>
 <p>{error}</p>
</form>
"""


def make_guard(nets, password, secret):
    open_paths = ("/healthz", "/login")

    @web.middleware
    async def guard(request, handler):
        if not peer_allowed(request.remote, nets):
            # Deliberately terse: nothing here confirms what's running.
            return web.Response(status=403, text="forbidden\n")
        if not password.required or request.path in open_paths:
            return await handler(request)
        if hmac.compare_digest(request.cookies.get(COOKIE, ""), secret):
            return await handler(request)
        return web.Response(status=401, text=LOGIN_PAGE.format(error=""),
                            content_type="text/html")
    return guard


def make_login(password, secret):
    async def login(request):
        form = await request.post()
        if not password.check(form.get("password", "")):
            await asyncio.sleep(1)          # take the fun out of guessing
            return web.Response(status=401, content_type="text/html",
                                text=LOGIN_PAGE.format(error="Wrong password."))
        response = web.HTTPFound("/")
        response.set_cookie(COOKIE, secret, httponly=True, samesite="Strict",
                            max_age=31536000, path="/")
        raise response
    return login


# ---------------------------------------------------------------- HTTP routes

async def index(_request):
    return web.FileResponse(STATIC / "index.html")


async def healthz(_request):
    return web.json_response({"ok": True, "version": VERSION})


async def api_sessions(_request):
    try:
        return web.json_response({"windows": await list_sessions()})
    except Exception as exc:
        log_error("listing sessions", exc)
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


async def api_new(request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = data.get("kind", "tab")
    if kind not in ("tab", "window"):
        return web.json_response({"message": "kind must be tab or window"},
                                 status=400)
    try:
        session = await new_session(kind, data.get("session_id"),
                                    data.get("window_id"))
        title = await session_var(session, "autoName") or session.name or "shell"
        return web.json_response({"session_id": session.session_id, "title": title})
    except Exception as exc:
        log_error(f"opening a {kind}", exc)
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


async def api_close(request):
    """Close a pane, a tab or a window.

    Always forced: the unforced call puts a confirmation sheet on the Mac,
    which is no use at all to someone holding a phone. The phone asks instead.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind, target = data.get("kind"), data.get("id")
    if kind not in ("session", "tab", "window") or not target:
        return web.json_response({"message": "kind must be session, tab or "
                                             "window, with an id"}, status=400)
    try:
        app = await bridge.app(refresh=True)
        thing = ({"session": app.get_session_by_id,
                  "tab": lambda i: find_tab(app, i),
                  "window": lambda i: find_window(app, i)}[kind])(target)
        if thing is None:
            return web.json_response({"message": f"{kind} already gone"},
                                     status=404)
        await thing.async_close(force=True)
        return web.json_response({"closed": kind})
    except Exception as exc:
        log_error(f"closing a {kind}", exc)
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


async def api_rename(request):
    """Name a session. Six tabs all called "shell" is not a list."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return web.json_response({"message": "a name is required"}, status=400)
    try:
        session = await get_session(data.get("session_id", ""))
        if session is None:
            return web.json_response({"message": "session gone"}, status=404)
        await session.async_set_name(name.strip()[:120])
        return web.json_response({"title": name.strip()[:120]})
    except Exception as exc:
        log_error("renaming a session", exc)
        return web.json_response(error_payload(exc), status=502)


async def api_search(request):
    """Find text anywhere in a session's history, not just the part the phone
    has loaded. Reads in chunks so a long buffer doesn't arrive at once."""
    session_id = request.query.get("session_id")
    needle = request.query.get("q", "")
    if not needle:
        return web.json_response({"message": "nothing to search for"},
                                 status=400)
    session = await get_session(session_id) if session_id else None
    if session is None:
        return web.json_response({"message": "session gone"}, status=404)
    limit = min(int(request.query.get("limit", 200) or 200), 500)
    lowered = needle.lower()
    hits = []
    try:
        info = await session.async_get_line_info()
        first = info.overflow
        total = (first + info.scrollback_buffer_height
                 + info.mutable_area_height)
        # Newest first: what you're looking for is usually near the end.
        end = total
        while end > first and len(hits) < limit:
            start = max(first, end - 2000)
            state = await read_lines(session, start, end - start)
            for offset, text in enumerate(state["lines"]):
                if lowered in text.lower():
                    hits.append({"n": state["start"] + offset, "text": text})
            end = start
        hits.sort(key=lambda hit: hit["n"])
        return web.json_response({"hits": hits[-limit:], "total": total,
                                  "first": first})
    except Exception as exc:
        log_error("searching", exc)
        return web.json_response(error_payload(exc), status=502)


async def api_cells(request):
    """A cell-by-cell dump of the last few lines, as plain text.

    Rendering bugs live in how iTerm2 describes a line, so this shows exactly
    that — openable from the phone, since that's where the problem is seen.
    """
    session_id = request.query.get("session_id")
    try:
        count = min(int(request.query.get("count", 6)), 50)
    except ValueError:
        count = 6
    if session_id and session_id != "current":
        session = await get_session(session_id)
    else:
        # No id given: whatever is in front on the Mac. Nobody knows their
        # session id off the top of their head, and looking it up on a phone
        # to file a bug report is a silly thing to ask.
        session = None
        with contextlib.suppress(Exception):
            app = await bridge.app(refresh=True)
            tab = app.current_window.current_tab if app.current_window else None
            session = tab.current_session if tab else None
    if session is None:
        return web.Response(status=404, text="no such session\n")
    try:
        conn = await bridge.connection()
        async with bridge.txn_lock:
            async with iterm2.Transaction(conn):
                info = await session.async_get_line_info()
                first = info.overflow
                total = (first + info.scrollback_buffer_height
                         + info.mutable_area_height)
                begin = max(first, total - count)
                lines = await session.async_get_contents(begin, total - begin)
    except Exception as exc:
        log_error("dumping cells", exc)
        return web.Response(status=502, text=f"{exc}\n")

    try:
        import importlib.metadata
        module = importlib.metadata.version("iterm2")
    except Exception:
        module = "unknown"
    protocol = "unknown"
    with contextlib.suppress(Exception):
        protocol = ".".join(str(p) for p in conn.iterm2_protocol_version)
    styled_cells = 0
    out = [f"termdeck v{VERSION}  —  iterm2 module {module}  —  "
           f"iTerm2 API protocol {protocol}  —  last {len(lines)} lines", ""]
    for n, line in enumerate(lines, start=begin):
        rebuilt, runs = rebuild_line(line)
        out.append(f"--- line {n} " + "-" * 40)
        out.append(f"  iTerm2 : {line.string!r}")
        out.append(f"  sent   : {rebuilt!r}")
        if line.string != rebuilt:
            out.append("  (differ — blanks were restored)")
        if runs:
            colours = sum(1 for run in runs if run[1:] != [None, None, 0])
            out.append(f"  styles : {len(runs)} runs, {colours} of them styled")
        else:
            out.append("  styles : NONE — the rebuild disagreed with iTerm2, "
                       "so colour was dropped to keep the text")
        empty = nul = odd = cells = 0
        notes = []
        while True:
            try:
                piece = line.string_at(cells)
            except IndexError:
                break
            if piece == "":
                empty += 1
                notes.append(f"cell {cells}: empty")
            elif not piece.strip("\x00"):
                nul += 1
                notes.append(f"cell {cells}: NUL")
            elif len(piece) != 1:
                odd += 1
                notes.append(f"cell {cells}: {len(piece)}cp {piece!r}")
            if line.style_at(cells) is not None:
                styled_cells += 1
            cells += 1
        out.append(f"  cells  : {cells}  (empty {empty}, NUL {nul}, multi {odd})")
        out += [f"    {note}" for note in notes[:8]]

    out.append("")
    if styled_cells:
        out.append(f"iTerm2 described the style of {styled_cells} cells — "
                   "colour information is arriving, so anything monochrome "
                   "on the phone is a rendering bug.")
    else:
        try:
            numeric = tuple(int(part) for part in module.split(".")[:2])
        except Exception:
            numeric = (0, 0)
        out.append("iTerm2 described the style of NO cells, which is why "
                   "there is no colour.")
        if numeric < (2, 10):
            out.append(f"CAUSE: the iterm2 module here is {module}, and only "
                       "2.10+ ever asks iTerm2 for styles.")
            out.append("FIX:   ./install.sh   (a pm2 restart does not upgrade "
                       "it, and nothing needs to close — not iTerm2, not your "
                       "sessions)")
        else:
            out.append(f"CAUSE: the iterm2 module is {module}, and every "
                       "request does set include_styles — so iTerm2 itself "
                       f"is not filling it in. Its API protocol is {protocol}.")
            out.append("FIX:   upgrade iTerm2 (Help -> Check For Updates). "
                       "That means quitting it, so it can wait: colour is the "
                       "only thing affected, and nothing else here depends on "
                       "it.")
    return web.Response(text="\n".join(out) + "\n", content_type="text/plain")


async def api_scrollback(request):
    try:
        session_id = request.query.get("session_id")
        if not session_id:
            return web.json_response({"message": "session_id required"}, status=400)
        try:
            start = int(request.query.get("start", 0))
            count = int(request.query.get("count", TAIL_LINES))
        except ValueError:
            return web.json_response({"message": "start/count must be integers"},
                                     status=400)
        session = await get_session(session_id)
        if session is None:
            return web.json_response({"message": "session gone"}, status=404)
        return web.json_response(await read_lines(session, start, count))
    except Exception as exc:
        log_error("reading scrollback", exc)
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    session_id = None            # where typing goes
    watchers = {}                # session id -> task feeding frames
    # Panes this viewer reshaped, and the size they had before, so the Mac
    # goes back to normal when the phone goes away.
    reshaped = {}

    async def send_json(payload):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    async def watch_loop(sid, count):
        last_state = None
        while True:
            try:
                session = await get_session(sid)
                if session is None:
                    await send_json({"type": "gone", "session_id": sid})
                    return
                async with change_signal(session) as wait:
                    while True:
                        session = await get_session(sid)
                        if session is None:
                            await send_json({"type": "gone", "session_id": sid})
                            return
                        state = await read_lines(session, count=count)
                        # Comparing the payloads directly beats hashing them:
                        # no serialising a screenful of text twice a frame.
                        if state != last_state:
                            last_state = state
                            await send_json({"type": "screen",
                                             "session_id": sid, **state})
                        # iTerm2 notifies on every screen change, which during
                        # heavy output is dozens a second — far more than a
                        # phone can show, and each read costs real work.
                        await asyncio.sleep(MIN_FRAME_SECONDS)
                        await wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_error(f"watching {sid}", exc)
                bridge.reset()
                await send_json(error_payload(exc))
                await asyncio.sleep(2)

    async def stop_watchers(keep=()):
        """`keep` is (session id, line count) pairs — a watcher feeding 15
        lines to a tile is no use to a full view that wants 400."""
        for sid in [s for s in watchers if (s, watchers[s][1]) not in keep]:
            task, _ = watchers.pop(sid)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def needed_session(sid):
        session = await get_session(sid)
        if session is None:
            await send_json({"type": "gone", "session_id": sid})
        return session

    async def handle(data):
        nonlocal session_id
        kind = data.get("type")
        if kind == "watch":
            # One session (the terminal view) or several (the tiled view).
            wanted = data.get("session_ids")
            if not isinstance(wanted, list):
                wanted = [data["session_id"]] if data.get("session_id") else []
            wanted = [w for w in wanted if isinstance(w, str)][:12]
            count = min(int(data.get("lines") or TAIL_LINES), TAIL_LINES)
            session_id = wanted[0] if len(wanted) == 1 else None
            await stop_watchers(keep=[(sid, count) for sid in wanted])
            for sid in wanted:
                if sid not in watchers:
                    watchers[sid] = (asyncio.create_task(watch_loop(sid, count)),
                                     count)
            return
        target = data.get("session_id") or session_id
        if not target:
            return                      # nothing to talk to yet
        if kind in ("input", "key"):
            text = (data.get("text", "") if kind == "input"
                    else KEYMAP.get(data.get("name", ""), ""))
            if not isinstance(text, str) or not text:
                return
            session = await get_session(target)
            if session:
                await session.async_send_text(text)
        elif kind == "resize":
            session = await needed_session(target)
            if session:
                grid = getattr(session, "grid_size", None)
                if target not in reshaped and grid:
                    reshaped[target] = (grid.width, grid.height)
                await resize_session(session, data.get("cols", 80),
                                     data.get("rows", 24))
        elif kind == "activate":
            session = await needed_session(target)
            if session:
                await activate_session(session)
                await send_json({"type": "activated", "session_id": target})

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue              # a bare JSON value is not a command
            try:
                await handle(data)
            except Exception as exc:
                # One bad message must never take the socket down with it —
                # from the phone that is indistinguishable from a crash.
                log_error("websocket message", exc)
                await send_json(error_payload(exc))
    finally:
        await stop_watchers()
        # Put the Mac back the way it was, even if the phone just went into a
        # tunnel — leaving someone's pane 51 columns wide is not on.
        for sid, (cols, rows) in reshaped.items():
            with contextlib.suppress(Exception):
                session = await get_session(sid)
                if session:
                    await resize_session(session, cols, rows)
    return ws


# --------------------------------------------------------------------- server

def tailscale_ip():
    for cmd in (["tailscale", "ip", "-4"],
                ["/Applications/Tailscale.app/Contents/MacOS/Tailscale", "ip", "-4"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=5).stdout.strip().splitlines()
            if out and out[0].startswith("100."):
                return out[0]
        except Exception:
            continue
    return None


async def bind(runner, host, sites):
    site = web.TCPSite(runner, host, PORT)
    await site.start()
    sites[host] = site
    print(f"termdeck listening on http://{host}:{PORT}", flush=True)


async def rebind_watcher(runner, sites):
    """Keep the Tailscale binding current.

    Sleeping the Mac, or Tailscale reconnecting, can hand it a different
    tailnet address; the socket bound to the old one still exists but nothing
    can reach it, and the process never crashed so no supervisor would notice.
    Check periodically and move the binding.
    """
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(REBIND_SECONDS)
        try:
            # tailscale_ip() shells out — keep it off the event loop.
            ip = await loop.run_in_executor(None, tailscale_ip)
            if not ip or ip in sites:
                continue
            for host, site in list(sites.items()):
                if host != "127.0.0.1":
                    await site.stop()
                    sites.pop(host, None)
                    print(f"tailscale address {host} went away", flush=True)
            await bind(runner, ip, sites)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"rebind failed: {exc}", file=sys.stderr, flush=True)


def make_app(password=None, allow_lan=False, secret="test-secret"):
    password = password or Password()          # no password: no login screen
    app = web.Application(middlewares=[
        make_guard(allowed_networks(allow_lan), password, secret)])
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/login", make_login(password, secret))
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/scrollback", api_scrollback)
    app.router.add_get("/api/cells", api_cells)
    app.router.add_post("/api/new", api_new)
    app.router.add_post("/api/close", api_close)
    app.router.add_post("/api/rename", api_rename)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="termdeck")
    parser.add_argument("--lan", action="store_true",
                        help="also serve this Mac's Wi-Fi address, not just Tailscale")
    parser.add_argument("--set-password", action="store_true",
                        help="set the password and exit; asks for it, stores "
                             "only a hash")
    parser.add_argument("--no-auth", action="store_true",
                        help="ignore any password that has been set")
    parser.add_argument("--auth", action="store_true",
                        help=argparse.SUPPRESS)     # kept so old configs run
    return parser.parse_args(argv)


def lan_ip():
    """This Mac's address on the local network, if it has one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))       # reserved, no packets are sent
        ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if ip.startswith(("127.", "100.")) else ip


async def main(argv=None):
    args = parse_args(argv)
    password = Password.from_disk()
    if args.no_auth:
        password.plain = password.stored = None
    runner = web.AppRunner(make_app(password, args.lan, session_secret()))
    await runner.setup()

    hosts = ["127.0.0.1"]
    ts_ip = tailscale_ip()
    if ts_ip:
        hosts.append(ts_ip)
    if args.lan:
        local = lan_ip()
        if local:
            hosts.append(local)
    sites, in_use = {}, False
    for host in hosts:
        try:
            await bind(runner, host, sites)
        except OSError as exc:
            in_use = in_use or exc.errno == errno.EADDRINUSE
            print(f"could not bind {host}:{PORT}: {exc}", file=sys.stderr, flush=True)
    if not sites:
        if in_use:
            print("termdeck is already running (or something else holds port "
                  f"{PORT}). Stop it first: pm2 stop termdeck, or launchctl "
                  "bootout gui/$(id -u)/com.carlitos.termdeck",
                  file=sys.stderr, flush=True)
        sys.exit(1)
    if not ts_ip:
        print("warning: no Tailscale IP found; will keep checking",
              file=sys.stderr, flush=True)
    reach = "loopback + tailnet" + (" + LAN" if args.lan else "")
    print(f"accepting connections from: {reach}", flush=True)
    if password.required:
        print("password required; each device asks once, then remembers",
              flush=True)
    else:
        print("no password set — anything that can reach the port gets a "
              "shell. Set one with: ./.venv/bin/python server.py --set-password",
              flush=True)
    await rebind_watcher(runner, sites)


def set_password_interactively():
    first = getpass.getpass("New termdeck password: ")
    if not first:
        print("nothing entered; password unchanged", file=sys.stderr)
        return 1
    if first != getpass.getpass("Again: "):
        print("they didn't match; password unchanged", file=sys.stderr)
        return 1
    Password.save(first)
    print(f"saved to {PASSWORD_FILE.name}. Restart termdeck to require it: "
          "pm2 restart termdeck")
    return 0


if __name__ == "__main__":
    if "--set-password" in sys.argv:
        sys.exit(set_password_interactively())
    asyncio.run(main())
