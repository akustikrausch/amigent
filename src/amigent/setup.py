"""One-shot installer, run from the PC once amigentd is up on the Amiga.

Usage: amigent-setup [HOST] [--home DIR]

Start `amigentd SETUP` on the Amiga first. This finds the machine, fetches
amigactld from its project page, puts everything in place and adds the two
services to S:User-Startup.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import sys
import urllib.request
from pathlib import Path

from . import lzh
from .amigactl import AmigaCtl
from .amigentd import Amigentd, AmigentdError
from .power import wait_for_port

REPO = Path(__file__).resolve().parents[2]
AMIGA = REPO / "amiga"
UPSTREAM = "https://api.github.com/repos/tbdye/amigactl/releases/latest"
SCRIPTS = ("amigent-repair", "amigent-scan", "menu-add.rexx")


def find_amiga() -> str | None:
    """Look for amigentd on the local /24."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("192.0.2.1", 9))
    mine = probe.getsockname()[0]
    probe.close()
    net = mine.rsplit(".", 1)[0]

    def check(host: str) -> str | None:
        try:
            with socket.create_connection((host, 6801), 1) as s:
                s.settimeout(2)
                return host if s.recv(4) == b"AMGT" else None
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        hits = [h for h in pool.map(check, (f"{net}.{i}" for i in range(1, 255))) if h]
    return hits[0] if hits else None


def fetch_amigactld() -> bytes:
    request = urllib.request.Request(UPSTREAM, headers={"User-Agent": "amigent-setup"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    url = next(a["browser_download_url"] for a in release["assets"] if a["name"].endswith(".lha"))
    print(f"fetching amigactld {release['tag_name']}")
    with urllib.request.urlopen(url, timeout=120) as response:
        return lzh.extract(response.read(), "/amigactld")


def run(link: Amigentd, command: str) -> None:
    code = link.run(command)
    if code > 5:
        raise SystemExit(f"Amiga said {code} to: {command}")


def install(link: Amigentd, home: str) -> None:
    binary = fetch_amigactld()
    print("uploading amigactld")
    link.put("C:amigactld", binary)
    print("uploading amigentd")
    link.put("C:amigentd.new", (AMIGA / "bin" / "amigentd").read_bytes())
    run(link, f"MakeDir >NIL: {home}")
    print("uploading scripts")
    for name in SCRIPTS:
        text = (AMIGA / name).read_text().replace("Stuff:amigent", home)
        link.put(f"{home}/{name}", text.encode("iso-8859-1"))
    link.put(f"{home}/amigent-repair.info", (AMIGA / "amigent-repair.info").read_bytes())
    script = f"""FailAt 21
Protect C:amigactld rwed
Protect {home}/amigent-repair +s
Copy C:amigentd.new C:amigentd QUIET
Protect C:amigentd rwed
Delete >NIL: C:amigentd.new QUIET
Copy C:amigactld {home}/ QUIET
Copy C:amigentd {home}/ QUIET
MakeDir >NIL: {home}/repair.d
MakeDir >NIL: {home}/apps
Search >NIL: S:User-Startup "C:amigactld" QUIET
If WARN
  Echo >>S:User-Startup "RUN >NIL: C:amigactld"
EndIf
Search >NIL: S:User-Startup "C:amigentd" QUIET
If WARN
  Echo >>S:User-Startup "RUN >NIL: C:amigentd"
EndIf
Status >NIL: COMMAND=C:amigactld
If WARN
  Run >NIL: C:amigactld
EndIf
"""
    link.put("T:amigent-setup", script.encode("iso-8859-1"))
    run(link, "Execute T:amigent-setup")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", nargs="?")
    parser.add_argument("--home", default="SYS:amigent", help="folder for the amigent scripts")
    args = parser.parse_args()

    host = args.host or find_amiga()
    if not host:
        print("No Amiga in sight. Is `amigentd SETUP` running over there?")
        return 1
    print(f"Amiga at {host}")
    link = Amigentd(host, timeout=60)
    try:
        link.connect()
        install(link, args.home)
        link.end_setup()
    except AmigentdError as e:
        print(f"{e}\nStart `amigentd SETUP` on the Amiga and try again.")
        return 1
    if not wait_for_port(host, 6800, 30):
        print("amigactld does not answer yet. A reboot of the Amiga should sort that out.")
        return 1
    ctl = AmigaCtl(host)
    ctl.connect()
    print(f"Done. {ctl.banner} and amigentd are up, and both start with the machine.")
    print(f"AMIGENT_HOST={host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
