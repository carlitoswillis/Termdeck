"""Opening tabs and windows from the phone."""
from conftest import FakeSession, FakeTab, FakeWindow


async def test_new_tab_goes_in_the_current_window(client, fake_iterm2):
    resp = await client.post("/api/new", json={"kind": "tab"})

    assert resp.status == 200
    body = await resp.json()
    assert body["session_id"] == "new-1"
    assert body["title"] == "shell"
    assert len(fake_iterm2.app.terminal_windows) == 1     # no new window
    assert len(fake_iterm2.app.terminal_windows[0].tabs) == 2


async def test_new_tab_follows_the_session_you_were_watching(client, fake_iterm2):
    """Two windows open: the tab belongs beside the pane on your phone, not
    beside whatever iTerm2 happens to have in front."""
    watched = FakeSession("elsewhere")
    other = FakeWindow([FakeTab([watched], tab_id="tab-other")])
    fake_iterm2.app.terminal_windows.insert(0, other)     # not current_window

    resp = await client.post("/api/new", json={"kind": "tab",
                                               "session_id": "elsewhere"})

    assert resp.status == 200
    assert len(other.tabs) == 2
    assert len(fake_iterm2.app.terminal_windows[-1].tabs) == 1


async def test_new_tab_with_no_windows_left_opens_one(client, fake_iterm2):
    fake_iterm2.app.terminal_windows.clear()

    resp = await client.post("/api/new", json={"kind": "tab"})

    assert resp.status == 200
    assert (await resp.json())["session_id"] == "win-1"
    assert len(fake_iterm2.app.terminal_windows) == 1


async def test_new_window(client, fake_iterm2):
    resp = await client.post("/api/new", json={"kind": "window"})

    assert resp.status == 200
    assert (await resp.json())["session_id"] == "win-1"
    assert len(fake_iterm2.app.terminal_windows) == 2


async def test_the_new_session_is_immediately_watchable(client, fake_iterm2):
    """A tab iTerm2 just opened isn't in the cached app tree, so watching it
    has to survive a cache miss."""
    created = (await (await client.post("/api/new", json={"kind": "tab"})).json())

    resp = await client.get("/api/scrollback",
                            params={"session_id": created["session_id"]})

    assert resp.status == 200


async def test_a_refused_window_is_reported(client, fake_iterm2):
    fake_iterm2.windows.refuse = True

    resp = await client.post("/api/new", json={"kind": "window"})

    assert resp.status == 502
    assert "didn't open a window" in (await resp.json())["message"]


async def test_a_refused_tab_is_reported(client, fake_iterm2):
    fake_iterm2.app.terminal_windows[0].refuse_tab = True

    resp = await client.post("/api/new", json={"kind": "tab"})

    assert resp.status == 502
    assert "didn't open a tab" in (await resp.json())["message"]


async def test_unknown_kind_is_rejected(client):
    resp = await client.post("/api/new", json={"kind": "spaceship"})
    assert resp.status == 400
