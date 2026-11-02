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

Remote control for a real Amiga from a PC: by an AI agent over MCP, or by
hand in a remote desktop window. https://github.com/akustikrausch/amigent

amigentd is the small service that runs on the Amiga. To install everything:

1. Copy this drawer to the Amiga, for example to RAM:.
2. Double-click amigentd (or run "amigentd SETUP" in a Shell).
   amigentd.000 is the same for a plain 68000.
3. On the PC, run amigent-setup. It finds the Amiga, copies amigentd to C:,
   adds it to S:User-Startup and gives it a key.

Requirements: AmigaOS 2.04 or newer (TLS needs OS 3 and AmiSSL), any CPU from
the 68000 on (amigentd.000
on a plain 68000; amigentd.080 is a build for the Vampire's 68080, which also
runs the plain amigentd), a TCP/IP stack with bsdsocket.library.

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
    if (BIN / "amigentd.080").exists():
        files["amigentd.080"] = BIN / "amigentd.080"
        files["amigentd.080.info"] = BIN / "amigentd.info"
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
    for name in ("amigentd", "amigentd.000", "amigentd.080", "amigent-status"):
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
