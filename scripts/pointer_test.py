"""Check that the pointer lands where it was sent, on chipset screens of every mode.

Usage: pointer_test.py HOST [PORT] [TEST]

TEST is amiga/pointer-test built on the Amiga (with amiga/build-amigentd,
SOURCE pointer-test.c TO pointer-test), default RAM:pointer-test. It opens a
screen in each mode, also dragged down, and writes where Intuition sees the
pointer; amigentd on PORT moves it there. A point below a dragged-down
screen's visible part stops at the display's last line, as it does by hand.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from amigent.amigentd import Amigentd  # noqa: E402
from amigent.channel import Commands  # noqa: E402
from amigent.errors import AmigaError  # noqa: E402

MODES = [  # name, display id, width, height, top
    ("lores", "00000000", 320, 256, 0), ("hires", "00008000", 640, 256, 0),
    ("hires lace", "00008004", 640, 512, 0), ("superhires", "00008020", 1280, 256, 0),
    ("lores dragged", "00000000", 320, 256, 100), ("hires dragged", "00008000", 640, 256, 120),
]
POS = "RAM:pointer-test.pos"


def read_pos(cmd) -> tuple[int, int, int]:
    """The test program rewrites the file ten times a second; now and then it is in use."""
    for _ in range(10):
        try:
            x, y, top = (int(v) for v in cmd.read_file(POS).split())
            return x, y, top
        except (AmigaError, ValueError):
            time.sleep(0.1)
    raise AmigaError(f"cannot read {POS}")


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 6801
    test = sys.argv[3] if len(sys.argv) > 3 else "RAM:pointer-test"
    cmd = Commands(host, screen_port=port, timeout=60)  # files and commands on the same amigentd
    screen = Amigentd(host, port)
    screen.connect()
    wrong = 0
    for name, mode, w, h, top in MODES:
        cmd.exec(f"Delete >NIL: {POS} QUIET")
        cmd.exec(f"Run >NIL: {test} {mode} {w} {h} {top} 20 >T:pointer-test.out")
        # Ready once it writes positions, or done when the mode is missing; a 68000
        # takes its time to open a screen. Its own output comes only at the end.
        said, ready, deadline = "", False, time.monotonic() + 20
        while not ready and "no screen" not in said and time.monotonic() < deadline:
            time.sleep(1)
            ready = cmd.exec(f"List >NIL: {POS}").rc == 0
            try:  # in use while the program runs, which says what it has to say at the end
                said = cmd.read_file("T:pointer-test.out").decode("latin-1")
            except AmigaError:
                said = ""
        if "no screen" in said:
            print(f"{name:14} no such mode here")
            continue
        if not ready:
            print(f"{name:14} the test program did not start")
            wrong += 1
            continue
        for x, y in ((10, 20), (w // 2, h // 2), (w - 5, h - 40)):
            screen.move(x, y)
            time.sleep(0.6)
            gx, gy, gtop = read_pos(cmd)
            last = 255 - gtop if gtop else None  # a 256-line display's last visible row
            ok = (gx, gy) == (x, y) or (last is not None and y > last and (gx, gy) == (x, last))
            wrong += not ok
            print(f"{name:14} sent {x:4},{y:4}  got {gx:4},{gy:4}  {'ok' if ok else 'WRONG'}")
        cmd.exec("Status >T:pointer-test.pid COMMAND " + test)
        pid = cmd.read_file("T:pointer-test.pid").decode().strip()
        if pid:
            cmd.exec(f"Break {pid} C")
        time.sleep(1)
    print("all right" if not wrong else f"{wrong} wrong")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
