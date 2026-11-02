"""One-shot installer, run from the PC once amigentd is up on the Amiga.

Usage: amigent-setup [HOST] [--home DIR] [--with-amigactld [--allow IP | --no-allow]]

Start `amigentd SETUP` on the Amiga first. This finds the machine, drops
amigentd and the scripts in place, hooks it into S:User-Startup and hands it a
key. amigentd does commands and files on its own; older versions of it, or
--with-amigactld, also pull amigactld from its project page and let it
accept only this PC.
"""

from __future__ import annotations

import argparse
import sys

from . import machines, tls
from .amigentd import Amigentd, AmigentdError
from .channel import CHANNEL_PROTOCOL, AmigaChannel
from .errors import AmigaError
from .legacy import (UNDO_ALLOW, allow_script, fetch_amigactld,  # noqa: F401 (allow_script: old address)
                     reach_amigactld, same_network, startup_script)
from .net import lan_address
from .paths import AMIGA
from .secret import new_key, own_key, save_key

SCRIPTS = ("amigent-repair", "amigent-scan", "menu-add.rexx")


def find_amiga() -> str | None:
    """Sweep the local /24 for amigentd; first one to say hello wins."""
    hits = machines.scan()
    return hits[0] if hits else None


def run_script(link: Amigentd, text: str) -> None:
    link.put("T:amigent-step", text.encode("iso-8859-1"))
    run(link, "Execute T:amigent-step")


def run(link: Amigentd, command: str) -> None:
    code = link.run(command)
    if code > 5:
        raise SystemExit(f"Amiga said {code} to: {command}")


def amigentd_build(link: Amigentd) -> str:
    """amigentd.000 for a stock 68000/68010, amigentd for 68020 and up."""
    code = link.run("CPU CHECK 68020")
    if code not in (0, 5):
        raise SystemExit(f"C:CPU answered {code}, so the CPU is unknown. Copy C:CPU from the "
                         "Workbench disk and try again.")
    return "amigentd.000" if code == 5 else "amigentd"


def install(link: Amigentd, home: str, key: str, allow: str | None, with_actld: bool) -> None:
    if with_actld:
        binary = fetch_amigactld()
        print("uploading amigactld")
        link.put("C:amigactld", binary)
    build = amigentd_build(link)
    print(f"uploading {build}")
    link.put("C:amigentd.new", (AMIGA / "bin" / build).read_bytes())
    run(link, f"MakeDir >NIL: {home}")
    print("uploading scripts")
    for name in SCRIPTS:
        text = (AMIGA / name).read_text(encoding="iso-8859-1").replace("SYS:amigent", home)
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
        script += startup_script(home)
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
    parser.add_argument("--serve", nargs="?", const=8001, type=int, metavar="PORT",
                        help="first step: offer amigentd to the Amiga over the network (port 8001)")
    parser.add_argument("--name", help="what to call this Amiga (default: Amiga, or the name it has)")
    args = parser.parse_args()
    if args.serve:
        from .serve import serve
        return serve(args.serve)

    host = args.host or find_amiga()
    if not host:
        print("No Amiga in sight. Is `amigentd SETUP` running over there?")
        return 1
    known = machines.find(host)
    name = args.name or (known["name"] if known else machines.unique_name("Amiga", machines.load()))
    host = machines.address(host)
    print(f"Amiga at {host}")
    allow = None if args.no_allow else args.allow
    if not allow and not args.no_allow:
        mine = lan_address(host)
        if same_network(mine, host):
            allow = mine
        else:
            print(f"This PC talks to the Amiga as {mine}, which looks translated (NAT); amigactld "
                  "stays open to the whole network. Use --allow with the PC's LAN address to change that.")
    link = Amigentd(host, timeout=60)
    try:
        link.connect()
        # Asks for a key? Then it was set up before, and the key that just got us in stays.
        key = link.key if link.flags & 1 else own_key(host) or new_key()
        link.key = key
        with_actld = args.with_amigactld or link.protocol < CHANNEL_PROTOCOL
        install(link, args.home, key, allow, with_actld)
        if link.flags & 2:
            tls.provision(link, host, args.home)
        if not with_actld:
            channel = AmigaChannel(host, key=key)
            channel.connect()
            channel.sysinfo()
            link.end_setup()
            path = save_key(key, host)
            print("Done. amigentd handles screen, commands and files, and starts with the machine.")
            print(f"The key is in {path} and in S:amigent.key; amigentd asks for it from now on.")
            return _named(name, host)
        ctl = reach_amigactld(host)
        if ctl is None and allow:
            # The Amiga sees us under another address: pull the ALLOW while the door is still open.
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
    path = save_key(key, host)
    print(f"Done. {ctl.banner} and amigentd are up, and both start with the machine.")
    print(f"The key is in {path} and in S:amigent.key; amigentd asks for it from now on.")
    return _named(name, host)


def _named(name: str, host: str) -> int:
    machines.remember(name, host)
    print(f'This Amiga is "{name}" now. Should its address change, Amigent finds it again.')
    print(f"AMIGENT_HOST={name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
