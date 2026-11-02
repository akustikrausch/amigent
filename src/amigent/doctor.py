"""Check the connection to the Amiga and measure what the link can carry.

Usage: python -m amigent.doctor [NAME OR ADDRESS]

Without an argument it checks AMIGENT_HOST, else the Amiga amigent-setup set up.
"""

from __future__ import annotations

import sys
import time

from . import machines
from .amigactl import AmigaError
from .channel import Commands
from .amigentd import Amigentd, AmigentdError


def main() -> int:
    if len(sys.argv) > 2 or sys.argv[1:] in (["-h"], ["--help"]):
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
        print(f"commands   OK   {ctl.banner}" + (", TLS" if hasattr(ctl._sock, "version") else ""))
        print(f"           {ctl.sysinfo()}")
        for volume in ctl.volumes():
            print(f"           {volume['name'].rstrip(':')}: {volume['free'] // 2**20} MB free")
        payload = bytes(512 * 1024)
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
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
