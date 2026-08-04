"""What termdeck does while iTerm2 isn't running.

Every connection attempt costs an AppleScript asking macOS whether iTerm2 is
running, so a phone left on the session list must not drive one every few
seconds forever — nor fill the log with the same traceback.
"""
import pytest

import server


@pytest.fixture
def refuses(monkeypatch):
    """iTerm2 isn't running: every attempt to connect fails."""
    attempts = []

    async def fail():
        attempts.append(1)
        raise ConnectionRefusedError("iTerm2 not running")

    monkeypatch.setattr(server.iterm2.Connection, "async_create", fail)
    return attempts


async def test_repeated_attempts_are_not_made_back_to_back(refuses):
    bridge = server.Bridge()

    for _ in range(20):
        with pytest.raises(Exception):
            await bridge.connection()

    # One real attempt; the rest were refused from the cooldown.
    assert len(refuses) == 1


async def test_the_wait_is_explained_not_just_imposed(refuses):
    bridge = server.Bridge()
    with pytest.raises(Exception):
        await bridge.connection()

    with pytest.raises(Exception) as caught:
        await bridge.connection()

    assert "iTerm2" in str(caught.value)
    # The hint machinery recognises it, so the phone says something useful.
    assert server.error_payload(caught.value)["hint"]


async def test_backing_off_has_a_ceiling(refuses, monkeypatch):
    """Opening iTerm2 shouldn't mean waiting minutes for the phone to notice."""
    bridge = server.Bridge()
    clock = [0.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])

    for _ in range(12):
        with pytest.raises(Exception):
            await bridge.connection()
        clock[0] = bridge.retry_at            # jump to when it may try again

    assert bridge.retry_at - clock[0] == 0
    assert len(refuses) == 12                  # it kept trying...
    # ...but never waits longer than the cap between attempts.
    assert min(2 ** bridge.failures, server.RETRY_CAP_SECONDS) \
        == server.RETRY_CAP_SECONDS


async def test_recovery_clears_the_cooldown(monkeypatch):
    bridge = server.Bridge()
    state = {"up": False}

    async def maybe():
        if not state["up"]:
            raise ConnectionRefusedError("iTerm2 not running")
        return "a live connection"

    monkeypatch.setattr(server.iterm2.Connection, "async_create", maybe)
    with pytest.raises(Exception):
        await bridge.connection()

    state["up"] = True
    bridge.retry_at = 0.0                      # as it would be after the wait

    assert await bridge.connection() == "a live connection"
    assert bridge.failures == 0


def test_the_same_failure_is_logged_once(capsys, monkeypatch):
    monkeypatch.setattr(server, "_logged_at", {})
    exc = ConnectionRefusedError("iTerm2 not running")

    for _ in range(50):
        server.log_error("connecting", exc)

    printed = capsys.readouterr().err
    assert printed.count("iTerm2 not running") == 1


def test_a_different_failure_still_gets_through(capsys, monkeypatch):
    monkeypatch.setattr(server, "_logged_at", {})

    server.log_error("connecting", ConnectionRefusedError("not running"))
    server.log_error("reading", RuntimeError("something else"))

    printed = capsys.readouterr().err
    assert "not running" in printed
    assert "something else" in printed
