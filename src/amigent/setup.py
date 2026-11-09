"""One-shot installer, run from the PC once amigentd is up on the Amiga.

Usage: amigent-setup [HOST] [--home DIR] [--with-amigactld [--allow IP | --no-allow]]
       amigent-setup --update [HOST] [--home DIR]

Start `amigentd SETUP` on the Amiga first. This finds the machine, drops
amigentd and the scripts in place, hooks it into S:User-Startup and hands it a
key. amigentd does commands and files on its own; older versions of it, or
--with-amigactld, also pull amigactld from its project page and let it
accept only this PC.

--update swaps the amigentd that already runs for the one in this package, over
the usual key and TLS, with nobody at the Amiga. Should the new one not come up,
the Amiga starts the old one again.
"""

from __future__ import annotations

import argparse
import sys
import time

from . import __version__, machines, tls
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
    for name in WINDOWS:  # the little windows on the Amiga's side
        if (AMIGA / "bin" / name).exists():
            link.put(f"{home}/{name}", (AMIGA / "bin" / name).read_bytes())
    print("setting the key")
    link.put("S:amigent.key", (key + "\n").encode())
    link.put(f"{home}/amigent.key", (key + "\n").encode())
    if with_actld and allow:
        print(f"amigactld will accept only {allow}")
        run_script(link, allow_script(allow, home))
    script = f"""FailAt 21
Protect {home}/amigent-repair +s
Protect >NIL: {home}/amigent-status +rwed
Protect >NIL: {home}/amigent-monitor +rwed
Copy C:amigentd.new C:amigentd QUIET
Protect C:amigentd rwed
Delete >NIL: C:amigentd.new QUIET
Copy C:amigentd {home}/ QUIET
Echo >ENVARC:amigent-home "{home}"
Copy >NIL: ENVARC:amigent-home ENV:
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


UPDATE_TROUBLE = {
    b"old": "The new amigentd did not start, so the Amiga runs the old one again.",
    b"busy": "The running amigentd did not stop, so nothing was changed.",
    b"reboot": "This amigentd is too old to restart itself. The new one is in C:amigentd now; "
               "restart the Amiga to start it.",
    b"missing": "C:amigentd.new did not arrive, so nothing was changed.",
}
WINDOWS = ("amigent-status", "amigent-status.info", "amigent-monitor", "amigent-monitor.info")
UPDATE_WAIT = 90.0  # seconds for the old amigentd to go and the new one to answer


def update_windows(channel, home: str | None) -> None:
    """amigent-status and amigent-monitor into the Amigent folder, where they differ."""
    if not home:
        try:
            home = channel.read_file("ENVARC:amigent-home").decode("iso-8859-1").strip()
        except AmigaError:
            home = ""
    home = home or "SYS:amigent"
    for name in WINDOWS:
        local = AMIGA / "bin" / name
        if not local.exists():
            continue
        target = f"{home}/{name}"
        try:
            there = channel.read_file(target)
        except AmigaError:
            there = None
        # an icon that is there stays: it holds the user's tool types and place
        if there == local.read_bytes() or (there is not None and name.endswith(".info")):
            continue
        print(f"uploading {name}")
        try:
            channel.write_file(target, local.read_bytes())
        except AmigaError as e:  # a missing or full folder must not stop the update itself
            print(f"  {name} not copied to {home}: {e}")
            return


def update(host: str, home: str | None) -> int:
    """Put this package's amigentd on an Amiga that runs an older one, by network."""
    channel = AmigaChannel(host, timeout=30)
    try:
        channel.connect()
        cpu = channel.exec("CPU CHECK 68020").rc
        if cpu not in (0, 5):
            print(f"C:CPU answered {cpu}, so the CPU is unknown. Copy C:CPU from the Workbench "
                  "disk and try again.")
            return 1
        build = "amigentd.000" if cpu == 5 else "amigentd"
        new = (AMIGA / "bin" / build).read_bytes()
        if home:  # else the script takes the folder from ENVARC:amigent-home, or SYS:amigent
            channel.exec(f'Echo >ENVARC:amigent-home "{home}"')  # for Amigent-Update on the Amiga too
        update_windows(channel, home)
        if channel.read_file("C:amigentd") == new:
            print(f"amigentd on {host} is already the one from Amigent {__version__}.")
            channel.close()
            return 0
        print(f"uploading {build} to {host}")
        channel.write_file("C:amigentd.new", new)
        channel.exec("Delete >NIL: T:amigent-update.done QUIET")
        # the same script that Amigent-Update runs on the Amiga; detached, so it outlives amigentd
        channel.write_file("T:amigent-update.rexx", (AMIGA / "amigent-update.rexx").read_bytes())
        channel.exec("Run >NIL: RexxMast >NIL:")  # ARexx is running on most Amigas; this makes sure
        channel.exec(f"Run >NIL: rx T:amigent-update.rexx {home or ''}".rstrip())
        channel.close()
    except AmigaError as e:
        channel.close()
        print(f"{e}\nWithout a working connection the update needs `amigentd SETUP` on the Amiga "
              "and amigent-setup without --update.")
        return 1
    print("restarting amigentd on the Amiga", end="", flush=True)
    time.sleep(6)
    deadline = time.monotonic() + UPDATE_WAIT
    while time.monotonic() < deadline:
        channel = AmigaChannel(host, timeout=30)
        try:
            channel.connect()
            done = channel.read_file("T:amigent-update.done").strip()  # AmigaError until then
            if done != b"new":
                channel.close()
                print("\n" + UPDATE_TROUBLE.get(done, f"The update script said {done!r}."))
                return 0 if done == b"reboot" else 1
            version = channel.exec("Version C:amigentd").output.strip()
            channel.exec("Delete >NIL: T:amigent-update.done T:amigent-update.rexx QUIET")
            channel.close()
            print(f"\nDone: {version}, protocol {channel.protocol}.")
            return 0
        except AmigaError:
            channel.close()
            print(".", end="", flush=True)
            time.sleep(3)
    print(f"\namigentd on {host} has not answered for {UPDATE_WAIT:.0f} s. Restart the Amiga; "
          "C:amigentd.old is the previous version.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", nargs="?")
    parser.add_argument("--home", help="folder for the Amigent scripts (default SYS:amigent; "
                        "--update takes the one setup used)")
    parser.add_argument("--with-amigactld", action="store_true",
                        help="also install amigactld (automatic for amigentd before protocol 3)")
    parser.add_argument("--allow", help="the PC address amigactld should accept (default: this PC)")
    parser.add_argument("--no-allow", action="store_true", help="let amigactld accept any address")
    parser.add_argument("--serve", nargs="?", const=8001, type=int, metavar="PORT",
                        help="first step: offer amigentd to the Amiga over the network (port 8001)")
    parser.add_argument("--name", help="what to call this Amiga (default: Amiga, or the name it has)")
    parser.add_argument("--allow-plain", action="store_true",
                        help="keep unencrypted connections from other computers even when TLS works")
    parser.add_argument("--update", action="store_true",
                        help="replace the amigentd that already runs with this version's, by network")
    args = parser.parse_args()
    if args.update:
        return update(machines.address(machines.pick(args.host or "")), args.home)
    args.home = args.home or "SYS:amigent"
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
        encrypted = bool(link.flags & 2) and tls.provision(link, host, args.home)
        if not with_actld:
            channel = AmigaChannel(host, key=key)
            channel.connect()
            channel.sysinfo()
            if encrypted and not args.allow_plain and tls.lock_plain(channel, args.home):
                print("From the next start amigentd takes only encrypted connections from other "
                      "computers (--allow-plain keeps the plain port open).")
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
