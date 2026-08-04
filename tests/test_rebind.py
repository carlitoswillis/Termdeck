"""Sleep/wake can hand the Mac a different tailnet address. Nothing crashes —
the old socket just stops being reachable — so the server has to notice."""
import asyncio

import server


class FakeSite:
    def __init__(self, runner=None, host=None, port=None):
        self.host = host
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True


async def run_watcher_until(sites, done, timeout=2):
    task = asyncio.create_task(server.rebind_watcher(None, sites))
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not done() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_the_binding_moves_to_the_new_address(monkeypatch):
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)
    monkeypatch.setattr(server, "REBIND_SECONDS", 0.01)
    monkeypatch.setattr(server, "tailscale_ip", lambda: "100.2.2.2")
    old = FakeSite(host="100.1.1.1")
    sites = {"127.0.0.1": FakeSite(host="127.0.0.1"), "100.1.1.1": old}

    await run_watcher_until(sites, lambda: "100.2.2.2" in sites)

    assert "100.2.2.2" in sites
    assert "100.1.1.1" not in sites
    assert old.stopped
    assert "127.0.0.1" in sites          # localhost is never disturbed


async def test_an_unchanged_address_is_left_alone(monkeypatch):
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)
    monkeypatch.setattr(server, "REBIND_SECONDS", 0.01)
    monkeypatch.setattr(server, "tailscale_ip", lambda: "100.1.1.1")
    existing = FakeSite(host="100.1.1.1")
    sites = {"127.0.0.1": FakeSite(host="127.0.0.1"), "100.1.1.1": existing}

    await run_watcher_until(sites, lambda: False, timeout=0.2)

    assert not existing.stopped
    assert set(sites) == {"127.0.0.1", "100.1.1.1"}


async def test_tailscale_coming_up_later_gets_bound(monkeypatch):
    """Started before Tailscale was ready: localhost only, then it appears."""
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)
    monkeypatch.setattr(server, "REBIND_SECONDS", 0.01)
    monkeypatch.setattr(server, "tailscale_ip", lambda: "100.3.3.3")
    sites = {"127.0.0.1": FakeSite(host="127.0.0.1")}

    await run_watcher_until(sites, lambda: "100.3.3.3" in sites)

    assert set(sites) == {"127.0.0.1", "100.3.3.3"}


async def test_a_failing_tailscale_cli_does_not_kill_the_watcher(monkeypatch):
    monkeypatch.setattr(server.web, "TCPSite", FakeSite)
    monkeypatch.setattr(server, "REBIND_SECONDS", 0.01)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("tailscale not responding")
        return "100.4.4.4"

    monkeypatch.setattr(server, "tailscale_ip", flaky)
    sites = {"127.0.0.1": FakeSite(host="127.0.0.1")}

    await run_watcher_until(sites, lambda: "100.4.4.4" in sites)

    assert "100.4.4.4" in sites
