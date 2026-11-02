"""Check both Amiga services and measure what the link can carry.

Usage: python -m amigent.doctor HOST
"""

from __future__ import annotations

import sys
import time

from .amigactl import AmigaError
from .channel import Commands
from .amigentd import Amigentd, AmigentdError


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    host = sys.argv[1]
    failed = False

    ctl = Commands(host)
    try:
        ctl.connect()
        print(f"amigactld  OK   {ctl.banner}")
        print(f"           {ctl.sysinfo()}")
        for volume in ctl.volumes():
            print(f"           {volume['name']}: {volume['free'] // 2**20} MB free")
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
        print(f"amigactld  FAIL {e}")
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

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
