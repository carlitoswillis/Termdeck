#!/usr/bin/env python3
"""Print what iTerm2 actually reports for the last lines of the current
session, cell by cell.

Run it on the Mac when something looks wrong on the phone — missing spaces,
colours in the wrong place, characters shifted:

    ./.venv/bin/python tools/inspect-lines.py [count]

For each line it shows the text iTerm2 hands over, then any cell that isn't a
plain single code point: `0cp` is a cell holding no characters at all (either
an unwritten gap or the second half of a wide character), `2cp` is one holding
several (a combining accent). Those are the cases that used to come out wrong.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import iterm2                                    # noqa: E402
import server                                    # noqa: E402


async def main(connection):
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    app = await iterm2.async_get_app(connection)
    session = app.current_window.current_tab.current_session
    info = await session.async_get_line_info()
    total = info.overflow + info.scrollback_buffer_height + info.mutable_area_height
    lines = await session.async_get_contents(max(info.overflow, total - count),
                                             min(count, total))

    for n, line in enumerate(lines):
        rebuilt, _ = server.rebuild_line(line)
        print(f"\n--- line {n} " + "-" * 50)
        print(f"  iTerm2 string : {line.string!r}")
        print(f"  rebuilt       : {rebuilt!r}")
        if line.string != rebuilt:
            print("  DIFFERENT — cells were missing from the string")
        odd, blanks, nuls = [], 0, 0
        x = 0
        while True:
            try:
                piece = line.string_at(x)
            except IndexError:
                break
            if piece == "":
                blanks += 1
                odd.append(f"cell {x}: empty (no code points)")
            elif not piece.strip("\x00"):
                nuls += 1
                odd.append(f"cell {x}: NUL {piece!r}")
            elif len(piece) != 1:
                odd.append(f"cell {x}: {len(piece)}cp {piece!r}")
            x += 1
        print(f"  cells         : {x}  (empty: {blanks}, NUL: {nuls})")
        for entry in odd[:12]:
            print(f"    {entry}")
        if len(odd) > 12:
            print(f"    … and {len(odd) - 12} more")


iterm2.run_until_complete(main)
