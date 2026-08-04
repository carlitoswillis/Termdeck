"""Turning iTerm2's per-cell styles into runs the browser can paint."""
from conftest import FakeColor, FakeLine, FakeStyle, PLAIN

import server


def test_a_plain_line_carries_no_styles():
    assert server.line_runs(FakeLine("hello")) is None


def test_one_run_per_stretch_of_shared_style():
    red = FakeStyle(fg=FakeColor(standard=1))
    line = FakeLine("ERR ok", [red] * 3 + [PLAIN] * 3)

    assert server.line_runs(line) == [[3, 1, None, 0], [3, None, None, 0]]


def test_true_colour_becomes_hex():
    teal = FakeStyle(fg=FakeColor(rgb=(0, 168, 170)))

    assert server.line_runs(FakeLine("hi", [teal] * 2)) == [[2, "#00a8aa", None, 0]]


def test_attributes_pack_into_one_flag_field():
    fancy = FakeStyle(bold=True, underline=True, inverse=True)
    expected = server.BOLD | server.UNDERLINE | server.INVERSE

    assert server.line_runs(FakeLine("x", [fancy])) == [[1, None, None, expected]]


def test_background_colour_survives():
    marked = FakeStyle(bg=FakeColor(standard=226))

    assert server.line_runs(FakeLine("!", [marked])) == [[1, None, 226, 0]]


def test_identical_but_separate_styles_stay_separate_runs():
    """Runs are found by object identity, matching how iTerm2 packs them, so
    two equal-looking styles are two runs — correct, just not merged."""
    a, b = FakeStyle(bold=True), FakeStyle(bold=True)

    assert server.line_runs(FakeLine("ab", [a, b])) == [
        [1, None, None, server.BOLD], [1, None, None, server.BOLD]]


def test_payload_only_mentions_styled_lines():
    red = FakeStyle(fg=FakeColor(standard=1))
    lines = [FakeLine("plain"), FakeLine("hot", [red] * 3), FakeLine("plain2")]

    text, styles = server.line_payload(lines)

    assert text == ["plain", "hot", "plain2"]
    assert list(styles) == ["1"]           # only the middle line
    assert styles["1"] == [[3, 1, None, 0]]


def test_a_broken_style_never_costs_us_the_text():
    class Exploding(FakeLine):
        def style_at(self, x):
            raise RuntimeError("style decode failed")

    text, styles = server.line_payload([Exploding("still here")])

    assert text == ["still here"]
    assert styles == {}


async def test_styles_ride_along_with_the_frame(client, fake_iterm2):
    green = FakeStyle(fg=FakeColor(standard=2))
    fake_iterm2.session.line_styles = lambda n: [green] * len(f"line {n}")

    resp = await client.get("/api/scrollback",
                            params={"session_id": "sess-1", "count": "2"})

    body = await resp.json()
    assert body["styles"]["0"][0][1] == 2       # fg = palette 2
