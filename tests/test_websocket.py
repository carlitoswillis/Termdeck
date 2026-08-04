import asyncio
import re
from pathlib import Path

import server


async def recv(ws, kind, timeout=5):
    """Read frames until one of `kind` shows up."""
    async def pump():
        while True:
            msg = await ws.receive_json()
            if msg["type"] == kind:
                return msg
    return await asyncio.wait_for(pump(), timeout)


async def test_watch_pushes_the_screen(client, fake_iterm2):
    fake_iterm2.session.info.overflow = 100
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})

        frame = await recv(ws, "screen")

    assert frame["first"] == 100
    assert frame["total"] == 115
    assert frame["lines"][0] == "line 100"


async def test_watching_a_missing_session_reports_gone(client):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "ghost"})

        assert (await recv(ws, "gone"))["type"] == "gone"


async def test_input_and_keys_reach_the_session(client, fake_iterm2):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        await ws.send_json({"type": "input", "text": "ls\r"})
        await ws.send_json({"type": "key", "name": "ctrl-c"})
        await ws.send_json({"type": "key", "name": "no-such-key"})
        await ws.send_json({"type": "input", "text": ""})

        for _ in range(50):
            if len(fake_iterm2.session.sent) >= 2:
                break
            await asyncio.sleep(0.02)

    assert fake_iterm2.session.sent == ["ls\r", "\x03"]


async def test_input_before_watch_is_ignored(client, fake_iterm2):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "input", "text": "rm -rf /\r"})
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

    assert fake_iterm2.session.sent == []


async def test_garbage_does_not_kill_the_socket(client):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_str("not json")
        await ws.send_json({"type": "watch"})            # no session_id
        await ws.send_json({"type": "watch", "session_id": "sess-1"})

        assert (await recv(ws, "screen"))["lines"]


def test_keymap_covers_every_key_in_the_ui():
    html = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()
    keys = re.search(r"const KEYS = \[(.*?)\];", html, re.S).group(1)
    names = re.findall(r'\["([a-z\-]+)"\s*,', keys)

    assert names
    assert set(names) <= set(server.KEYMAP)
