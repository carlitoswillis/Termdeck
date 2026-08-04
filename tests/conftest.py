"""Fakes standing in for iTerm2, so the server can be tested without a Mac."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server  # noqa: E402


class FakeLineInfo:
    def __init__(self, overflow, scrollback, screen):
        self.overflow = overflow
        self.scrollback_buffer_height = scrollback
        self.mutable_area_height = screen


class FakeColor:
    def __init__(self, standard=None, rgb=None):
        self.is_standard = standard is not None
        self.is_rgb = rgb is not None
        self.standard = standard
        self.rgb = SimpleNamespace(red=rgb[0], green=rgb[1], blue=rgb[2]) if rgb else None


class FakeStyle:
    def __init__(self, fg=None, bg=None, **flags):
        self.fg_color = fg
        self.bg_color = bg
        for name in ("bold", "italic", "underline", "inverse", "faint",
                     "strikethrough"):
            setattr(self, name, flags.get(name, False))


PLAIN = FakeStyle()


class FakeLine:
    """A line as iTerm2 describes it: a list of cells, each holding zero or
    more code points. `string` is only ever those code points joined up —
    which is why a cell holding none vanishes from it entirely.

    iTerm2 also hands back one shared CellStyle object per run, which is what
    lets the server find run boundaries by identity.
    """

    def __init__(self, string, styles=None, cells=None):
        self.cells = list(string) if cells is None else cells
        self.string = "".join(self.cells)
        self.styles = styles if styles is not None else [PLAIN] * len(self.cells)

    def string_at(self, x):
        return self.cells[x]           # IndexError past the end, as iTerm2 does

    def style_at(self, x):
        return self.styles[x] if 0 <= x < len(self.styles) else None


class FakeSession:
    """A session whose line N always reads "line N", so tests can assert on
    which absolute line numbers came back."""

    def __init__(self, session_id="sess-1", overflow=0, scrollback=10, screen=5,
                 cols=80):
        self.session_id = session_id
        self.name = "shell"
        self.info = FakeLineInfo(overflow, scrollback, screen)
        self.grid_size = SimpleNamespace(width=cols, height=screen)
        self.sent = []
        self.requests = []
        self.activations = []
        self.activate_error = None
        self.line_styles = None       # tests can hand back per-line styles
        self.streamer = None
        self.streamer_error = None
        self.resizes = []
        self.resize_error = None
        self.job = "zsh"
        self.cursor = (0, 0)          # (x, y), y relative to the screen top
        self.cursor_error = None

    async def async_get_line_info(self):
        return self.info

    async def async_get_contents(self, first_line, number_of_lines):
        self.requests.append((first_line, number_of_lines))
        first = self.info.overflow
        total = first + self.info.scrollback_buffer_height + self.info.mutable_area_height
        # iTerm2 returns a subset when the range runs past what it still has.
        lo = max(first_line, first)
        hi = min(first_line + number_of_lines, total)
        return [FakeLine(f"line {n}", self.line_styles(n) if self.line_styles else None)
                for n in range(lo, max(lo, hi))]

    async def async_get_variable(self, name):
        return {"jobName": self.job, "path": "/Users/x/work",
                "tty": "/dev/ttys001", "autoName": "shell"}.get(name)

    async def async_get_screen_contents(self):
        if self.cursor_error:
            raise self.cursor_error
        first = self.info.overflow + self.info.scrollback_buffer_height
        return SimpleNamespace(cursor_coord=SimpleNamespace(x=self.cursor[0],
                                                            y=self.cursor[1]),
                               number_of_lines_above_screen=first)

    async def async_send_text(self, text, suppress_broadcast=False):
        self.sent.append(text)

    async def async_set_grid_size(self, size):
        if self.resize_error:
            raise self.resize_error
        self.resizes.append((size.width, size.height))
        self.grid_size = SimpleNamespace(width=size.width, height=size.height)

    def get_screen_streamer(self, want_contents=True):
        if self.streamer_error:
            raise self.streamer_error
        self.streamer = FakeStreamer()
        return self.streamer

    async def async_activate(self, select_tab=True, order_window_front=True):
        if self.activate_error:
            raise self.activate_error
        self.activations.append((select_tab, order_window_front))


class FakeStreamer:
    """iTerm2's screen streamer: async_get() blocks until the screen changes."""

    def __init__(self):
        self.changed = asyncio.Event()
        self.entered = False
        self.exited = False
        self.gets = 0

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def async_get(self, style=False):
        self.gets += 1
        await self.changed.wait()
        self.changed.clear()
        return None

    def push(self):
        self.changed.set()


class FakeTab:
    def __init__(self, sessions, tab_id="tab-1"):
        self.sessions = sessions
        self.tab_id = tab_id


class FakeWindow:
    counter = 0

    def __init__(self, tabs):
        self.tabs = tabs
        self.current_tab = tabs[0] if tabs else None
        self.refuse_tab = False

    async def async_create_tab(self, profile=None, command=None, index=None,
                               profile_customizations=None):
        if self.refuse_tab:
            return None
        FakeWindow.counter += 1
        tab = FakeTab([FakeSession(f"new-{FakeWindow.counter}")],
                      tab_id=f"tab-new-{FakeWindow.counter}")
        self.tabs.append(tab)
        return tab


class FakeApp:
    def __init__(self, windows):
        self.terminal_windows = windows
        self.refreshes = 0
        self.activations = []

    @property
    def current_window(self):
        return self.terminal_windows[-1] if self.terminal_windows else None

    async def async_refresh(self):
        self.refreshes += 1

    async def async_activate(self, raise_all_windows=True, ignoring_other_apps=False):
        self.activations.append((raise_all_windows, ignoring_other_apps))

    def get_session_by_id(self, session_id, include_buried=True):
        for window in self.terminal_windows:
            for tab in window.tabs:
                for session in tab.sessions:
                    if session.session_id == session_id:
                        return session
        return None


class FakeTransaction:
    """iterm2.Transaction stand-in; records that reads were wrapped in one."""
    depth = 0
    max_depth = 0

    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        FakeTransaction.depth += 1
        FakeTransaction.max_depth = max(FakeTransaction.max_depth,
                                        FakeTransaction.depth)

    async def __aexit__(self, *exc):
        FakeTransaction.depth -= 1


@pytest.fixture(autouse=True)
def fake_iterm2(monkeypatch):
    """Point the bridge at a fake app and connection."""
    sess = FakeSession()
    app = FakeApp([FakeWindow([FakeTab([sess])])])
    FakeTransaction.depth = FakeTransaction.max_depth = 0
    FakeWindow.counter = 0

    class WindowFactory:
        """Stands in for iterm2.Window, whose async_create is a staticmethod."""
        refuse = False

        @staticmethod
        async def async_create(connection, profile=None, command=None,
                               profile_customizations=None):
            if WindowFactory.refuse:
                return None
            FakeWindow.counter += 1
            n = FakeWindow.counter
            window = FakeWindow([FakeTab([FakeSession(f"win-{n}")],
                                         tab_id=f"tab-win-{n}")])
            app.terminal_windows.append(window)
            return window

    async def fake_connection():
        return "fake-connection"

    async def fake_app(refresh=False):
        if refresh:
            await app.async_refresh()
        return app

    monkeypatch.setattr(server.bridge, "connection", fake_connection)
    monkeypatch.setattr(server.bridge, "app", fake_app)
    monkeypatch.setattr(server.iterm2, "Transaction", FakeTransaction)
    monkeypatch.setattr(server.iterm2, "Window", WindowFactory)
    return SimpleNamespace(app=app, session=sess, windows=WindowFactory)


@pytest.fixture
async def client():
    async with TestClient(TestServer(server.make_app())) as test_client:
        yield test_client
