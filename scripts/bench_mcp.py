"""Time the MCP tools over a fake slow wire: plipbox speed, serial speed, real latency.

    python3 scripts/bench_mcp.py                       # run, print the table
    python3 scripts/bench_mcp.py --save before.json    # keep the numbers
    python3 scripts/bench_mcp.py --compare before.json # before/after table
    python3 scripts/bench_mcp.py --src /other/src      # bench another checkout

Everything runs against tests/fakeamiga.py on 127.0.0.1; no Amiga gets hurt.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINKS = {"50K/20ms": (50_000, 0.020), "15K/100ms": (15_000, 0.100)}


def tokens(w: int, h: int) -> int:
    """What the model pays for a picture, roughly: pixels / 750 after its own downscale."""
    scale = min(1.0, 1568 / max(w, h), (1_150_000 / (w * h)) ** 0.5)
    return round(w * scale * h * scale / 750)


def image_of(result) -> tuple[int, int, int]:
    """(bytes, width, height) of the picture in a tool result, zeros when there is none."""
    from PIL import Image
    import io

    for part in result if isinstance(result, list) else []:
        data = getattr(part, "data", None)
        if data:
            if isinstance(data, str):
                data = base64.b64decode(data)
            w, h = Image.open(io.BytesIO(data)).size
            return len(data), w, h
    return 0, 0, 0


class Bench:
    def __init__(self, server, fake, link):
        self.server, self.fake, self.link = server, fake, link
        self.rows: dict[str, dict] = {}

    def fresh(self):
        """New connections and an empty screen memory, as after starting the MCP server."""
        from amigent.amigentd import Amigentd
        from amigent.channel import Commands

        with contextlib.suppress(Exception):
            self.server.ctl.close()
            self.server.screen.close()
        self.server.ctl = Commands("127.0.0.1", 1, self.link.port, timeout=30)
        self.server.screen = Amigentd("127.0.0.1", self.link.port, timeout=30)
        self.server.last_look = 0.0
        for name in ("_last_shot",):
            if hasattr(self.server, name):
                setattr(self.server, name, None)
        # warm up: connections are not what we measure
        self.server.ctl.connect()
        self.server.screen.connect()

    def run(self, name, fn, note=""):
        self.link.reset()
        start = time.monotonic()
        try:
            result = fn()
            error = ""
        except Exception as e:  # a tool that fails is a number too
            result, error = None, f"{type(e).__name__}: {e}"[:70]
        took = time.monotonic() - start
        size, w, h = image_of(result)
        row = {"s": round(took, 2), "up": self.link.bytes["up"], "down": self.link.bytes["down"],
               "img": size, "wh": f"{w}x{h}" if w else "", "tok": tokens(w, h) if w else 0,
               "err": error, "note": note or (str(result)[:60] if isinstance(result, str) else "")}
        self.rows[name] = row
        print(f"  {name:24} {took:7.2f}s  up {row['up']:>8}  down {row['down']:>8}  "
              f"img {size:>7} {row['wh']:>9} tok {row['tok']:>5} {error or row['note']}", flush=True)


def scenarios(bench: Bench, work: Path):
    s, fake = bench.server, bench.fake
    rnd = random.Random(7)

    big = work / "big.bin"
    big.write_bytes(bytes(rnd.getrandbits(8) for _ in range(500_000)))
    tree = work / "tree"
    for i in range(200):
        f = tree / f"dir{i % 10}" / f"file{i:03}.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(bytes(rnd.getrandbits(8) for _ in range(rnd.randrange(1024, 4096))))
    for i in range(800):
        fake.files[f"Work:Big/entry{i:04}.info"] = b"x" * (i * 13 % 9000)
    fake.dirs.add("Work:Big")

    bench.fresh()
    bench.run("exec", lambda: s.amiga_exec("Version"))
    bench.run("exec x5", lambda: [s.amiga_exec("Version") for _ in range(5)][-1])
    bench.run("put 500K", lambda: s.amiga_put(str(big), "RAM:big.bin"))
    bench.run("put 500K again", lambda: s.amiga_put(str(big), "RAM:big.bin"))
    bench.run("get 500K", lambda: s.amiga_get("RAM:big.bin", str(work / "back.bin")))
    bench.run("put tree 200", lambda: s.amiga_put(str(tree), "RAM:tree"))
    bench.run("put tree again", lambda: s.amiga_put(str(tree), "RAM:tree"))
    bench.run("get tree 200", lambda: s.amiga_get("RAM:tree", str(work / "tree-back")))
    bench.run("list 800", lambda: s.amiga_list("Work:Big"), note="")
    bench.run("list tree recursive", lambda: s.amiga_list("RAM:tree", True))

    for w, h in ((640, 256), (1920, 1080)):
        from fakeamiga import desktop

        fake.picture = desktop(w, h)
        fake.sent.clear()
        bench.fresh()
        bench.run(f"shot {w}x{h} first", lambda: s.amiga_screenshot())
        bench.run(f"shot {w}x{h} same", lambda: s.amiga_screenshot())
        fake.picture.paste((255, 255, 255), (100, 100, 180, 140))
        bench.run(f"shot {w}x{h} small chg", lambda: s.amiga_screenshot())
        bench.run(f"shot {w}x{h} region", lambda: s.amiga_screenshot(0, 0, 320, 128))
        bench.run(f"color {w}x{h} 300x100", lambda: s.amiga_screenshot_color(10, 10, 300, 100))

    # wait_change on a busy screen, no picture yet: the poll that used to eat the deadline
    fake.picture = desktop(1920, 1080, seed=3)
    fake.sent.clear()
    bench.fresh()
    stop = threading.Event()

    def demo():  # something animating far away from the region we watch
        n = 0
        while not stop.is_set():
            n += 1
            fake.picture.paste((n * 40 % 256, n * 90 % 256, 0), (800, 300, 1900, 1000))
            time.sleep(0.05)

    threading.Thread(target=demo, daemon=True).start()
    try:
        bench.run("wait_change 3s cold", lambda: s.amiga_wait_change(0, 0, 200, 100, timeout=3))
        bench.run("wait_change 3s warm", lambda: s.amiga_wait_change(0, 0, 200, 100, timeout=3))
        fake.picture.paste((0, 0, 0), (10, 10, 50, 30))
        bench.run("wait_change hit", lambda: s.amiga_wait_change(0, 0, 200, 100, timeout=3))
    finally:
        stop.set()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default=str(ROOT / "src"))
    parser.add_argument("--save")
    parser.add_argument("--compare")
    parser.add_argument("--links", default=",".join(LINKS))
    parser.add_argument("--op-delay", type=float, default=0.004,
                        help="Amiga time per file operation in seconds")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="amigent-bench-"))
    os.environ["XDG_CONFIG_HOME"] = str(scratch / "config")
    os.environ["AMIGENT_HOST"] = "127.0.0.1"
    sys.path[:0] = [args.src, str(ROOT / "tests")]
    from fakeamiga import FakeAmiga, SlowLink
    import amigent.server as server
    from amigent import machines

    machines.scan = lambda: []
    server._exclusive = lambda name=None: contextlib.nullcontext()
    results = {}
    for name in args.links.split(","):
        rate, latency = LINKS[name]
        print(f"{name}: {rate // 1000} KB/s, {latency * 1000:.0f} ms each way, half duplex")
        fake = FakeAmiga(op_delay=args.op_delay, crc_rate=300_000)
        link = SlowLink(fake.port, rate, latency)
        bench = Bench(server, fake, link)
        work = scratch / name.replace("/", "_")
        work.mkdir()
        scenarios(bench, work)
        results[name] = bench.rows
        link.close()
        fake.close()
    if args.save:
        Path(args.save).write_text(json.dumps(results, indent=1))
    if args.compare:
        table(json.loads(Path(args.compare).read_text()), results)


def table(before: dict, after: dict) -> None:
    print("\n| link | scenario | before s | after s | before wire KB | after wire KB "
          "| before img KB / tok | after img KB / tok |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for link, rows in after.items():
        for name, a in rows.items():
            b = before.get(link, {}).get(name)
            if not b:
                continue

            def wire(r):
                return f"{(r['up'] + r['down']) / 1024:.1f}"

            def img(r):
                return f"{r['img'] / 1024:.0f} / {r['tok']}" if r["img"] else "-"

            bs = "fail" if b["err"] else f"{b['s']:.2f}"
            as_ = "fail" if a["err"] else f"{a['s']:.2f}"
            print(f"| {link} | {name} | {bs} | {as_} | {wire(b)} | {wire(a)} | {img(b)} | {img(a)} |")


if __name__ == "__main__":
    main()
