"""Check the link to the Amiga and clock what it can carry.

Usage: python -m amigent.doctor [NAME OR ADDRESS]

Without an argument it checks AMIGENT_HOST, else the Amiga amigent-setup set up.
"""

from __future__ import annotations

import sys
import time

from . import machines
from .errors import AmigaError
from .channel import Commands
from .amigentd import Amigentd, AmigentdError


def main() -> int:
    if sys.argv[1:] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if len(sys.argv) > 2:
        print(__doc__)
        return 2
    host = machines.pick(sys.argv[1] if len(sys.argv) == 2 else "")
    if not host:
        print("No Amiga known yet: run amigent-setup, or give the Amiga's address.")
        return 2
    failed = False

    ctl = Commands(host)
    try:
        ctl.connect()
        print(f"commands   OK   {ctl.banner}" + (", TLS" if ctl.is_tls else ""))
        print(f"           {ctl.sysinfo()}")
        for volume in ctl.volumes():
            print(f"           {volume['name'].rstrip(':')}: {volume['free'] // 2**20} MB free")
        payload = bytes(512 * 1024)  # half a meg up and back through RAM: is the stopwatch
        start = time.monotonic()
        ctl.write_file("RAM:amigent-speedtest", payload)
        up = len(payload) / (time.monotonic() - start)
        start = time.monotonic()
        ctl.read_file("RAM:amigent-speedtest")
        down = len(payload) / (time.monotonic() - start)
        ctl.delete("RAM:amigent-speedtest")
        print(f"           to Amiga {up / 1024:.0f} KB/s, from Amiga {down / 1024:.0f} KB/s")
    except (AmigaError, OSError) as e:
        print(f"commands   FAIL {e}")
        failed = True

    screen = Amigentd(host)
    try:
        screen.connect()
        print(f"amigentd   OK   {screen.width}x{screen.height}")
        start = time.monotonic()
        screen.capture()
        print(f"           full screen: {time.monotonic() - start:.1f} s")
        start = time.monotonic()
        screen.capture()
        print(f"           unchanged screen: {time.monotonic() - start:.2f} s")
    except (AmigentdError, OSError) as e:
        print(f"amigentd   FAIL {e}")
        failed = True

    entry = machines.find(host)
    where = f'"{entry["name"]}" at {entry["host"]}' if entry else host
    print(f"amiga      {where}")
    failed |= not check_14(ctl, entry or {})
    return 1 if failed else 0


def check_14(ctl, entry: dict) -> bool:
    """What 1.4.0 adds: protocol 6, the disassembler, an emulator's GDB server."""
    from .channel import LOADED_PROTOCOL
    ok = True
    proto = getattr(getattr(ctl, "backend", None), "protocol", 0)
    if proto >= LOADED_PROTOCOL:
        info = ctl.sysinfo()
        print(f"1.4        OK   protocol {proto}: memory, tasks, tracepoints"
              + ("; job output while it runs" if info.get("self") else "; no self path, jobs as before")
              + ("; ALLOWPOKE on" if info.get("poke") == "1" else ""))
    else:
        print(f"1.4        --   amigentd speaks protocol {proto}; amigent-setup --update brings "
              "memory, tasks, tracepoints and live job output")
    try:
        import capstone  # noqa: F401
        print("disasm     OK   capstone")
    except ImportError:
        print("disasm     FAIL capstone is missing: pipx inject amigent capstone")
        ok = False
    gdb_at = entry.get("gdb")
    if gdb_at:
        from . import gdb, others
        try:
            host, port = gdb.parse_address(gdb_at)
            if others.listening(host, port, 2):
                print(f"emulator   OK   GDB server on {gdb_at}")
            else:
                print(f"emulator   --   nothing on {gdb_at}; WinUAE 6.1 with -s debugging_features=gdbserver")
        except gdb.GdbError as e:
            print(f"emulator   FAIL {e}")
            ok = False
    return ok


if __name__ == "__main__":
    sys.exit(main())
