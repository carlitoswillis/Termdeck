"""Recovering when iTerm2 restarts underneath us.

The iterm2 library keeps one global App and refreshes it on whatever
connection built it. After iTerm2 quits, that connection is dead — and unless
the global is cleared, every reconnection attempt refreshes the dead one and
fails again, forever, while the process itself looks perfectly healthy.
"""
import pytest

import server


class DeadConnection(Exception):
    """What websockets raises when the far end went away."""

    def __init__(self):
        super().__init__("no close frame received or sent")


class Library:
    """Stands in for iterm2's module-level App cache."""

    def __init__(self):
        self.instance = None
        self.alive = False          # has iTerm2 come back yet?
        self.constructed = 0

    async def async_get_app(self, _connection):
        if not self.alive:
            # Nothing works while iTerm2 is gone, built fresh or refreshed.
            raise DeadConnection()
        if self.instance is None:
            self.constructed += 1
            self.instance = self
        return self.instance

    async def async_refresh(self):
        if not self.alive:
            raise DeadConnection()

    def invalidate(self):
        self.instance = None


@pytest.fixture
def library(monkeypatch):
    lib = Library()
    monkeypatch.setattr(server.iterm2, "async_get_app", lib.async_get_app)
    monkeypatch.setattr(server, "forget_cached_app", lib.invalidate)

    async def fake_create():
        return object()

    monkeypatch.setattr(server.iterm2.Connection, "async_create", fake_create)
    return lib


async def test_a_restarted_iterm2_is_recovered_from(library):
    """The exact sequence: connected, iTerm2 restarts, next request."""
    bridge = server.Bridge()
    library.alive = True
    await bridge.app()                      # connected and cached

    library.alive = False                   # iTerm2 quits
    with pytest.raises(Exception):
        await bridge.app(refresh=True)
    library.alive = True                    # iTerm2 is back

    recovered = await bridge.app(refresh=True)

    assert recovered is library
    assert library.constructed == 2         # a genuinely new App, not the corpse


async def test_reset_clears_the_libraries_cached_app(library):
    bridge = server.Bridge()
    library.alive = True
    await bridge.app()
    assert library.instance is not None

    bridge.reset()

    assert library.instance is None


def test_a_dead_socket_is_explained_rather_than_quoted():
    payload = server.error_payload(DeadConnection())

    assert "restarted" in payload["hint"]
    assert "close frame" not in payload["hint"]
