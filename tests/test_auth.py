"""The password and the address allowlist — the two things standing between a
tailnet and a shell."""
import pytest
from aiohttp.test_utils import TestClient, TestServer

import server

SECRET = "cookie-value-for-tests"


@pytest.fixture
async def locked():
    """A server with a password set, and a client that doesn't follow
    redirects so the login hop is visible."""
    password = server.Password(stored=server.hash_password("hunter2"))
    async with TestClient(TestServer(
            server.make_app(password, secret=SECRET))) as client:
        yield client


def test_a_password_survives_a_round_trip():
    stored = server.hash_password("correct horse")

    assert server.password_matches("correct horse", stored)
    assert not server.password_matches("Correct Horse", stored)
    assert not server.password_matches("", stored)


def test_the_stored_form_is_not_the_password():
    stored = server.hash_password("hunter2")

    assert "hunter2" not in stored
    assert stored.count("$") == 1                 # salt$digest


def test_the_same_password_stores_differently_each_time():
    assert server.hash_password("hunter2") != server.hash_password("hunter2")


def test_a_corrupt_password_file_just_fails_the_login():
    assert not server.password_matches("hunter2", "not-a-valid-hash")


def test_the_environment_can_supply_it():
    password = server.Password(plain="from-env")

    assert password.required
    assert password.check("from-env")
    assert not password.check("something else")


def test_no_password_means_no_login():
    assert not server.Password().required


async def test_locked_pages_show_the_login_form(locked):
    for path in ("/", "/api/sessions", "/static/index.html"):
        resp = await locked.get(path, allow_redirects=False)
        assert resp.status == 401, path
        assert "type=password" in await resp.text()


async def test_the_wrong_password_is_refused(locked):
    resp = await locked.post("/login", data={"password": "nope"},
                             allow_redirects=False)

    assert resp.status == 401
    assert "Wrong password" in await resp.text()
    assert server.COOKIE not in resp.cookies


async def test_the_right_password_sets_a_cookie(locked):
    resp = await locked.post("/login", data={"password": "hunter2"},
                             allow_redirects=False)

    assert resp.status == 302
    assert resp.headers["Location"] == "/"
    cookie = resp.cookies[server.COOKIE]
    assert cookie.value == SECRET
    assert cookie["httponly"]
    assert cookie["samesite"] == "Strict"


async def test_the_cookie_then_opens_everything(locked):
    await locked.post("/login", data={"password": "hunter2"})

    assert (await locked.get("/")).status == 200
    assert (await locked.get("/api/sessions")).status == 200
    async with locked.ws_connect("/api/ws") as ws:
        await ws.send_json({"type": "watch", "session_id": "sess-1"})
        assert (await ws.receive_json())["type"] == "screen"


async def test_the_websocket_is_not_a_back_door(locked):
    """Websockets can't carry an Authorization header, which is why this is a
    cookie — the handshake has to be refused too."""
    with pytest.raises(Exception):
        async with locked.ws_connect("/api/ws"):
            pass


async def test_healthz_stays_open_for_the_installer(locked):
    resp = await locked.get("/healthz")

    assert resp.status == 200
    assert (await resp.text()).strip() == "ok"


async def test_without_a_password_nothing_is_locked(client):
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
    async with TestClient(TestServer(server.make_app())) as client:
        resp = await client.get("/")

        assert resp.status == 403
        assert "termdeck" not in (await resp.text())
