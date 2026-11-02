# performance

Amigent is built for slow Amigas and slow wires. These are the numbers behind
that claim, how they were taken, and how to take them again.

## The Amiga side

Measured with `amigentd` on WinUAE with real CPU timing (`cpu_speed=real`),
Workbench 3.1 at 640×256 with 4 colours, before and after the planar frames:

| | 68000, 7 MHz | | 68020, 14 MHz | |
|---|---|---|---|---|
| | before | now | before | now |
| whole screen | 15.4 s | 0.64 s | 1.59 s | 0.11 s |
| unchanged screen | 13.1 s | 80 ms | 1.0–1.3 s | 10–19 ms |
| mouse move behind a whole frame | 15.9 s | 120 ms | 1.97 s | during the frame |
| CPU while the PC watches | machine full | under 10 % | machine full | 2.4 % |

What makes the difference:

- **Bitplanes as they are.** Chipset screens go out in the planes the Amiga
  keeps anyway, so a 68000 converts nothing. A helper at the lowest priority
  notices changes by comparing the planes in chip RAM, 40 KB in about 76 ms.
- **Packing that costs the Amiga almost nothing.** Each 64×64 tile is
  PackBits, optionally the difference to the previous tile, to the row above,
  or to the same picture scrolled, whichever is smallest.
- **Graphics-card screens** go out as tiles with their own palette of 2 to 16
  colours where that fits, else as 8-bit colour: a 1920×1080 Workbench drops
  from 128 KB to 88 KB, and over a plipbox from 9.6–13.1 s to 3.0–4.7 s.
- **Input between tiles.** Mouse moves, clicks and keys arriving while a frame
  is on its way are carried out at once, not after it.
- **No tiny packets.** The plipbox pays per packet, so frames are sent in large
  pieces.

Every screen depth was checked pixel for pixel against the full-colour path:
16 and 256 colours, HAM6, HAM8 and Extra Halfbrite.

## Over the wire

Measured on a real A1200 (PiStorm, 1920×1080 RTG Workbench) over its plipbox:

| update | whole screen as raw pixels | Amigent |
|---|---|---|
| first picture | 6.2 MB, about 124 s | 69 KB, 1.9 s |
| one line typed in a Shell | 6.2 MB, about 124 s | 3.2 KB, 0.17 s |
| nothing changed | 6.2 MB, about 124 s | 12 bytes, 7 ms |

The raw figure is plain arithmetic: 1920 × 1080 × 3 bytes at 50 KB/s.

## The remote desktop

Measured with `scripts/bench_viewer.py`, a fake `amigentd` behind a simulated
link (rate, latency, small buffers like a plipbox), old PC side against new:

| link | first tile | whole picture | key on a busy screen | click |
|---|---|---|---|---|
| 50 KB/s, 20 ms | 2.56 → 0.26 s | 2.56 → 0.80 s | 1101 → 57 ms | 1797 → 66 ms |
| 15 KB/s, 100 ms | 8.13 → 1.05 s | 8.13 → 2.44 s | 3009 → 543 ms | 5756 → 267 ms |
| 68000, 15 KB/s, 100 ms | 6.73 → 1.12 s | 6.73 → 2.15 s | 3242 → 334 ms | 6607 → 298 ms |

While idle the viewer asks about once a second and costs the Amiga 4–8 % of
its time instead of up to 76 %. Mouse moves never queue up, and held keys drop
repeats instead of piling them up behind a slow link.

## The AI agent

Measured with `scripts/bench_mcp.py` on the same simulated links:

| task | 50 KB/s, 20 ms | 15 KB/s, 100 ms |
|---|---|---|
| copy a folder of 200 files (500 KB) | 19.1 → 10.4 s | 76.7 → 34.7 s |
| the same folder again | 19.6 → 2.9 s | 79.0 → 3.9 s |
| recursive listing | 0.73 → 0.31 s | 3.0 → 1.13 s |
| `wait_change` with a 3 s limit | 5.0 → 3.0 s | 16.4 → 3.0 s |

A 1920×1080 screenshot for the model shrank from 557 KB to 58 KB at the same
detail, and asking again for an unchanged screen returns a short note instead
of a picture. On the real A1200, 100 files (236 KB) go up in 5.3 s, and again
in 0.7 s because unchanged files stay where they are.

## Measure it yourself

```bash
python scripts/bench_viewer.py
```

```bash
python scripts/bench_mcp.py
```

Both run offline against the simulated link and print a table; `--save` and
`--compare` keep a run for a before/after. Against a real Amiga,
`amigent-doctor` reports the link speed and the time for a whole and an
unchanged screen.
