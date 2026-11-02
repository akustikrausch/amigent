"""One-shot installer, run from the PC once amigentd is up on the Amiga.

Usage: amigent-setup [HOST] [--home DIR] [--with-amigactld [--allow IP | --no-allow]]

Start `amigentd SETUP` on the Amiga first. This finds the machine, puts
amigentd and the scripts in place, adds it to S:User-Startup and gives it a
key. amigentd handles commands and files itself; older versions of it, or
--with-amigactld, also install amigactld from its project page and let it
accept only this PC.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

from . import lzh
from .amigactl import AmigaCtl, AmigaError
from .amigentd import Amigentd, AmigentdError
from .channel import CHANNEL_PROTOCOL, AmigaChannel
from .secret import load_key, new_key, save_key

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


def own_address(host: str) -> str:
    """The address this PC uses towards the Amiga."""
    with socket.create_connection((host, 6801), 5) as s:
        return s.getsockname()[0]


def same_network(a: str, b: str) -> bool:
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def allow_script(address: str, home: str) -> str:
    """Add ALLOW for address to S:amigactld.conf, keeping whatever is in there."""
    return f"""FailAt 21
If EXISTS S:amigactld.conf
  Copy S:amigactld.conf S:amigactld.conf.amigent-bak QUIET
  ; Search can't match whole lines (1.2.3.5 would match 1.2.3.50), and a second
  ; identical ALLOW line does no harm.
  Echo >>S:amigactld.conf "ALLOW {address}"
Else
  Echo >S:amigactld.conf "ALLOW {address}"
EndIf
Copy S:amigactld.conf {home}/amigactld.conf QUIET
"""


UNDO_ALLOW = """FailAt 21
If EXISTS S:amigactld.conf.amigent-bak
  Copy S:amigactld.conf.amigent-bak S:amigactld.conf QUIET
Else
  Delete >NIL: S:amigactld.conf QUIET
EndIf
Status >NIL: COMMAND=C:amigactld
If NOT WARN
  Break `Status COMMAND=C:amigactld` C
  Wait 2
EndIf
Run >NIL: C:amigactld
"""


def reach_amigactld(host: str, wait: float = 40) -> AmigaCtl | None:
    """A working amigactld connection, or None if it stays silent or turns us away."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            ctl = AmigaCtl(host)
            ctl.connect()
            return ctl
        except AmigaError:
            time.sleep(3)
    return None


def run_script(link: Amigentd, text: str) -> None:
    link.put("T:amigent-step", text.encode("iso-8859-1"))
    run(link, "Execute T:amigent-step")


def run(link: Amigentd, command: str) -> None:
    code = link.run(command)
    if code > 5:
        raise SystemExit(f"Amiga said {code} to: {command}")


def install(link: Amigentd, home: str, key: str, allow: str | None, with_actld: bool) -> None:
    if with_actld:
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
    print("setting the key")
    link.put("S:amigent.key", (key + "\n").encode())
    link.put(f"{home}/amigent.key", (key + "\n").encode())
    if with_actld and allow:
        print(f"amigactld will accept only {allow}")
        run_script(link, allow_script(allow, home))
    script = f"""FailAt 21
Protect {home}/amigent-repair +s
Copy C:amigentd.new C:amigentd QUIET
Protect C:amigentd rwed
Delete >NIL: C:amigentd.new QUIET
Copy C:amigentd {home}/ QUIET
MakeDir >NIL: {home}/repair.d
MakeDir >NIL: {home}/apps
Search >NIL: S:User-Startup "C:amigentd" QUIET
If WARN
  Echo >>S:User-Startup "RUN >NIL: C:amigentd"
EndIf
"""
    if with_actld:
        script += f"""Protect C:amigactld rwed
Copy C:amigactld {home}/ QUIET
Search >NIL: S:User-Startup "C:amigactld" QUIET
If WARN
  Echo >>S:User-Startup "RUN >NIL: C:amigactld"
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
    parser.add_argument("--home", default="SYS:amigent", help="folder for the Amigent scripts")
    parser.add_argument("--with-amigactld", action="store_true",
                        help="also install amigactld (automatic for amigentd before protocol 3)")
    parser.add_argument("--allow", help="the PC address amigactld should accept (default: this PC)")
    parser.add_argument("--no-allow", action="store_true", help="let amigactld accept any address")
    args = parser.parse_args()

    host = args.host or find_amiga()
    if not host:
        print("No Amiga in sight. Is `amigentd SETUP` running over there?")
        return 1
    print(f"Amiga at {host}")
    key = load_key(host) or new_key()
    allow = None if args.no_allow else args.allow
    if not allow and not args.no_allow:
        mine = own_address(host)
        if same_network(mine, host):
            allow = mine
        else:
            print(f"This PC talks to the Amiga as {mine}, which looks translated (NAT); amigactld "
                  "stays open to the whole network. Use --allow with the PC's LAN address to change that.")
    link = Amigentd(host, timeout=60, key=key)
    try:
        link.connect()
        with_actld = args.with_amigactld or link.protocol < CHANNEL_PROTOCOL
        install(link, args.home, key, allow, with_actld)
        if not with_actld:
            channel = AmigaChannel(host, key=key)
            channel.connect()
            channel.sysinfo()
            link.end_setup()
            path = save_key(key)
            print("Done. amigentd handles screen, commands and files, and starts with the machine.")
            print(f"The key is in {path} and in S:amigent.key; amigentd asks for it from now on.")
            print(f"AMIGENT_HOST={host}")
            return 0
        ctl = reach_amigactld(host)
        if ctl is None and allow:
            # The Amiga sees us under another address: undo the restriction while we still can.
            print("amigactld turns this PC away; removing the ALLOW line again")
            run_script(link, UNDO_ALLOW)
            ctl = reach_amigactld(host)
        if ctl is None:
            print("amigactld does not answer yet. A reboot of the Amiga should sort that out.")
            return 1
        link.end_setup()
    except (AmigentdError, AmigaError) as e:
        print(f"{e}\nStart `amigentd SETUP` on the Amiga and try again.")
        return 1
    path = save_key(key)
    print(f"Done. {ctl.banner} and amigentd are up, and both start with the machine.")
    print(f"The key is in {path} and in S:amigent.key; amigentd asks for it from now on.")
    print(f"AMIGENT_HOST={host}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
