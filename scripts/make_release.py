"""Pack the Amiga side of a release as an .lha archive, using LhA on an Amiga.

Usage: make_release.py HOST

Needs a running amigentd or amigactld on HOST and LhA in its C: (AmiKit has it).
Writes dist/amigent-<version>.lha with an Amigent drawer: amigentd (68020),
amigentd.000 (68000) if built, the icon, ReadMe and the licence.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "src" / "amigent" / "amiga" / "bin"
sys.path.insert(0, str(ROOT / "src"))

from amigent import __version__  # noqa: E402
from amigent.channel import Commands  # noqa: E402

README = f"""Amigent {__version__}

A remote desktop for your real Amiga, like TeamViewer or VNC, and an AI agent
that can work on it, both from a PC or Mac over the home network.
https://github.com/akustikrausch/amigent

This drawer is the Amiga half. The PC or Mac half comes from the internet.

On the Amiga

1. Copy this drawer to the Amiga, for example to RAM:.
2. Double-click amigentd, or run "amigentd SETUP" in a Shell.
   On a plain 68000 take amigentd.000 instead.

On the PC or Mac

3. Paste this into your AI agent (Claude, Codex or any other MCP client):

     Install Amigent from https://github.com/akustikrausch/amigent for me.

   The agent installs the Python package with "pipx install amigent", runs
   amigent-setup, which finds the Amiga on the network, copies amigentd to
   C:, adds it to S:User-Startup and gives it a key, and sets up the
   remote desktop as a program.

   Without an AI agent, in a terminal:

     pipx install amigent
     amigent-setup
     amigent-viewer --install

   The full guide is on the project page above.

Requirements: AmigaOS 2.04 or newer (encryption needs OS 3.0 and AmiSSL),
any CPU from the 68000 on (a Vampire's 68080 runs the plain amigentd), a
TCP/IP stack with bsdsocket.library. On the PC or Mac: Python 3.10 or newer.

Tool types of the icon: SETUP (accept the first install from the PC),
KEY=<file> (default S:amigent.key), PORT=<n> (default 6801), QUIET, TLSONLY.

amigent-status shows a small window with amigentd's state and its
connections, refreshed every 2 seconds. amigentd also answers ARexx on the
port AMIGENT: STATUS, CLIENTS, VERSION, HELP, QUIT.

Licence: MIT, see LICENSE.
"""


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    host = sys.argv[1]
    ctl = Commands(host, timeout=120)
    stage = "RAM:amigent-release"
    folder = f"{stage}/Amigent"
    ctl.exec(f"Delete >NIL: {stage} ALL QUIET")
    ctl.makedir(stage)
    ctl.makedir(folder)
    files = {"amigentd": BIN / "amigentd", "amigentd.info": BIN / "amigentd.info",
             "LICENSE": ROOT / "LICENSE"}
    if (BIN / "amigent-status").exists():
        files["amigent-status"] = BIN / "amigent-status"
        files["amigent-status.info"] = BIN / "amigent-status.info"
    if (BIN / "amigentd.000").exists():
        files["amigentd.000"] = BIN / "amigentd.000"
        files["amigentd.000.info"] = BIN / "amigentd.info"
    for name, source in files.items():
        ctl.write_file(f"{folder}/{name}", source.read_bytes())
    ctl.write_file(f"{folder}/ReadMe", README.encode("iso-8859-1"))
    ctl.write_file(f"{stage}/Amigent.info", (BIN / "Amigent.info").read_bytes())
    for name in ("amigentd", "amigentd.000", "amigent-status"):
        if name in files:
            ctl.exec(f"Protect {folder}/{name} +rwed")
    archive = f"amigent-{__version__}.lha"
    result = ctl.exec(f"LhA -r -x -q a {stage}/{archive} Amigent Amigent.info", cd=stage, timeout=120)
    if result.rc > 5:
        raise SystemExit(f"LhA said {result.rc}:\n{result.output}")
    data = ctl.read_file(f"{stage}/{archive}")
    ctl.exec(f"Delete >NIL: {stage} ALL QUIET")
    out = ROOT / "dist" / archive
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(data)
    print(f"{out} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
