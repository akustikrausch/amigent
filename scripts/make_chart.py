"""Draw docs/screenshots/speed.svg: seconds per screen update over a plipbox.

Usage: make_chart.py

The Amigent figures were measured on a real A1200 (PiStorm, 1920x1080 RTG
Workbench) over its plipbox; the raw grab is the plain arithmetic of moving the
whole screen as 24-bit pixels, which is what a screen grab without packing
costs on the wire. Change the numbers below after a new measurement.
"""

from __future__ import annotations

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
HI = 130.0  # seconds, the end of the linear axis
STUB = 3  # px: a bar too short to see still shows where it is


def x(seconds: float) -> float:
    """Linear on purpose: the bar lengths are the real ratio, nothing flattered."""
    return LEFT + min(seconds, HI) / HI * (W - LEFT - RIGHT)


def fmt(seconds: float) -> str:
    if seconds >= 10:
        return f"{seconds:.0f} s"
    if seconds >= 1:
        return f"{seconds:.1f} s"
    return f"{seconds * 1000:.0f} ms"


def bar(y: float, seconds: float, cls: str, label: str, note: str = "") -> str:
    width = max(x(seconds) - LEFT, STUB)
    radius = min(4, width / 2)
    text = fmt(seconds) + (f" · {note}" if note else "")
    return (f'<path class="{cls}" d="M{LEFT} {y}h{width - radius:.1f}a{radius:.1f} {radius:.1f} 0 0 1 '
            f'{radius:.1f} {radius:.1f}v{BAR - 2 * radius:.1f}a{radius:.1f} {radius:.1f} 0 0 1 '
            f'-{radius:.1f} {radius:.1f}h-{width - radius:.1f}z"><title>{label}: {fmt(seconds)}</title></path>'
            f'<text class="value" x="{LEFT + width + 6:.1f}" y="{y + BAR - 5}">{text}</text>')


def faster(raw: float, ours: float) -> str:
    """Two significant digits: a measurement, not a lottery number."""
    factor = float(f"{raw / ours:.2g}")
    return f"{factor:,.0f}× faster" if factor >= 10 else f"{factor:.1f}× faster"


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
             '<text class="sub" x="20" y="52">Shorter is better. 1920×1080 Workbench on a real A1200; '
             'bar lengths to scale.</text>',
             f'<rect class="raw" x="{LEFT}" y="64" width="12" height="12" rx="3"/>'
             f'<text class="sub" x="{LEFT + 18}" y="75">Whole screen as raw pixels</text>',
             f'<rect class="ours" x="{LEFT + 210}" y="64" width="12" height="12" rx="3"/>'
             f'<text class="sub" x="{LEFT + 228}" y="75">Amigent</text>']
    bottom = TOP + len(CASES) * GROUP - 8
    for tick in (0, 30, 60, 90, 120):
        tx = x(tick)
        parts.append(f'<line class="grid" x1="{tx:.1f}" y1="{TOP - 4}" x2="{tx:.1f}" y2="{bottom}"/>')
        label = f"{tick} s"
        parts.append(f'<text class="tick" x="{tx:.1f}" y="{bottom + 16}" text-anchor="middle">'
                     f'{label}</text>')
    parts.append(f'<line class="axis" x1="{LEFT}" y1="{TOP - 4}" x2="{LEFT}" y2="{bottom}"/>')
    for i, (label, _, seconds) in enumerate(CASES):
        y = TOP + i * GROUP
        parts.append(f'<text class="case" x="{LEFT - 12}" y="{y + BAR + 4}" text-anchor="end">{label}</text>')
        parts.append(bar(y, raw, "raw", "Whole screen as raw pixels"))
        parts.append(bar(y + BAR + GAP, seconds, "ours", "Amigent", faster(raw, seconds)))
    parts.append("</svg>")
    OUT.write_text("\n".join(parts) + "\n", encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
