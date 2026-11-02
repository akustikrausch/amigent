"""Pictures for the model: no bigger than it can use, packed as tight as PNG allows.

The model pays per pixel, and anything past its ceiling gets resampled on the
other end by a filter nobody chose. So we squash first, with a filter we did
choose, and hand over an indexed PNG: an Amiga screen rarely has more colours
than a C64 has excuses.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageDraw

MAX_EDGE = 1568  # longest side the model API takes without resizing
MAX_PIXELS = 1_150_000  # and its area ceiling: 1920x1080 lands at 1430x804 either way
SHEET_LIMIT = 40  # gadgets per picture sheet; past that it's a phone book


def fit(image: Image.Image) -> tuple[Image.Image, float]:
    """Shrink to what the model takes in; returns the picture and screen pixels per image pixel."""
    w, h = image.size
    scale = max(1.0, max(w, h) / MAX_EDGE, (w * h / MAX_PIXELS) ** 0.5)
    if scale == 1.0:
        return image, 1.0
    size = (max(1, round(w / scale)), max(1, round(h / scale)))
    # BOX = area average: text stays text, and the PNG a third of LANCZOS's
    return image.resize(size, Image.Resampling.BOX), scale


def _bits(colours: int) -> int:
    return 1 if colours <= 2 else 2 if colours <= 4 else 4 if colours <= 16 else 8


def png(image: Image.Image, exact: bool = False) -> bytes:
    """PNG with a palette whenever that's lossless, or (exact=False) close enough.

    A screen read as RGB332 has at most 256 colours, so the palette costs
    nothing. After a downscale the edges blend into thousands of shades; those go
    back to 256, which text survives fine and the PNG shrinks by half. exact
    pictures (amiga_screenshot_color) keep every colour when there are more.
    """
    image = image.convert("RGB")
    colours = image.getcolors(256)
    if colours is not None:
        # median cut asked for exactly as many colours as there are hands each its own box;
        # quantize(palette=...) does not, it rounds dark greys to whatever is near
        indexed = _indexed(image, len(colours))
        if not ImageChops.difference(indexed.convert("RGB"), image).getbbox():
            return _save(indexed, bits=_bits(len(colours)))
    if exact:
        return _save(image)
    return _save(_indexed(image, 256))


def _indexed(image: Image.Image, colours: int) -> Image.Image:
    return image.quantize(colours, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def _save(image: Image.Image, **options) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True, **options)
    return out.getvalue()


def unlabelled(tree: list) -> list[dict]:
    """The active window's gadgets that come without a readable label."""
    active = next((w["id"] for w in tree if w["kind"] == "window" and w.get("active")), None)
    return [g for g in tree if g["kind"] == "gadget" and g["window"] == active
            and not g["label"] and g["type"] != "sys" and 4 <= g["w"] <= 600 and 4 <= g["h"] <= 120]


def gadget_sheet(gadgets: list[dict], picture: Image.Image,
                 origin: tuple[int, int] = (0, 0)) -> Image.Image:
    """Each gadget cut out of the picture next to its #number, stacked like a sprite sheet.

    origin is where the picture sits on the screen, for a picture of bounds() only."""
    rows, width = [], 0
    ox, oy = origin
    for g in gadgets[:SHEET_LIMIT]:
        crop = picture.crop((g["x"] - ox, g["y"] - oy, g["x"] - ox + g["w"], g["y"] - oy + g["h"]))
        scale = min(2.0, 48 / max(g["h"], 1))
        if scale > 1:
            crop = crop.resize((round(g["w"] * scale), round(g["h"] * scale)), Image.NEAREST)
        rows.append((g["id"], crop))
        width = max(width, crop.width)
    sheet = Image.new("RGB", (width + 70, sum(c.height + 8 for _, c in rows) + 8), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    y = 8
    for number, crop in rows:
        draw.text((8, y + crop.height // 2 - 6), f"#{number}", fill=(0, 0, 0))
        sheet.paste(crop, (62, y))
        y += crop.height + 8
    return sheet


def bounds(gadgets: list[dict]) -> tuple[int, int, int, int]:
    """The smallest box around the gadgets: all the screen the sheet needs to fetch."""
    gadgets = gadgets[:SHEET_LIMIT]
    left = min(g["x"] for g in gadgets)
    top = min(g["y"] for g in gadgets)
    right = max(g["x"] + g["w"] for g in gadgets)
    bottom = max(g["y"] + g["h"] for g in gadgets)
    return left, top, right - left, bottom - top
