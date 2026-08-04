"""Rebuilding a line walks every one of its cells — 20ms for a screenful of a
wide pane, which cannot happen thirty times a second. Scrollback can't change
once it has scrolled off the mutable area, so it's built once."""
from conftest import FakeColor, FakeLine, FakeStyle, PLAIN

import server


class CountingLine(FakeLine):
    """Counts how often its cells actually get walked."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.walks = 0

    def string_at(self, x):
        if x == 0:
            self.walks += 1
        return super().string_at(x)


def frame(lines, first=0, frozen=None):
    return server.line_payload(lines, "sess-1", first, frozen)


def test_history_is_rebuilt_once():
    lines = [CountingLine("line one"), CountingLine("line two")]

    frame(lines, first=0, frozen=10)          # both below the mutable area
    frame(lines, first=0, frozen=10)
    frame(lines, first=0, frozen=10)

    assert [line.walks for line in lines] == [1, 1]


def test_the_mutable_area_is_always_rebuilt():
    """The screen is where things change, so it never comes from the cache."""
    lines = [CountingLine("still moving")]

    frame(lines, first=20, frozen=10)          # line 20 is inside the screen
    frame(lines, first=20, frozen=10)

    assert lines[0].walks == 2


def test_a_changed_line_is_not_served_from_the_cache():
    """A rewrap renumbers the buffer, so a cached line number can come back
    holding something else entirely."""
    frame([FakeLine("before")], first=3, frozen=10)

    text, _ = frame([FakeLine("after")], first=3, frozen=10)

    assert text == ["after"]


def test_the_cache_keeps_the_styles_with_the_text():
    red = FakeStyle(fg=FakeColor(standard=1))
    line = FakeLine("", cells=list("hot"), styles=[red] * 3)

    first_text, first_styles = frame([line], first=0, frozen=10)
    again_text, again_styles = frame([line], first=0, frozen=10)

    assert again_text == first_text
    assert again_styles == first_styles == {"0": [[3, 1, None, 0]]}


def test_sessions_do_not_share_cached_lines():
    server.line_payload([FakeLine("mine")], "sess-a", 0, 10)

    text, _ = server.line_payload([FakeLine("yours")], "sess-b", 0, 10)

    assert text == ["yours"]


def test_the_cache_does_not_grow_without_bound(monkeypatch):
    monkeypatch.setattr(server, "CACHE_LIMIT", 50)

    for n in range(400):
        server.line_payload([FakeLine(f"line {n}")], "sess-1", n, 10_000)

    assert len(server._line_cache) <= 51


def test_no_session_means_no_caching():
    """Callers that don't say which session — the diagnostics dump — must not
    poison the cache or read from it."""
    lines = [CountingLine("plain")]

    server.line_payload(lines)
    server.line_payload(lines)

    assert lines[0].walks == 2
