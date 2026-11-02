"""amigactld, the old crew: only setup's --with-amigactld path and pre-protocol-3 amigentd come here.

Fetching it from its project page, the ALLOW line that locks it to this PC
and the Startup lines that bring it up with the machine. Current installs
never touch this file; it stays until the last old amigentd retires.
"""

from __future__ import annotations

import json
import time
import urllib.request

from . import lzh
from .amigactl import AmigaCtl
from .errors import AmigaError

UPSTREAM = "https://api.github.com/repos/tbdye/amigactl/releases/latest"


def fetch_amigactld() -> bytes:
    request = urllib.request.Request(UPSTREAM, headers={"User-Agent": "amigent-setup"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    url = next(a["browser_download_url"] for a in release["assets"] if a["name"].endswith(".lha"))
    print(f"fetching amigactld {release['tag_name']}")
    with urllib.request.urlopen(url, timeout=120) as response:
        return lzh.extract(response.read(), "/amigactld")


def same_network(a: str, b: str) -> bool:
    return a.rsplit(".", 1)[0] == b.rsplit(".", 1)[0]


def allow_script(address: str, home: str) -> str:
    """AmigaDOS script: ALLOW address in S:amigactld.conf, old lines untouched, backup first."""
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


def startup_script(home: str) -> str:
    """Script tail for the install: amigactld into C:, a spare copy, User-Startup, and go."""
    return f"""Protect C:amigactld rwed
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


def reach_amigactld(host: str, wait: float = 40) -> AmigaCtl | None:
    """A live amigactld link, or None if it stays mute or shows us the door."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            ctl = AmigaCtl(host)
            ctl.connect()
            return ctl
        except AmigaError:
            time.sleep(3)
    return None
