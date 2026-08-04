"""Watching several sessions at once, naming them, and searching them."""
import asyncio

import pytest
from conftest import FakeSession, FakeTab

from test_websocket import recv


async def frames_for(ws, ids, timeout=3):
    """Collect one screen frame per session id."""
    seen = {}

    async def pump():
        while len(seen) < len(ids):
            msg = await ws.receive_json()
            if msg["type"] == "screen":
                seen[msg["session_id"]] = msg
        return seen

    return await asyncio.wait_for(pump(), timeout)


@pytest.fixture
def three(fake_iterm2):
    """Three sessions in one window, as a tiled view would watch."""
    window = fake_iterm2.app.terminal_windows[0]
    for n in (2, 3):
        window.tabs.append(FakeTab([FakeSession(f"sess-{n}", scrollback=n * 2)],
                                   tab_id=f"tab-{n}"))
    return fake_iterm2


async def test_watching_several_sessions_at_once(client, three):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch",
                            "session_ids": ["sess-1", "sess-2", "sess-3"],
                            "lines": 15})

        frames = await frames_for(ws, ["sess-1", "sess-2", "sess-3"])

    assert set(frames) == {"sess-1", "sess-2", "sess-3"}
    # Every frame says which session it belongs to, or they can't be routed.
    for sid, frame in frames.items():
        assert frame["session_id"] == sid
        assert frame["lines"]


async def test_a_tile_only_asks_for_the_lines_it_shows(client, three):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_ids": ["sess-1"],
                            "lines": 15})
        await frames_for(ws, ["sess-1"])

    assert three.session.requests[0][1] == 15      # not the full 400


async def test_switching_to_one_session_drops_the_others(client, three):
    second = three.app.terminal_windows[0].tabs[1].sessions[0]
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch",
                            "session_ids": ["sess-1", "sess-2"], "lines": 5})
        await frames_for(ws, ["sess-1", "sess-2"])

        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")
        for _ in range(200):
            if second.streamer.exited:
                break
            await asyncio.sleep(0.01)

    assert second.streamer.exited


async def test_asking_for_more_lines_restarts_the_watcher(client, three):
    """A tile watches 15 lines; opening that session wants 400. Reusing the
    tile's watcher would leave the full view showing fifteen lines."""
    three.session.info.scrollback_buffer_height = 1000   # more than 400 to give
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_ids": ["sess-1"],
                            "lines": 15})
        await frames_for(ws, ["sess-1"])

        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        for _ in range(300):
            if any(count == 400 for _, count in three.session.requests):
                break
            await asyncio.sleep(0.01)

    assert three.session.requests[0][1] == 15
    assert any(count == 400 for _, count in three.session.requests)


async def test_typing_still_lands_with_one_session_watched(client, fake_iterm2):
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        await recv(ws, "screen")

        await ws.send_json({"type": "input", "text": "hello\r"})
        for _ in range(100):
            if fake_iterm2.session.sent:
                break
            await asyncio.sleep(0.01)

    assert fake_iterm2.session.sent == ["hello\r"]


async def test_a_tile_can_be_typed_into_by_naming_it(client, three):
    """With several watched there's no single 'current' session, so the
    message has to say which one it means."""
    async with client.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch",
                            "session_ids": ["sess-1", "sess-2"], "lines": 5})
        await frames_for(ws, ["sess-1", "sess-2"])

        await ws.send_json({"type": "input", "session_id": "sess-1",
                            "text": "aimed\r"})
        for _ in range(100):
            if three.session.sent:
                break
            await asyncio.sleep(0.01)

    assert three.session.sent == ["aimed\r"]


async def test_renaming_a_session(client, fake_iterm2):
    resp = await client.post("/api/rename", json={"session_id": "sess-1",
                                                  "name": "  decrypt  "})

    assert resp.status == 200
    assert (await resp.json())["title"] == "decrypt"
    assert fake_iterm2.session.name == "decrypt"


@pytest.mark.parametrize("payload, status", [
    ({"session_id": "sess-1"}, 400),
    ({"session_id": "sess-1", "name": "   "}, 400),
    ({"session_id": "ghost", "name": "x"}, 404),
])
async def test_rename_rejects_nonsense(client, payload, status):
    assert (await client.post("/api/rename", json=payload)).status == status


async def test_search_finds_lines_the_phone_never_loaded(client, fake_iterm2):
    """The point of searching server-side: the match may be thousands of
    lines above anything the phone is holding."""
    fake_iterm2.session.info.scrollback_buffer_height = 5000

    resp = await client.get("/api/search", params={"session_id": "sess-1",
                                                   "q": "line 42"})

    assert resp.status == 200
    hits = (await resp.json())["hits"]
    assert [hit["n"] for hit in hits][:2] == [42, 420]
    assert hits[0]["text"] == "line 42"


async def test_search_is_case_insensitive(client, fake_iterm2):
    resp = await client.get("/api/search", params={"session_id": "sess-1",
                                                   "q": "LINE 3"})

    assert [hit["n"] for hit in (await resp.json())["hits"]] == [3]


async def test_search_needs_something_to_look_for(client):
    assert (await client.get("/api/search",
                             params={"session_id": "sess-1"})).status == 400
