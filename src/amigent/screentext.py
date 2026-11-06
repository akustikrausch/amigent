"""The front screen's text as amigentd recorded it, for the agent to read.

amigentd (protocol 5) keeps what programs draw with Text(), per window, and
answers TEXT with lines:

    I  seconds_recorded  evicted  used  size
    W  window  x  y  w  h  flags  title      flags: a active, b backdrop, o older
    L  window  x  y  w  h  text

This module turns them into windows with their rows, leaves out rows that a
window in front covers, and says plainly when the text is not enough, so the
agent knows when to take a picture instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Row:
    x: int
    y: int
    w: int
    h: int
    text: str
    hidden: bool = False

    @property
    def centre(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass
class Window:
    id: str
    x: int
    y: int
    w: int
    h: int
    flags: str
    title: str
    rows: list[Row] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return "a" in self.flags

    @property
    def older(self) -> bool:
        return "o" in self.flags

    @property
    def backdrop(self) -> bool:
        return "b" in self.flags

    def covers(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def box(self) -> str:
        return f"({self.x},{self.y} {self.w}x{self.h})"


@dataclass
class ScreenText:
    recording: bool
    seconds: int = 0
    evicted: int = 0
    windows: list[Window] = field(default_factory=list)


def parse(lines: list[str]) -> ScreenText:
    result = ScreenText(recording=False)
    by_id: dict[str, Window] = {}
    for line in lines:
        f = line.split("\t")
        if f[0] == "I" and len(f) >= 3:
            seconds = int(f[1])
            result.recording = seconds >= 0
            result.seconds = max(seconds, 0)
            result.evicted = int(f[2])
        elif f[0] == "W" and len(f) >= 8:
            w = Window(f[1], int(f[2]), int(f[3]), int(f[4]), int(f[5]), f[6], f[7])
            result.windows.append(w)
            by_id[w.id] = w
        elif f[0] == "L" and len(f) >= 7 and f[1] in by_id:
            by_id[f[1]].rows.append(Row(int(f[2]), int(f[3]), int(f[4]), int(f[5]), f[6]))
    # windows come front to back: a row is hidden when one further up covers it
    for i, w in enumerate(result.windows):
        for row in w.rows:
            cx, cy = row.centre
            row.hidden = any(front.covers(cx, cy) for front in result.windows[:i])
    return result


def _matches(w: Window, which: str) -> bool:
    which = which.strip().lower()
    return not which or which == w.id.lower() or which in w.title.lower()


def _in_region(row: Row, region: tuple[int, int, int, int] | None) -> bool:
    if not region:
        return True
    x, y, w, h = region
    cx, cy = row.centre
    return x <= cx < x + (w or 1 << 30) and y <= cy < y + (h or 1 << 30)


def render(st: ScreenText, window: str = "", find: str = "", region=None,
           hidden: bool = False, positions: bool = True) -> str:
    """Text for the agent: per window its rows, top to bottom, and what is missing."""
    if not st.recording:
        return ("amigentd records no text on this Amiga (amigentd before 1.3.0, or started "
                "with NOTEXT). Use amiga_screenshot.")
    out, notes, shown = [], [], 0
    older_empty, empty, behind = [], [], []
    windows = [w for w in st.windows if _matches(w, window)]
    if window and not windows:
        return f"no window on the front screen matches {window!r}; amiga_ui_tree lists them"
    needle = find.lower()
    for w in windows:
        rows = [r for r in w.rows if _in_region(r, region) and (hidden or not r.hidden)]
        if needle:
            rows = [r for r in rows if needle in r.text.lower()]
        covered = sum(1 for r in w.rows if r.hidden) if not hidden else 0
        label = f"{w.title or 'untitled'!r} {_shot(w)}"
        if not rows:
            if needle or region:
                continue
            if covered:
                behind.append(repr(w.title or w.id))
            elif w.older:
                older_empty.append(label)
            elif w.title:  # an untitled backdrop is the desktop: nothing to say
                empty.append(label)
            continue
        out.append(f"window {w.title!r} {w.id} {w.box()}{' active' if w.active else ''}"
                   f"{' (open before recording: rows may be missing)' if w.older else ''}")
        for r in rows:
            where = f"({r.x},{r.y}) " if positions else ""
            out.append(f"  {where}{r.text}{'  [behind another window]' if r.hidden else ''}")
            shown += 1
        if covered:
            out.append(f"  ... {covered} row(s) behind other windows left out")
    if needle and not shown:
        return f"{find!r} is not in the recorded text of the front screen"
    if empty:
        notes.append("no text recorded for " + ", ".join(empty) + ": drawn as pictures or "
                     "another way; amiga_screenshot with that box shows it")
    if older_empty:
        notes.append("open before amigentd started recording, text unknown until they redraw: "
                     + ", ".join(older_empty))
    if behind:
        notes.append("text only behind other windows: " + ", ".join(behind))
    if not out and not notes:
        return ("no text recorded on the front screen: it may show graphics, a game or a "
                "screen of its own. Use amiga_screenshot.")
    if st.evicted:
        notes.append(f"{st.evicted} older runs of text were dropped to make room; a busy window "
                     "may miss rows")
    if out:
        notes.append("text drawn as pictures (icons, many buttons, games) is not here; take a "
                     "screenshot of the part you need when this is not enough")
    return "\n".join(out + [f"note: {n}" for n in notes])


def _shot(w: Window) -> str:
    return f"({w.x},{w.y} {w.w}x{w.h})"


def find_row(st: ScreenText, text: str, window: str = "") -> tuple[Window, Row] | None:
    """The first visible row containing text, exact rows first; for clicking on it."""
    needle = text.strip().lower()
    hits = [(w, r) for w in st.windows if _matches(w, window) for r in w.rows
            if not r.hidden and needle in r.text.lower()]
    exact = [(w, r) for w, r in hits if r.text.strip().lower() == needle]
    return (exact or hits or [None])[0]


def point_of(row: Row, text: str) -> tuple[int, int]:
    """Where to click for text inside row: its share of the row's width."""
    at = row.text.lower().find(text.strip().lower())
    if at < 0 or not row.text:
        return row.centre
    middle = (at + len(text.strip()) / 2) / len(row.text)
    return row.x + int(row.w * middle), row.y + row.h // 2
