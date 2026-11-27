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

from . import __version__, machines, roster, tls
from .amigentd import Amigentd, AmigentdError
from .channel import CHANNEL_PROTOCOL, AmigaChannel
from .daemons import (CPU_68000, CPU_68020, CPU_68080, Machine, available_names, choose,
                      consider_replacement, detect_cpu)
from .errors import AmigaError
from .legacy import (UNDO_ALLOW, allow_script, fetch_amigactld,  # noqa: F401 (allow_script: old address)
                     reach_amigactld, same_network, startup_script)
from .net import lan_address
from .paths import AMIGA
from .secret import new_key, own_key, save_key

SCRIPTS = ("amigent-repair", "amigent-scan", "menu-add.rexx")
AFF_68080 = 1 << 10  # exec's AttnFlags bit for the 68080 (AFB_68080 10 in ApolloOS's cpu_m68k.h)


def find_amiga() -> str | None:
    """Find the only amigentd on the local /24; require an address if ambiguous."""
    hits = machines.scan()
    if len(hits) > 1:
        print("Several Amigas found: " + ", ".join(hits) + ". Give setup an explicit address.")
        return None
    return hits[0] if hits else None


def run_script(link: Amigentd, text: str) -> None:
    link.put("T:amigent-step", text.encode("iso-8859-1"))
    run(link, "Execute T:amigent-step")


def run(link: Amigentd, command: str) -> None:
    code = link.run(command)
    if code > 5:
        raise SystemExit(f"Amiga said {code} to: {command}")


def apollo_os(info: dict | None = None, version_text: str = "") -> bool:
    """ApolloOS, as opposed to AmigaOS on the same Vampire.

    The public ApolloOS tree calls the system ApolloOS, its exec is version 51
    and sets the 68080 attention bit. AmigaOS on a Vampire runs exec 40 to 47
    and keeps the plain amigentd.
    """
    info = info or {}
    if info.get("os") == "ApolloOS" or "ApolloOS" in (version_text or ""):
        return True
    try:
        major = int(str(info.get("exec_version", "0")).split(".", 1)[0])
    except ValueError:
        major = 0
    try:
        flags = int(str(info.get("attn_flags", "0") or "0"), 0)
    except ValueError:
        flags = 0
    return major >= 51 and bool(flags & AFF_68080)


def _chosen(machine: Machine) -> str:
    """The packaged daemon for ``machine``, or a stop when none can run there."""
    chosen = choose(machine, available_names(AMIGA / "bin"))
    if chosen is None:
        raise SystemExit("this package has no compatible amigentd for this Amiga, so nothing was changed.")
    return chosen.name


def _machine(cpu: int | None, apollo: bool) -> Machine:
    """ApolloOS only runs on a 68080; CPU CHECK 68020 tells no more than 68020 or better."""
    if apollo and cpu is not None and cpu >= CPU_68020:
        return Machine(CPU_68080, apollo=True)
    return Machine(cpu)


def select_build(cpu_rc: int, info: dict | None = None, version_text: str = "") -> str:
    """amigentd.000 on a 68000, amigentd.apollo on ApolloOS, amigentd otherwise.

    amigentd.apollo is optional. When this package does not contain it, an
    ApolloOS machine gets the same amigentd a 68020 gets today.
    """
    if cpu_rc not in (0, 5):
        raise SystemExit(f"C:CPU answered {cpu_rc}, so the CPU is unknown. Copy C:CPU from the "
                         "Workbench disk and try again.")
    cpu = CPU_68000 if cpu_rc == 5 else CPU_68020
    return _chosen(_machine(cpu, apollo_os(info, version_text)))


def amigentd_build(link: Amigentd) -> str:
    """The best packaged daemon that can run on this Amiga.

    A missing 68020 build falls back to the 68000 build when that file is
    packaged. An unknown CPU may take only the 68000 build on a fresh install.
    ApolloOS is selected only when the probe says so and the Apollo daemon is
    already in this package.
    """
    cpu = detect_cpu(link.run)
    if cpu is None:
        print("C:CPU did not say whether this is a 68020 or better (it may be missing), so the "
              "68000 build of amigentd goes on; it runs on every Amiga. amigent-setup --update "
              "--force puts the faster one on once C:CPU answers.")
    apollo = cpu is not None and cpu >= CPU_68020 and _apollo_by_probe(link)
    return _chosen(_machine(cpu, apollo))


def build_for_channel(channel) -> str:
    """The build an Amiga that already runs amigentd should get.

    An unknown CPU keeps the installed daemon. A fresh install may still use the
    68000 build, because there is no installed daemon to preserve.
    """
    cpu = detect_cpu(lambda command: channel.exec(command).rc)
    if cpu is None:
        raise SystemExit("the CPU could not be identified, so the installed amigentd was left in place.")
    apollo = cpu >= CPU_68020 and _apollo_packaged() and _apollo_by_channel(channel)
    return _chosen(_machine(cpu, apollo))


APOLLO_KEPT = ("{name} runs ApolloOS, and this package has no amigentd.apollo, so the amigentd "
               "there (likely a build for the 68080) was left in place; the scripts were renewed. "
               "--force puts the classic amigentd on all the same.")


def apollo_without_build(channel) -> bool:
    """ApolloOS, but no Apollo daemon in this package: an update must not swap the one there."""
    return not _apollo_packaged() and _apollo_by_channel(channel)


def _apollo_packaged() -> bool:
    """Only an Apollo build makes ApolloOS matter; without one nothing is asked."""
    return (AMIGA / "bin" / "amigentd.apollo").is_file()


def _apollo_by_channel(channel) -> bool:
    """ApolloOS by what the running amigentd says, else exec's version text, else the probe."""
    try:
        info = channel.sysinfo() or {}
    except (AmigaError, AttributeError):
        info = {}
    if _reported(info):  # a daemon that already described itself is trusted
        return apollo_os(info)
    try:
        version_text = channel.exec("Version FULL exec.library").output
    except (AmigaError, AttributeError):
        version_text = ""
    # The probe reads exec's version and AttnFlags. A probe that is missing or
    # does not succeed leaves the classic choice in place.
    return apollo_os({}, version_text) or apollo_os({}, _probe_output(channel))


def _probe_output(channel) -> str:
    """What the small ApolloOS probe printed, or '' when it cannot be run."""
    probe = AMIGA / "bin" / "amigent-apollo-probe"
    if not probe.is_file():
        return ""
    try:
        channel.write_file("T:amigent-apollo-probe", probe.read_bytes())
        channel.exec("Protect T:amigent-apollo-probe +e")
        return channel.exec("T:amigent-apollo-probe").output
    except (AmigaError, AttributeError, OSError):
        return ""


def _reported(info: dict) -> bool:
    return bool(info.get("os") or info.get("exec_version") or info.get("attn_flags"))


def _apollo_by_probe(link: Amigentd) -> bool:
    """SETUP only returns a program's exit code.

    An Apollo probe returns 0 only when ApolloOS and the FPGA core bit are both
    present. A missing probe is an unsuccessful probe: the caller keeps the
    classic binary. Without an Apollo build in the package the probe is not
    even sent.
    """
    probe = AMIGA / "bin" / "amigent-apollo-probe"
    if not _apollo_packaged() or not probe.is_file():
        return False
    link.put("T:amigent-apollo-probe", probe.read_bytes())
    link.run("Protect T:amigent-apollo-probe +e")
    return link.run("T:amigent-apollo-probe") == 0


def monitor_file(build: str) -> str:
    """The packaged monitor for a build ``select_build`` already returned.

    The installed name stays ``amigent-monitor``. ApolloOS uses the Apollo
    monitor when that file is in the package, and the general-purpose
    monitor when it is not. Every other build keeps the general-purpose one.
    """
    if build == "amigentd.apollo" and (AMIGA / "bin" / "amigent-monitor.apollo").is_file():
        return "amigent-monitor.apollo"
    return "amigent-monitor"


def window_source(name: str, build: str):
    """Local file to store as ``name``, or None when that window is not packaged."""
    packaged = monitor_file(build) if name == "amigent-monitor" else name
    path = AMIGA / "bin" / packaged
    return path if path.is_file() else None


def install(link: Amigentd, home: str, key: str, allow: str | None, with_actld: bool) -> None:
    build = amigentd_build(link)
    if with_actld:
        binary = fetch_amigactld()
        print("uploading amigactld")
        link.put("C:amigactld", binary)
    print(f"uploading {build}")
    link.put("C:amigentd.new", (AMIGA / "bin" / build).read_bytes())
    run(link, f"MakeDir >NIL: {home}")
    print("uploading scripts")
    for name in SCRIPTS:
        text = (AMIGA / name).read_text(encoding="iso-8859-1").replace("SYS:amigent", home)
        link.put(f"{home}/{name}", text.encode("iso-8859-1"))
    link.put(f"{home}/amigent-repair.info", (AMIGA / "amigent-repair.info").read_bytes())
    for name in WINDOWS:  # the little windows on the Amiga's side
        local = window_source(name, build)
        if local is not None:
            link.put(f"{home}/{name}", local.read_bytes())
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
# seconds for the old amigentd to go and the new one to answer: the script waits up to 2 + 15 + 100 s,
# and if it falls back, the old amigentd may retry its port for another 60 s
UPDATE_WAIT = 180.0


def _noted(channel, name: str) -> str:
    """A folder name amigent-setup left in ENVARC: (amigent-home, amigent-spare), or ''."""
    try:
        return channel.read_file(f"ENVARC:{name}").decode("iso-8859-1").strip()
    except AmigaError:
        return ""


def _put_if_new(channel, target: str, data: bytes) -> bool:
    try:
        if channel.read_file(target) == data:
            return False
    except AmigaError:
        pass
    channel.write_file(target, data)
    return True


def update_scripts(channel, folder: str) -> None:
    """amigent-repair and its helpers as this version has them, for this folder.

    They used to stay as the first installation left them: a repair after a
    system update then ran a script that knew nothing of what came since.
    """
    changed = []
    for name in SCRIPTS:
        text = (AMIGA / name).read_text(encoding="iso-8859-1").replace("SYS:amigent", folder)
        if _put_if_new(channel, f"{folder}/{name}", text.encode("iso-8859-1")):
            changed.append(name)
    if "amigent-repair" in changed:
        channel.exec(f"Protect {folder}/amigent-repair +s")
    if changed:
        print(f"renewed in {folder}: {', '.join(changed)}")


SPARE_COPIES = ("amigent.key", "amigent-tls.pem", "amigent.tlsonly")


def update_spare(channel, home: str, spare: str, amigentd: bytes) -> None:
    """A second Amigent folder on another disk, for when the first is gone: the same
    scripts (repairing from there), this amigentd, key, certificate and repair modules."""
    try:
        channel.exec(f"MakeDir >NIL: {spare}")
        update_scripts(channel, spare)
        if _put_if_new(channel, f"{spare}/amigentd", amigentd):
            print(f"renewed in {spare}: amigentd")
        channel.exec(f"Protect >NIL: {spare}/amigentd +rwed")
        channel.exec(f"Copy >NIL: {home}/amigent-repair.info {spare}/ CLONE QUIET")
        for name in SPARE_COPIES:
            channel.exec(f"Copy >NIL: {home}/{name} {spare}/ CLONE QUIET")
        channel.exec(f"MakeDir >NIL: {spare}/repair.d")
        channel.exec(f"Copy >NIL: {home}/repair.d/#? {spare}/repair.d/ CLONE QUIET")
    except AmigaError as e:  # a disk that is away or full must not stop the update
        print(f"  the spare folder {spare} was not renewed: {e}")


def update_folders(channel, home: str | None, spare: str | None, amigentd: bytes) -> None:
    """Everything next to C:amigentd: the Amigent folder and, if there is one, its spare."""
    home = home or _noted(channel, "amigent-home") or "SYS:amigent"
    if spare:
        channel.exec(f'Echo >ENVARC:amigent-spare "{spare}"')
    spare = spare or _noted(channel, "amigent-spare")
    try:
        update_scripts(channel, home)
    except AmigaError as e:
        print(f"  the scripts in {home} were not renewed: {e}")
    if spare and spare.rstrip("/").lower() != home.rstrip("/").lower():
        update_spare(channel, home, spare, amigentd)


def update_windows(channel, home: str | None, build: str = "amigentd") -> None:
    """amigent-status and amigent-monitor into the Amigent folder, where they differ."""
    home = home or _noted(channel, "amigent-home") or "SYS:amigent"
    for name in WINDOWS:
        local = window_source(name, build)
        if local is None:
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


def update(host: str, home: str | None, spare: str | None = None, force: bool = False) -> int:
    """Put this package's amigentd on an Amiga that runs an older one, by network."""
    channel = AmigaChannel(host, timeout=30)
    shown = roster.name_of(host)  # host may be the saved ID; people know the name
    try:
        channel.connect()
        try:
            build = build_for_channel(channel)
            new = (AMIGA / "bin" / build).read_bytes()
        except SystemExit as e:
            print(e)
            channel.close()
            return 1
        if home:  # else the script takes the folder from ENVARC:amigent-home, or SYS:amigent
            channel.exec(f'Echo >ENVARC:amigent-home "{home}"')  # for Amigent-Update on the Amiga too
        update_windows(channel, home, build)
        try:
            installed = channel.read_file("C:amigentd")
        except AmigaError as e:
            if e.code is None:
                raise  # the line went dead; that says nothing about C:amigentd
            installed = None  # the Amiga answered: there is no C:amigentd to read
        if force and installed != new:
            replace, reason = True, ""
        elif installed is not None and installed != new and apollo_without_build(channel):
            replace, reason = False, APOLLO_KEPT.format(name=shown)
        else:
            replace, reason = consider_replacement(installed, new, build, AMIGA / "bin")
        if not replace:
            # the scripts are renewed all the same; the folders get the amigentd that runs
            update_folders(channel, home, spare, installed or new)
            if installed == new:
                print(f"amigentd on {shown} is already the one from Amigent {__version__}.")
            else:
                print(reason)
            channel.close()
            return 0
        print(f"uploading {build} to {shown}")
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
            print()
            update_folders(channel, home, spare, new)  # only now: the new amigentd has proved itself
            channel.close()
            print(f"Done: {version}, protocol {channel.protocol}.")
            return 0
        except AmigaError:
            channel.close()
            print(".", end="", flush=True)
            time.sleep(3)
    print(f"\namigentd on {shown} has not answered for {UPDATE_WAIT:.0f} s. Restart the Amiga; "
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
    parser.add_argument("--spare", metavar="FOLDER",
                        help="with --update: keep a second Amigent folder on another disk (scripts, amigentd, "
                        "key, repair modules), to repair from when the first is gone; remembered")
    parser.add_argument("--update", action="store_true",
                        help="replace the amigentd that already runs with this version's, by network")
    parser.add_argument("--force", action="store_true",
                        help="with --update: put this version's amigentd on even when the one there "
                        "looks newer or is a build of its own")
    args = parser.parse_args()
    if args.update:
        return update(machines.identity(machines.pick(args.host or "")), args.home, args.spare,
                      args.force)
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
    # Reject a conflicting name before installing anything on the Amiga.
    named = machines.find(name)
    if named and (not known or named["id"] != known["id"]):
        print(f"{name!r} already belongs to another Amiga. Choose another name.")
        return 1
    if known and name != known["name"]:
        print("Rename the saved Amiga in Your Amigas before running setup.")
        return 1
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
