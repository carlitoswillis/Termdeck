"""What the session list needs to answer "which one needs me?" — plus the
cursor, so live typing has somewhere to land."""
import pytest
from conftest import FakeSession

import server


async def panes(client):
    body = await (await client.get("/api/sessions")).json()
    return body["windows"][0]["tabs"][0]["sessions"][0]


@pytest.mark.parametrize("job, busy", [
    ("zsh", False),
    ("-zsh", False),          # a login shell
    ("bash", False),
    ("fish", False),
    ("claude", True),
    ("npm", True),
    ("vim", True),
    ("", False),              # nothing reported: don't cry wolf
])
async def test_a_shell_at_a_prompt_is_not_busy(client, fake_iterm2, job, busy):
    fake_iterm2.session.job = job

    assert (await panes(client))["busy"] is busy


async def test_the_line_count_is_what_makes_new_output_visible(client, fake_iterm2):
    """It only goes up, so the phone can compare it against what it saw last
    time without the server tracking any per-device state."""
    before = (await panes(client))["lines"]

    fake_iterm2.session.info.scrollback_buffer_height += 7

    assert (await panes(client))["lines"] == before + 7


async def test_a_session_that_cannot_be_measured_still_lists(client, fake_iterm2):
    class Unmeasurable(FakeSession):
        async def async_get_line_info(self):
            raise RuntimeError("buried session")

    broken = Unmeasurable("sess-1")
    fake_iterm2.app.terminal_windows[0].tabs[0].sessions[0] = broken

    pane = await panes(client)

    assert pane["id"] == "sess-1"
    assert pane["lines"] == 0


async def test_the_cursor_is_reported_in_absolute_lines(client, fake_iterm2):
    """cursor_coord is relative to the top of the screen; everything else here
    counts from the start of the session."""
    session = fake_iterm2.session          # overflow 0, scrollback 10, screen 5
    session.cursor = (12, 3)               # column 12, fourth line of screen

    state = await server.read_lines(session)

    assert state["cursor"] == [13, 12]     # 10 above the screen + 3


async def test_the_cursor_follows_the_scrollback_overflowing():
    session = FakeSession(overflow=1000, scrollback=500, screen=24)
    session.cursor = (4, 2)

    state = await server.read_lines(session)

    assert state["cursor"] == [1502, 4]


async def test_no_cursor_is_better_than_no_text():
    session = FakeSession()
    session.cursor_error = RuntimeError("screen contents unavailable")

    state = await server.read_lines(session)

    assert state["cursor"] is None
    assert state["lines"]                  # the text still came through
