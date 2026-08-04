"""Closing panes, tabs and windows, and saying which window a tab opens in."""
import pytest
from conftest import FakeSession, FakeTab, FakeWindow


async def test_closing_a_pane(client, fake_iterm2):
    resp = await client.post("/api/close", json={"kind": "session",
                                                 "id": "sess-1"})

    assert resp.status == 200
    assert await resp.json() == {"closed": "session"}
    assert fake_iterm2.session.closed is True     # forced, see below


async def test_closing_a_tab(client, fake_iterm2):
    tab = fake_iterm2.app.terminal_windows[0].tabs[0]

    resp = await client.post("/api/close", json={"kind": "tab", "id": tab.tab_id})

    assert resp.status == 200
    assert tab.closed is True


async def test_closing_a_window(client, fake_iterm2):
    window = fake_iterm2.app.terminal_windows[0]

    resp = await client.post("/api/close", json={"kind": "window",
                                                 "id": window.window_id})

    assert resp.status == 200
    assert window.closed is True


async def test_closing_is_always_forced(client, fake_iterm2):
    """Unforced, iTerm2 puts a confirmation sheet on the Mac — which someone
    holding a phone will never see, or be able to dismiss."""
    await client.post("/api/close", json={"kind": "session", "id": "sess-1"})

    assert fake_iterm2.session.closed is True     # force=True reached iTerm2


@pytest.mark.parametrize("payload, status", [
    ({"kind": "session"}, 400),                   # no id
    ({"id": "sess-1"}, 400),                      # no kind
    ({"kind": "everything", "id": "sess-1"}, 400),
    ({"kind": "session", "id": "ghost"}, 404),
    ({"kind": "tab", "id": "ghost"}, 404),
    ({"kind": "window", "id": "ghost"}, 404),
])
async def test_close_rejects_nonsense(client, payload, status):
    assert (await client.post("/api/close", json=payload)).status == status


async def test_a_refusal_is_reported(client, fake_iterm2, monkeypatch):
    async def refuse(force=False):
        raise RuntimeError("iTerm2 said no")

    monkeypatch.setattr(fake_iterm2.session, "async_close", refuse)

    resp = await client.post("/api/close", json={"kind": "session",
                                                 "id": "sess-1"})

    assert resp.status == 502
    assert (await resp.json())["message"] == "iTerm2 said no"


async def test_the_list_names_windows_and_tabs(client, fake_iterm2):
    """The phone needs stable ids to aim at — indices shuffle as things open
    and close."""
    body = await (await client.get("/api/sessions")).json()
    window = body["windows"][0]

    assert window["id"] == "win-1"
    assert window["tabs"][0]["id"] == "tab-1"


async def test_a_new_tab_goes_in_the_window_you_named(client, fake_iterm2):
    """The whole point: no guessing which window it landed in."""
    second = FakeWindow([FakeTab([FakeSession("other")], tab_id="tab-other")],
                        window_id="win-2")
    fake_iterm2.app.terminal_windows.append(second)
    first = fake_iterm2.app.terminal_windows[0]

    resp = await client.post("/api/new", json={"kind": "tab",
                                               "window_id": "win-1"})

    assert resp.status == 200
    assert len(first.tabs) == 2          # named window got it...
    assert len(second.tabs) == 1         # ...even though it isn't current


async def test_an_unknown_window_falls_back_rather_than_failing(client, fake_iterm2):
    resp = await client.post("/api/new", json={"kind": "tab",
                                               "window_id": "win-gone"})

    assert resp.status == 200
    assert len(fake_iterm2.app.terminal_windows[0].tabs) == 2
