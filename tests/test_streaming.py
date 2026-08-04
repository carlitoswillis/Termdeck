"""Updates come from iTerm2 pushing, with polling only as a fallback."""
import asyncio

import server
from test_websocket import recv


async def test_a_screen_change_pushes_a_frame(client, fake_iterm2):
    session = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")                      # the first frame is free

        # Wait for the watcher to be parked on the streamer rather than
        # spinning: that's the whole point of the change.
        for _ in range(200):
            if session.streamer and session.streamer.gets:
                break
            await asyncio.sleep(0.01)
        assert session.streamer.entered
        gets_before = session.streamer.gets

        session.info.scrollback_buffer_height += 1    # something new on screen
        session.streamer.push()

        frame = await recv(ws, "screen")

    assert frame["total"] == 16
    assert session.streamer.gets >= gets_before


async def test_an_idle_screen_is_not_re_read(client, fake_iterm2):
    """The old design asked iTerm2 for 400 lines every 400ms forever, per
    viewer. An idle session should now cost nothing until it changes."""
    session = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        await asyncio.sleep(0.05)                 # let it park on the streamer
        settled = len(session.requests)

        await asyncio.sleep(0.5)                  # ~12 polls, in the old design

        assert len(session.requests) == settled


async def test_it_falls_back_to_polling_when_it_cannot_subscribe(client, fake_iterm2, monkeypatch):
    """An iTerm2 that won't push must not leave the phone frozen."""
    monkeypatch.setattr(server, "POLL_SECONDS", 0.01)
    session = fake_iterm2.session
    session.streamer_error = RuntimeError("no notifications here")

    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        session.info.scrollback_buffer_height += 1
        frame = await recv(ws, "screen")

    assert frame["total"] == 16
    assert session.streamer is None            # never got one


async def test_the_subscription_is_released_when_the_viewer_leaves(client, fake_iterm2):
    session = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        for _ in range(200):
            if session.streamer and session.streamer.gets:
                break
            await asyncio.sleep(0.01)

    for _ in range(200):                       # the socket closing tears it down
        if session.streamer.exited:
            break
        await asyncio.sleep(0.01)
    assert session.streamer.exited


async def test_switching_sessions_drops_the_old_subscription(client, fake_iterm2):
    first = fake_iterm2.session
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        for _ in range(200):
            if first.streamer and first.streamer.gets:
                break
            await asyncio.sleep(0.01)

        await ws.send_json({"type": "watch", "session_id": "ghost"})
        await recv(ws, "gone")

    assert first.streamer.exited
