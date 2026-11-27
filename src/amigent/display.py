"""Which video output the picture is on: the graphics card's or the chipset's (#1).

A graphics card and the chipset feed different outputs. On a PiStorm the card
goes out over the Pi's HDMI and the chipset over the Amiga's own RGB port, so a
program that opens a chipset screen moves the picture to the other monitor
input. amigentd reads both kinds; these helpers say which one is in front, in
words for the AI agent and as a small record for the remote desktop.

All of them take the screen list as Amigentd.screens() gives it.
"""

from __future__ import annotations

DUALPF_KEY = 0x400  # graphics/modeid.h: dual playfield
INVALID_ID = 0xFFFFFFFF


def mode_label(s: dict) -> str:
    """The mode's name from the display database, "" where amigentd didn't say."""
    if s.get("mode_name"):
        return s["mode_name"]
    mode = s.get("mode")
    if mode is None or mode == INVALID_ID:
        return ""
    return f"mode {mode:#010x}"


def has_card(found: list) -> bool:
    """A graphics card is in use: one of the screens lives on it."""
    return any(s.get("rtg") for s in found)


def front(found: list) -> dict | None:
    return next((s for s in found if s.get("front")), found[0] if found else None)


def state(found: list) -> dict:
    """What the remote desktop needs: the front screen's kind and mode.

    output is "card" or "chipset"; moved is True when the picture left the
    graphics card's output for the chipset's, the case worth telling about.
    An empty dict when nothing is known."""
    s = front(found)
    if not s:
        return {}
    card = has_card(found)
    chipset = not s.get("rtg")
    foreign = bool(found[0].get("foreign_view"))
    limits = ["planar"] if chipset else []
    if chipset and (s.get("mode") or 0) & DUALPF_KEY and s.get("mode") != INVALID_ID:
        limits.append("dualpf")
    if chipset and foreign:
        limits.append("own_view")
    return {"output": "chipset" if chipset else "card", "mode": mode_label(s),
            "title": s.get("title", ""), "card": card, "moved": chipset and card,
            "limits": limits}


def where(found: list) -> str:
    """One line for amiga_status and screenshots when the picture is on the
    chipset output of an Amiga that also has a graphics card; "" otherwise."""
    st = state(found)
    if not st.get("moved"):
        return ""
    mode = f" ({st['mode']})" if st["mode"] else ""
    line = (f"the front screen {st['title']!r} is a chipset screen{mode}: it shows on the "
            "Amiga's own video output, not on the graphics card's (on a PiStorm: the RGB port, "
            "not HDMI); tell the user to look at that monitor or input")
    line += ("; the picture here is read from its bitplanes, so sprites and colour changes "
             "during the frame (copper) are missing")
    if "dualpf" in st["limits"]:
        line += ("; it is a dual playfield screen, which the picture here shows only in part")
    return line
