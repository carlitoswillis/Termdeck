#!/usr/bin/env python3
"""termdeck — view and type into your local iTerm2 sessions from any device
on your Tailscale network.

Requires: iTerm2 with the Python API enabled
(iTerm2 -> Settings -> General -> Magic -> "Enable Python API").

Binds only to localhost and the Tailscale interface — never a public IP.
"""
import argparse
import asyncio
import errno
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import subprocess
import sys
from pathlib import Path

from aiohttp import web, WSMsgType
import iterm2

PORT = 7717
TAIL_LINES = 400          # lines of scrollback kept in the live view
POLL_SECONDS = 0.4        # screen poll interval per viewer
REBIND_SECONDS = 30       # how often to recheck the Tailscale address
STATIC = Path(__file__).parent / "static"
TOKEN_FILE = Path(__file__).parent / ".termdeck-token"
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


async def list_sessions():
    app = await bridge.app(refresh=True)
    windows = []
    for w, window in enumerate(app.terminal_windows):
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
                })
            tabs.append({"index": t, "sessions": sessions,
                         "active": tab.tab_id == window.current_tab.tab_id
                         if window.current_tab else False})
        windows.append({"index": w, "tabs": tabs})
    return windows


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
    # The pane's real width in cells, so the phone can size text to fit it
    # rather than guessing from the longest line it happens to have.
    grid = getattr(session, "grid_size", None)
    return {
        "first": first,
        "start": begin,
        "total": total,
        "cols": getattr(grid, "width", 0) or 0,
        "rows": getattr(grid, "height", 0) or 0,
        "lines": [l.string for l in lines],
    }


def window_for_session(app, session_id):
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id == session_id:
                    return window
    return None


async def new_session(kind, near_session_id=None):
    """Open a tab or a window and hand back the session inside it.

    A new tab goes in the window of the session the phone was last watching,
    falling back to iTerm2's current window — and to a new window when there
    is none (every window closed, iTerm2 sitting in the dock).
    """
    app = await bridge.app(refresh=True)
    window = None
    if kind == "tab":
        window = (window_for_session(app, near_session_id) if near_session_id
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


async def activate_session(session):
    """Bring a session to the front on the Mac: select the pane, select its
    tab, raise its window, and put iTerm2 itself in the foreground."""
    await session.async_activate(select_tab=True, order_window_front=True)
    app = await bridge.app()
    # raise_all_windows would stack the other iTerm2 windows back on top of
    # the one we just raised.
    await app.async_activate(raise_all_windows=False)


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


def load_token():
    """Read the shared token, creating one on first run."""
    if TOKEN_FILE.exists():
        existing = TOKEN_FILE.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


UNAUTHORIZED = """<!doctype html><meta charset=utf-8>
<title>termdeck</title>
<body style="font:15px system-ui;background:#101114;color:#d6d7db;padding:2rem">
<h1 style="font-size:17px">termdeck</h1>
<p>This link needs the access token. Start termdeck and open the URL it
prints, or run <code>cat .termdeck-token</code> on the Mac and visit
<code>/?t=&lt;token&gt;</code>.</p>
"""


def make_guard(nets, token):
    @web.middleware
    async def guard(request, handler):
        if not peer_allowed(request.remote, nets):
            # Deliberately terse: nothing here confirms what's running.
            return web.Response(status=403, text="forbidden\n")
        if token is None or request.path == "/healthz":
            return await handler(request)
        supplied = request.query.get("t")
        if supplied and hmac.compare_digest(supplied, token):
            # Move it out of the URL so it stops riding along in history,
            # bookmarks and the address bar. The fragment (#s=…) survives.
            response = web.HTTPFound(request.path)
            response.set_cookie(COOKIE, token, httponly=True, samesite="Strict",
                                max_age=31536000, path="/")
            raise response
        if hmac.compare_digest(request.cookies.get(COOKIE, ""), token):
            return await handler(request)
        return web.Response(status=401, text=UNAUTHORIZED, content_type="text/html")
    return guard


# ---------------------------------------------------------------- HTTP routes

async def index(_request):
    return web.FileResponse(STATIC / "index.html")


async def healthz(_request):
    return web.Response(text="ok\n")


async def api_sessions(_request):
    try:
        return web.json_response({"windows": await list_sessions()})
    except Exception as exc:
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
        session = await new_session(kind, data.get("session_id"))
        title = await session_var(session, "autoName") or session.name or "shell"
        return web.json_response({"session_id": session.session_id, "title": title})
    except Exception as exc:
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


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
        bridge.reset()
        return web.json_response(error_payload(exc), status=502)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    session_id = None
    watcher = None

    async def send_json(payload):
        try:
            await ws.send_json(payload)
        except Exception:
            pass

    async def watch_loop(sid):
        last_hash = None
        while True:
            try:
                session = await get_session(sid)
                if session is None:
                    await send_json({"type": "gone"})
                    return
                state = await read_lines(session)
                digest = hashlib.md5(
                    ("\n".join(state["lines"]) + f'|{state["total"]}').encode()
                ).hexdigest()
                if digest != last_hash:
                    last_hash = digest
                    await send_json({"type": "screen", **state})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                bridge.reset()
                await send_json(error_payload(exc))
                await asyncio.sleep(2)
            await asyncio.sleep(POLL_SECONDS)

    async def stop_watcher():
        nonlocal watcher
        if watcher:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
            watcher = None

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type")
            if kind == "watch":
                await stop_watcher()
                session_id = data.get("session_id")
                if not session_id:
                    continue
                watcher = asyncio.create_task(watch_loop(session_id))
            elif kind in ("input", "key") and session_id:
                text = (data.get("text", "") if kind == "input"
                        else KEYMAP.get(data.get("name", ""), ""))
                if not text:
                    continue
                try:
                    session = await get_session(session_id)
                    if session:
                        await session.async_send_text(text)
                except Exception as exc:
                    bridge.reset()
                    await send_json(error_payload(exc))
            elif kind == "activate" and session_id:
                try:
                    session = await get_session(session_id)
                    if session is None:
                        await send_json({"type": "gone"})
                        continue
                    await activate_session(session)
                    await send_json({"type": "activated"})
                except Exception as exc:
                    bridge.reset()
                    await send_json(error_payload(exc))
    finally:
        await stop_watcher()
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


def make_app(token=None, allow_lan=False):
    app = web.Application(middlewares=[make_guard(allowed_networks(allow_lan), token)])
    app.router.add_get("/", index)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/scrollback", api_scrollback)
    app.router.add_post("/api/new", api_new)
    app.router.add_get("/api/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    return app


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="termdeck")
    parser.add_argument("--lan", action="store_true",
                        help="also serve this Mac's Wi-Fi address, not just Tailscale")
    parser.add_argument("--no-auth", action="store_true",
                        help="skip the access token (anyone who can reach the "
                             "port gets a shell)")
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
    token = None if args.no_auth else load_token()
    runner = web.AppRunner(make_app(token, args.lan))
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
    if token:
        for host in sites:
            print(f"open: http://{host}:{PORT}/?t={token}", flush=True)
        print(f"(token also in {TOKEN_FILE.name}; the link only needs it once "
              "per device)", flush=True)
    else:
        print("warning: --no-auth, so anything that can reach the port gets a "
              "shell", file=sys.stderr, flush=True)
    await rebind_watcher(runner, sites)


if __name__ == "__main__":
    asyncio.run(main())
