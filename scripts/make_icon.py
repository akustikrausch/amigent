"""Write Workbench icons: a classic two-plane image, and a colour (GlowIcon) one
for OS 3.5 and later, from the Amigent icon.

Usage: make_icon.py repair OUT.info      project icon that runs a script through IconX
       make_icon.py amigentd OUT.info    tool icon for amigentd with its tool types
Layout from the icon.library DiskObject structure and the GlowIcon IFF chunks.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

from PIL import Image

WIDTH, HEIGHT, DEPTH = 40, 32, 2
GLOW = 48
WBTOOL, WBPROJECT = 3, 4
NO_ICON_POSITION = 0x80000000
SOURCE = Path(__file__).resolve().parents[1] / "src" / "amigent" / "web" / "icon.png"
WORKBENCH = [(170, 170, 170), (0, 0, 0), (255, 255, 255), (102, 136, 187)]  # the four standard pens


def repair_pixel(x: int, y: int) -> int:
    """Blue box (3) with a white border (2) and a white cross."""
    if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1):
        return 1
    in_bar = 16 <= x < 24 and 6 <= y < 26
    in_beam = 8 <= x < 32 and 12 <= y < 20
    return 2 if in_bar or in_beam else 3


def amigent_pixels() -> list[list[int]]:
    """The Amigent icon reduced to the four Workbench pens."""
    picture = Image.open(SOURCE).convert("RGBA").resize((WIDTH, HEIGHT), Image.LANCZOS)
    rows = []
    for y in range(HEIGHT):
        row = []
        for x in range(WIDTH):
            r, g, b, a = picture.getpixel((x, y))
            if a < 128:
                row.append(0)
                continue
            row.append(min(range(1, 4), key=lambda i: sum((c - p) ** 2 for c, p in zip((r, g, b), WORKBENCH[i]))))
        rows.append(row)
    return rows


def planes(pixel) -> bytes:
    data = bytearray()
    row_bytes = (WIDTH + 15) // 16 * 2
    for plane in range(DEPTH):
        for y in range(HEIGHT):
            row = bytearray(row_bytes)
            for x in range(WIDTH):
                if pixel(x, y) >> plane & 1:
                    row[x // 8] |= 0x80 >> (x % 8)
            data += row
    return bytes(data)


def chunk(name: bytes, body: bytes) -> bytes:
    return name + struct.pack(">I", len(body)) + body + (b"\0" if len(body) % 2 else b"")


def glow_icon() -> bytes:
    """FORM ICON with one uncompressed palette image; colour 0 is transparent."""
    picture = Image.open(SOURCE).convert("RGBA").resize((GLOW, GLOW), Image.LANCZOS)
    solid = Image.new("RGB", picture.size, (0, 0, 0))
    solid.paste(picture, mask=picture.split()[3])
    indexed = solid.quantize(colors=255)
    palette = indexed.getpalette()[:255 * 3]
    alpha = picture.split()[3].load()
    pixels = bytearray()
    source = indexed.load()
    for y in range(GLOW):
        for x in range(GLOW):
            pixels.append(0 if alpha[x, y] < 128 else source[x, y] + 1)
    colours = bytes([0, 0, 0]) + bytes(palette)
    face = struct.pack(">BBBBH", GLOW - 1, GLOW - 1, 0, 0x11, len(colours) - 1)
    imag = struct.pack(">BBBBBBHH", 0, len(colours) // 3 - 1, 0b11, 0, 0, 8,
                       len(pixels) - 1, len(colours) - 1) + bytes(pixels) + colours
    body = b"ICON" + chunk(b"FACE", face) + chunk(b"IMAG", imag)
    return b"FORM" + struct.pack(">I", len(body)) + body


def disk_object(kind: int, image: bytes, default_tool: bytes | None, tool_types: list[str],
                stack: int) -> bytes:
    gadget = struct.pack(">IhhHHHHHIIIIIHI", 0, 0, 0, WIDTH, HEIGHT, 5, 3, 1, 1, 0, 0, 0, 0, 0, 1)
    disk = struct.pack(">HH", 0xE310, 1) + gadget
    disk += struct.pack(">BxIIIIIII", kind, int(default_tool is not None), int(bool(tool_types)),
                        NO_ICON_POSITION, NO_ICON_POSITION, 0, 0, stack)
    header = struct.pack(">hhHHHIBBI", 0, 0, WIDTH, HEIGHT, DEPTH, 1, 3, 0, 0)
    out = disk + header + image
    if default_tool is not None:
        out += struct.pack(">I", len(default_tool)) + default_tool
    if tool_types:
        out += struct.pack(">I", (len(tool_types) + 1) * 4)
        for entry in tool_types:
            raw = entry.encode("iso-8859-1") + b"\0"
            out += struct.pack(">I", len(raw)) + raw
    return out


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("repair", "amigentd"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "repair":
        data = disk_object(WBPROJECT, planes(repair_pixel), b"C:IconX\0", [], 4096)
    else:
        rows = amigent_pixels()
        data = disk_object(WBTOOL, planes(lambda x, y: rows[y][x]), None,
                           ["SETUP", "(KEY=S:amigent.key)", "(PORT=6801)", "(QUIET)", "DONOTWAIT"], 16384)
        data += glow_icon()
    Path(sys.argv[2]).write_bytes(data)


if __name__ == "__main__":
    main()
