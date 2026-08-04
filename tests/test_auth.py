"""The token and the address allowlist — the two things standing between a
tailnet and a shell."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

import server

TOKEN = "test-token-abc"


@pytest.fixture
async def secured():
    """A client that does NOT follow redirects, so we can see the cookie hop."""
    async with TestClient(TestServer(server.make_app(token=TOKEN))) as client:
        yield client


async def test_no_token_is_refused(secured):
    for path in ("/", "/api/sessions", "/static/index.html"):
        resp = await secured.get(path, allow_redirects=False)
        assert resp.status == 401, path


async def test_wrong_token_is_refused(secured):
    resp = await secured.get("/", params={"t": "not-it"}, allow_redirects=False)
    assert resp.status == 401


async def test_a_good_token_becomes_a_cookie(secured):
    resp = await secured.get("/", params={"t": TOKEN}, allow_redirects=False)

    assert resp.status == 302
    assert resp.headers["Location"] == "/"        # token dropped from the URL
    cookie = resp.cookies[server.COOKIE]
    assert cookie.value == TOKEN
    assert cookie["httponly"]
    assert cookie["samesite"] == "Strict"


async def test_the_cookie_then_opens_everything(secured):
    await secured.get("/", params={"t": TOKEN})   # follow the redirect, keep the cookie

    assert (await secured.get("/")).status == 200
    assert (await secured.get("/api/sessions")).status == 200
    async with secured.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        assert (await ws.receive_json())["type"] == "screen"


async def test_the_websocket_is_not_a_back_door(secured):
    """Websockets can't carry an Authorization header, which is exactly why
    the token lives in a cookie — the handshake has to be refused too."""
    with pytest.raises(Exception):
        async with secured.ws_connect("/api/ws"):
            pass


async def test_healthz_stays_open_for_the_installer(secured):
    resp = await secured.get("/healthz")
    assert resp.status == 200
    assert (await resp.text()).strip() == "ok"


async def test_no_token_configured_means_no_auth(client):
    assert (await client.get("/")).status == 200


@pytest.mark.parametrize("remote, allowed", [
    ("127.0.0.1", True),
    ("::1", True),
    ("100.64.0.1", True),            # tailnet
    ("100.72.206.61", True),
    ("::ffff:100.72.206.61", True),  # same peer over v6-mapped v4
    ("192.168.1.50", False),         # LAN, unless --lan
    ("10.0.0.9", False),
    ("8.8.8.8", False),
    ("203.0.113.7", False),
    ("", False),
    (None, False),
    ("not-an-ip", False),
])
def test_who_may_connect(remote, allowed):
    assert server.peer_allowed(remote, server.allowed_networks()) is allowed


@pytest.mark.parametrize("remote, allowed", [
    ("192.168.1.50", True),
    ("10.0.0.9", True),
    ("100.64.0.1", True),
    ("8.8.8.8", False),
])
def test_lan_opt_in(remote, allowed):
    assert server.peer_allowed(remote, server.allowed_networks(allow_lan=True)) is allowed


async def test_a_blocked_address_gets_nothing_useful(monkeypatch):
    """A rejected peer shouldn't learn what's running here."""
    monkeypatch.setattr(server, "peer_allowed", lambda *a: False)
    async with TestClient(TestServer(server.make_app(token=TOKEN))) as client:
        resp = await client.get("/")
        assert resp.status == 403
        assert "termdeck" not in (await resp.text())
