"""The page and the server have to be able to notice when they've drifted
apart — a pull updates static files at once but leaves the old Python running
until something restarts it, and until now that just looked like features
quietly not working."""
import re
from pathlib import Path

import server

INDEX = Path(__file__).resolve().parents[1] / "static" / "index.html"


def test_the_page_asks_for_the_version_the_server_ships():
    wanted = int(re.search(r"const NEEDS_VERSION = (\d+)", INDEX.read_text()).group(1))

    assert wanted == server.VERSION, (
        "static/index.html expects a different server version than server.py "
        "provides — bump them together"
    )


async def test_healthz_reports_the_version(client):
    body = await (await client.get("/healthz")).json()

    assert body["version"] == server.VERSION
    assert body["ok"] is True
