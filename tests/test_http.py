import pytest

import server


async def test_index_is_served(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert "termdeck" in await resp.text()


async def test_sessions_lists_windows_tabs_and_panes(client, fake_iterm2):
    resp = await client.get("/api/sessions")

    assert resp.status == 200
    windows = (await resp.json())["windows"]
    pane = windows[0]["tabs"][0]["sessions"][0]
    assert pane == {"id": "sess-1", "title": "shell", "job": "zsh",
                    "path": "/Users/x/work", "tty": "/dev/ttys001"}
    assert windows[0]["tabs"][0]["active"] is True
    assert fake_iterm2.app.refreshes == 1   # list view always sees new tabs


async def test_sessions_reports_a_dead_api_with_a_hint(client, monkeypatch):
    async def boom(refresh=False):
        raise ConnectionRefusedError("[Errno 61] Connection refused")

    monkeypatch.setattr(server.bridge, "app", boom)

    resp = await client.get("/api/sessions")

    assert resp.status == 502
    body = await resp.json()
    assert "Enable Python API" in body["hint"]


async def test_scrollback_returns_a_clamped_range(client, fake_iterm2):
    fake_iterm2.session.info.overflow = 1000
    fake_iterm2.session.info.scrollback_buffer_height = 500

    resp = await client.get("/api/scrollback",
                            params={"session_id": "sess-1", "start": "0",
                                    "count": "50"})

    assert resp.status == 200
    body = await resp.json()
    assert body["start"] == 1000
    assert body["first"] == 1000
    assert body["lines"][0] == "line 1000"


@pytest.mark.parametrize("params, status", [
    ({}, 400),
    ({"session_id": "sess-1", "start": "abc"}, 400),
    ({"session_id": "nope"}, 404),
])
async def test_scrollback_rejects_bad_requests(client, params, status):
    resp = await client.get("/api/scrollback", params=params)
    assert resp.status == status
