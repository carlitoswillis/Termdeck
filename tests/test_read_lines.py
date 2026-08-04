"""Line-range math: iTerm2 numbers lines absolutely and forgets the oldest
ones once scrollback fills, so every read has to start at or after
`info.overflow`."""
import asyncio

import pytest
from conftest import FakeSession, FakeTransaction

import server


async def test_tail_returns_the_last_n_lines():
    session = FakeSession(overflow=1000, scrollback=500, screen=24)

    state = await server.read_lines(session, count=400)

    assert state["first"] == 1000
    assert state["total"] == 1524
    assert state["start"] == 1124
    assert state["lines"][0] == "line 1124"
    assert state["lines"][-1] == "line 1523"
    assert len(state["lines"]) == 400


async def test_start_below_overflow_is_clamped_up():
    """Asking from line 0 on a session that has overflowed used to return a
    short, wrongly-labelled chunk."""
    session = FakeSession(overflow=1000, scrollback=500, screen=24)

    state = await server.read_lines(session, start=0, count=400)

    assert state["start"] == 1000
    assert session.requests == [(1000, 400)]
    assert state["lines"][0] == "line 1000"
    assert len(state["lines"]) == 400


async def test_the_pane_width_rides_along():
    """The phone sizes text from the pane's real column count, so it has to
    come with every frame."""
    session = FakeSession(cols=132, screen=40)

    state = await server.read_lines(session)

    assert state["cols"] == 132
    assert state["rows"] == 40


async def test_a_session_without_a_grid_reports_zero():
    session = FakeSession()
    del session.grid_size

    state = await server.read_lines(session)

    assert state["cols"] == 0


async def test_short_session_returns_everything_it_has():
    session = FakeSession(overflow=0, scrollback=3, screen=2)

    state = await server.read_lines(session, count=400)

    assert state["start"] == 0
    assert state["total"] == 5
    assert state["lines"] == [f"line {n}" for n in range(5)]


async def test_start_past_the_end_returns_nothing():
    session = FakeSession(overflow=10, scrollback=5, screen=5)

    state = await server.read_lines(session, start=9999, count=400)

    assert state["start"] == 20
    assert state["lines"] == []
    assert session.requests == []      # no pointless round trip


async def test_negative_count_is_harmless():
    session = FakeSession(overflow=0, scrollback=10, screen=5)

    state = await server.read_lines(session, start=3, count=-5)

    assert state["lines"] == []


class SlowSession(FakeSession):
    """Blocks inside the transaction until released."""

    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def async_get_contents(self, first_line, number_of_lines):
        self.entered.set()
        await self.release.wait()
        return await super().async_get_contents(first_line, number_of_lines)


async def test_a_cancelled_read_still_closes_its_transaction():
    """An abandoned transaction would block every iTerm2 API client, so a
    watcher going away mid-read must not leave one open."""
    session = SlowSession()
    task = asyncio.create_task(server.read_lines(session))
    await asyncio.wait_for(session.entered.wait(), 1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert FakeTransaction.depth == 1        # still held by the shielded read

    session.release.set()
    for _ in range(100):
        if FakeTransaction.depth == 0:
            break
        await asyncio.sleep(0.01)

    assert FakeTransaction.depth == 0
    assert not server.bridge.txn_lock.locked()


async def test_reads_happen_inside_one_transaction():
    session = FakeSession()

    await server.read_lines(session)

    assert FakeTransaction.max_depth == 1
    assert FakeTransaction.depth == 0
