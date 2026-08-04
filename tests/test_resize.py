"""Reshaping the pane itself — the only real answer to a portrait phone."""
import asyncio

import server
from test_websocket import recv


async def wait_for(predicate, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return predicate()


async def test_resize_reshapes_the_pane(client, fake_iterm2):
    session = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        await ws.send_json({"type": "resize", "cols": 58, "rows": 30})

        assert await wait_for(lambda: session.resizes)
    assert session.resizes[0] == (58, 30)


async def test_absurd_sizes_are_clamped(client, fake_iterm2):
    session = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        await ws.send_json({"type": "resize", "cols": 0, "rows": 99999})

        assert await wait_for(lambda: session.resizes)
    assert session.resizes[0] == (20, 200)


async def test_the_pane_is_restored_when_the_viewer_disconnects(client, fake_iterm2):
    """A phone dropping into a tunnel must not leave someone's pane 58
    columns wide."""
    session = fake_iterm2.session
    original = (session.grid_size.width, session.grid_size.height)

    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        await ws.send_json({"type": "resize", "cols": 58, "rows": 30})
        assert await wait_for(lambda: session.resizes)

    assert await wait_for(lambda: len(session.resizes) >= 2)
    assert session.resizes[-1] == original


async def test_only_the_first_shape_is_remembered(client, fake_iterm2):
    """Resizing twice shouldn't make the second shape the one we restore."""
    session = fake_iterm2.session
    original = (session.grid_size.width, session.grid_size.height)

    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        await ws.send_json({"type": "resize", "cols": 58, "rows": 30})
        assert await wait_for(lambda: session.resizes)
        await ws.send_json({"type": "resize", "cols": 40, "rows": 20})
        assert await wait_for(lambda: len(session.resizes) >= 2)

    assert await wait_for(lambda: len(session.resizes) >= 3)
    assert session.resizes[-1] == original


async def test_a_refusal_is_reported_not_swallowed(client, fake_iterm2):
    """iTerm2 refuses this for split panes and fullscreen windows, and the
    phone should say why rather than look broken."""
    fake_iterm2.session.resize_error = RuntimeError("cannot resize a split pane")

    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        await ws.send_json({"type": "resize", "cols": 58, "rows": 30})

        assert (await recv(ws, "error"))["message"] == "cannot resize a split pane"


async def test_resize_without_a_session_is_ignored(client, fake_iterm2):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "resize", "cols": 58, "rows": 30})
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

    assert fake_iterm2.session.resizes == []
