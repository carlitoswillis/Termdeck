"""Turning iTerm2's per-cell styles into runs the browser can paint."""
from conftest import FakeColor, FakeLine, FakeStyle, PLAIN

import server


def test_cells_a_tui_never_wrote_come_back_as_spaces():
    """The bug this exists to stop: iTerm2's line text is just the cells'
    code points joined, so a gap a TUI left by moving the cursor instead of
    printing spaces closed up — "foo   bar" arrived as "foobar"."""
    line = FakeLine("", cells=["f", "o", "o", "", "", "", "b", "a", "r"])
    assert line.string == "foobar"          # what we used to send

    text, _ = server.line_payload([line])

    assert text == ["foo   bar"]


def test_the_second_half_of_a_wide_character_is_not_a_space():
    """An empty cell also means 'the previous character is two wide'. Putting
    a space there would shift every CJK and emoji line to the right."""
    line = FakeLine("", cells=["世", "", "界", "", "!"])

    text, _ = server.line_payload([line])

    assert text == ["世界!"]


def test_runs_are_measured_in_code_units_not_cells():
    """One cell can hold several code points — e + a combining accent — and a
    run length that counts cells would drift the colours along the line."""
    red = FakeStyle(fg=FakeColor(standard=1))
    line = FakeLine("", cells=["é", "x"], styles=[red, PLAIN])

    text, styles = server.line_payload([line])

    assert text == ["éx"]
    assert styles["0"][0][0] == 2           # two code units, one cell


def test_trailing_blank_cells_are_not_sent_as_spaces():
    line = FakeLine("", cells=["h", "i"] + [""] * 70)

    text, _ = server.line_payload([line])

    assert text == ["hi"]


def test_styles_survive_the_gaps_being_filled():
    red = FakeStyle(fg=FakeColor(standard=1))
    line = FakeLine("", cells=["a", "", "b"], styles=[red, None, red])

    text, styles = server.line_payload([line])

    assert text == ["a b"]
    assert styles["0"] == [[1, 1, None, 0], [1, None, None, 0], [1, 1, None, 0]]


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
