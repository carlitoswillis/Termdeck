#!/usr/bin/env python3
"""termdeck — view and type into your local iTerm2 sessions from any device
on your Tailscale network.

Requires: iTerm2 with the Python API enabled
(iTerm2 -> Settings -> General -> Magic -> "Enable Python API").

Binds only to localhost and the Tailscale interface — never a public IP.
"""
import asyncio
import errno
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from aiohttp import web, WSMsgType
import iterm2

PORT = 7717
TAIL_LINES = 400          # lines of scrollback kept in the live view
POLL_SECONDS = 0.4        # screen poll interval per viewer
STATIC = Path(__file__).parent / "static"

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
    return {
        "first": first,
        "start": begin,
        "total": total,
        "lines": [l.string for l in lines],
    }


def error_payload(exc):
    msg = str(exc) or exc.__class__.__name__
    hint = ""
    low = msg.lower()
    if "refused" in low or "no such file" in low or "connect" in low:
        hint = ("Can't reach iTerm2's API. Enable it in iTerm2 -> Settings -> "
                "General -> Magic -> 'Enable Python API', then retry.")
    return {"type": "error", "message": msg, "hint": hint}


# ---------------------------------------------------------------- HTTP routes

async def index(_request):
    return web.FileResponse(STATIC / "index.html")


async def api_sessions(_request):
    try:
        return web.json_response({"windows": await list_sessions()})
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


def make_app():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_get("/api/scrollback", api_scrollback)
    app.router.add_get("/api/ws", ws_handler)
    app.router.add_static("/static", STATIC)
    return app


async def main():
    runner = web.AppRunner(make_app())
    await runner.setup()

    hosts = ["127.0.0.1"]
    ts_ip = tailscale_ip()
    if ts_ip:
        hosts.append(ts_ip)
    bound, in_use = 0, False
    for host in hosts:
        try:
            await web.TCPSite(runner, host, PORT).start()
            bound += 1
            print(f"termdeck listening on http://{host}:{PORT}", flush=True)
        except OSError as exc:
            in_use = in_use or exc.errno == errno.EADDRINUSE
            print(f"could not bind {host}:{PORT}: {exc}", file=sys.stderr, flush=True)
    if not bound:
        if in_use:
            print("termdeck is already running (or something else holds port "
                  f"{PORT}). Stop it first: launchctl bootout "
                  "gui/$(id -u)/com.carlitos.termdeck", file=sys.stderr, flush=True)
        sys.exit(1)
    if not ts_ip:
        print("warning: no Tailscale IP found; serving on localhost only",
              file=sys.stderr, flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
