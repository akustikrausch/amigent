"""Draw docs/screenshots/speed.svg: seconds per screen update over a plipbox.

Usage: make_chart.py

The Amigent figures were measured on a real A1200 (PiStorm, 1920x1080 RTG
Workbench) over its plipbox; the raw grab is the plain arithmetic of moving the
whole screen as 24-bit pixels, which is what a screen grab without packing
costs on the wire. Change the numbers below after a new measurement.
"""

from __future__ import annotations

import math
from pathlib import Path

LINK = 50_000  # bytes per second, a plipbox on a good day
RAW = 1920 * 1080 * 3  # one whole 24-bit screen
CASES = [  # (label, amigent bytes, amigent seconds measured)
    ("First picture", 69_427, 1.94),
    ("One line typed in a Shell", 3_159, 0.17),
    ("Nothing changed", 12, 0.007),
]
OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots" / "speed.svg"

W, LEFT, RIGHT, TOP = 760, 210, 70, 92
BAR, GAP, GROUP = 18, 4, 64
LO, HI = 0.005, 300.0  # seconds, the log axis


def x(seconds: float) -> float:
    span = math.log10(HI) - math.log10(LO)
    return LEFT + (math.log10(max(seconds, LO)) - math.log10(LO)) / span * (W - LEFT - RIGHT)


def fmt(seconds: float) -> str:
    if seconds >= 10:
        return f"{seconds:.0f} s"
    if seconds >= 1:
        return f"{seconds:.1f} s"
    return f"{seconds * 1000:.0f} ms"


def bar(y: float, seconds: float, cls: str, label: str) -> str:
    end = x(seconds)
    return (f'<path class="{cls}" d="M{LEFT} {y}h{end - LEFT - 4:.1f}a4 4 0 0 1 4 4v{BAR - 8}'
            f'a4 4 0 0 1 -4 4h-{end - LEFT - 4:.1f}z"><title>{label}: {fmt(seconds)}</title></path>'
            f'<text class="value" x="{end + 6:.1f}" y="{y + BAR - 5}">{fmt(seconds)}</text>')


def main() -> None:
    height = TOP + len(CASES) * GROUP + 40
    raw = RAW / LINK
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" '
             f'height="{height}" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif" '
             f'role="img" aria-labelledby="t d">',
             '<title id="t">Seconds per screen update over a plipbox</title>',
             f'<desc id="d">Whole screen as raw pixels: {fmt(raw)} per update. Amigent: '
             + ", ".join(f"{c[0].lower()} {fmt(c[2])}" for c in CASES) + ".</desc>",
             """<style>
  .bg{fill:#fcfcfb}.title{fill:#0b0b0b;font-size:17px;font-weight:600}
  .sub,.tick{fill:#52514e;font-size:12px}.case{fill:#0b0b0b;font-size:13px}
  .value{fill:#0b0b0b;font-size:12px;font-variant-numeric:tabular-nums}
  .grid{stroke:#e1e0d9;stroke-width:1}.axis{stroke:#c3c2b7;stroke-width:1}
  .raw{fill:#eb6834}.ours{fill:#2a78d6}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1a1a19}.title,.case,.value{fill:#f0efec}.sub,.tick{fill:#c3c2b7}
    .grid{stroke:#2c2c2a}.axis{stroke:#383835}.raw{fill:#d95926}.ours{fill:#3987e5}}
</style>""",
             f'<rect class="bg" width="{W}" height="{height}" rx="10"/>',
             '<text class="title" x="20" y="32">Seconds per screen update over a plipbox (50 KB/s)</text>',
             '<text class="sub" x="20" y="52">Lower is better. 1920×1080 Workbench on a real A1200, '
             'logarithmic scale.</text>',
             f'<rect class="raw" x="{LEFT}" y="64" width="12" height="12" rx="3"/>'
             f'<text class="sub" x="{LEFT + 18}" y="75">Whole screen as raw pixels</text>',
             f'<rect class="ours" x="{LEFT + 210}" y="64" width="12" height="12" rx="3"/>'
             f'<text class="sub" x="{LEFT + 228}" y="75">Amigent</text>']
    bottom = TOP + len(CASES) * GROUP - 8
    for tick in (0.01, 0.1, 1, 10, 100):
        tx = x(tick)
        parts.append(f'<line class="grid" x1="{tx:.1f}" y1="{TOP - 4}" x2="{tx:.1f}" y2="{bottom}"/>')
        label = f"{tick:g} s" if tick >= 1 else fmt(tick)
        parts.append(f'<text class="tick" x="{tx:.1f}" y="{bottom + 16}" text-anchor="middle">'
                     f'{label}</text>')
    parts.append(f'<line class="axis" x1="{LEFT}" y1="{TOP - 4}" x2="{LEFT}" y2="{bottom}"/>')
    for i, (label, _, seconds) in enumerate(CASES):
        y = TOP + i * GROUP
        parts.append(f'<text class="case" x="{LEFT - 12}" y="{y + BAR + 4}" text-anchor="end">{label}</text>')
        parts.append(bar(y, raw, "raw", "Whole screen as raw pixels"))
        parts.append(bar(y + BAR + GAP, seconds, "ours", "Amigent"))
    parts.append("</svg>")
    OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
