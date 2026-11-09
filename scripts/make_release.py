"""Pack the Amiga side of a release as an .lha archive, using LhA on an Amiga.

Usage: make_release.py HOST

Needs a running amigentd or amigactld on HOST and LhA in its C: (AmiKit has it).
Writes dist/amigent.lha with an Amigent drawer: amigentd (68020),
amigentd.000 (68000) if built, the icon, Amigent-Update, ReadMe and the licence,
and next to it amigent.readme for Aminet. Aminet wants the version in the
readme, not in the file name: uploading amigent.lha again replaces the last one.
The same archive goes to the GitHub release as amigent-<version>.lha.
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

Updating from an earlier Amigent: Amigent has two halves, amigentd on the
Amiga and the program on the PC or Mac. Update both:

- From the PC or Mac, both at once: "pipx upgrade amigent", then
  "amigent-setup --update", which puts the new amigentd on the Amiga.
- Or on the Amiga: copy this drawer over, double-click Amigent-Update.
  Then on the PC or Mac: "pipx upgrade amigent".

Afterwards restart the AI app and the remote desktop. Key, certificate and
the start line in S:User-Startup stay; should the new amigentd not start,
the old one starts again. One side alone keeps working as before, the new
features need both. From 1.3.0 on the PC tells you when the Amiga is ahead.

Requirements: AmigaOS 2.04 or newer (encryption needs OS 3.0 and AmiSSL),
any CPU from the 68000 on (a Vampire's 68080 runs the plain amigentd), a
TCP/IP stack with bsdsocket.library. On the PC or Mac: Python 3.10 or newer.

Tool types of the icon: SETUP (accept the first install from the PC),
KEY=<file> (default S:amigent.key), PORT=<n> (default 6801), QUIET, TLSONLY.

amigent-monitor opens a window that shows, line by line, what the agent
does on this Amiga: the tools it calls, commands, files, clicks and keys,
next to the data rates and totals. Tool types: NAME=<your name> for the
greeting, WIDTH, HEIGHT, LEFT, TOP, FONT, SIZE. The window can be sized
and zoomed. Esc or the close gadget ends it, C clears the log.

amigent-status shows a small window with amigentd's state and its
connections, refreshed every 2 seconds. amigentd also answers ARexx on the
port AMIGENT: STATUS, CLIENTS, VERSION, PAUSE, RESUME, ACTIVATEWINDOW,
TYPE, KEY, BUSY, HELP, QUIT. PAUSE <seconds> stops all screen reading for that
long, so a test or a benchmark has the CPU to itself; RESUME ends it.
ACTIVATEWINDOW <pattern> brings a window to the front, TYPE <text> types
into it through your keymap (keys in angle brackets: <return>, <ctrl c>),
KEY <name> presses one key, BUSY says whether a menu or a popup holds
Intuition up.

Licence: MIT, see LICENSE.
"""


# Aminet files from before the archive lost its version number; they go away with
# the next upload. Empty this once Aminet shows only amigent.lha.
REPLACES = "comm/net/amigent-1.1.1.lha"

AMINET_HEAD = f"""Short:        Remote desktop and AI agent for Amigas
Author:       Andreas Wendorf (Akustikrausch)
Uploader:     akustikrausch@gmail.com (Andreas Wendorf)
Type:         comm/net
Version:      {__version__}
""" + (f"Replaces:     {REPLACES}\n" if REPLACES else "") + """\
Architecture: m68k-amigaos >= 2.0.4
Distribution: Aminet
Requires:     a TCP/IP stack with bsdsocket.library, such as Roadshow
"""

AMINET_BODY = """
A REMOTE DESKTOP FOR YOUR REAL AMIGA, LIKE TEAMVIEWER OR VNC

Sit at your PC or Mac and use your Amiga as if it were in front of you: its
screen live in a window, your own mouse and keyboard on it, Amiga keys and
shortcuts on a side panel, files in either direction (drop them on the
window to send them) and one shared clipboard. No VNC server and no extra
software on the Amiga beyond this archive, and it stays quick even on a
7 MHz 68000 behind a plipbox.

And if you like, an AI agent (Claude, Codex or any other MCP client) takes
over the same way: it runs commands, copies files, installs software and
clicks through programs, while you watch every click in that window and can
take over at any moment.

This archive is the Amiga half: amigentd, the small service that does the
work on the Amiga, amigent-status, a window that shows who is connected,
amigent-monitor, a window that shows what the agent is doing, and
Amigent-Update for updating an earlier installation. The PC or Mac half is a
Python package:

    pipx install amigent

or just tell your AI agent: "Install Amigent from
https://github.com/akustikrausch/amigent for me." It then finds the Amiga on
the network and sets everything up from the PC.


NEW IN THIS VERSION

- Scripts on the Amiga can bring a window to the front and type into it
  through the ARexx port: ACTIVATEWINDOW, TYPE and KEY.
- A drive that isn't there makes a command fail at once instead of opening
  an "insert volume" requester that waits for someone at the Amiga.
- Games and programs on 256-colour graphics card screens come through in
  their real colours in the remote desktop and in screenshots.
- After a lost connection the AI agent learns whether the Amiga started
  again and which guru code it kept.
- An open menu or a popup left open on the desktop, which holds back every
  new window, is noticed; the AI agent closes the popup before it starts a
  program.
- The AI agent reads the text on the screen instead of taking a picture
  where text is enough, and waits on the Amiga for a window or a text.
- Data travel packed when the Amiga packs faster than its network carries,
  such as a PiStorm behind a plipbox: several times as fast.
- amigent-monitor, a window on the Amiga, shows line by line what the AI
  agent does, with the traffic of the link; it fits an A500 screen too and
  can be sized.
- The AI agent moves and sizes windows by mouse movement, as a hand would,
  so drag bars, size gadgets and programs that follow the mouse react.
- Other AI agent bridges on the same Amiga, such as amimcp, are noticed and
  reported before the first click; nothing of theirs is touched.
- Installations can be taken back; Aminet archives install from the PC,
  disk images (.adf, .adz) included.
- kprintf and Enforcer output reach the AI agent (start amigentd with
  NOTEXT or NODEBUG to leave the recording out).


TUNED FOR SLOW AMIGAS AND SLOW NETWORKS

Chipset screens go out as the bitplanes the Amiga keeps anyway, graphics card
screens as tiles with their own small palette, and only what changed. Mouse
and keys are carried out at once, even while a picture is still on its way.

  68000 at 7 MHz, 640x256:  whole screen 0.6-1.0 s, unchanged 80 ms,
                            under 10% of the CPU while the PC watches
  68020 at 14 MHz:          whole screen 0.1 s, 2-3% of the CPU
  1920x1080 RTG on a plipbox (about 50 KB/s): whole screen about 3 s


INSTALLATION

1. Unpack the archive, e.g.  LhA x amigent.lha RAM:
2. Double-click amigentd (amigentd.000 on a plain 68000), or in a Shell:
       RAM:Amigent/amigentd SETUP
3. On the PC or Mac: pipx install amigent, then amigent-setup.
   It copies amigentd to C:, adds it to S:User-Startup and gives it a key.
4. For the remote desktop alone, no AI agent needed: amigent-viewer --install
   puts Amigent into the Start menu (Windows) or Applications (macOS).

A Vampire's 68080 runs the plain amigentd. Tool types and Shell arguments:
SETUP, KEY=<file>, PORT=<n>, QUIET, TLSONLY. amigentd answers ARexx on the
port AMIGENT (STATUS, CLIENTS, VERSION, PAUSE, RESUME, ACTIVATEWINDOW,
TYPE, KEY, BUSY, HELP, QUIT).


UPDATING FROM AN EARLIER AMIGENT

Amigent has two halves, amigentd on the Amiga and the program on the PC or
Mac. Update both:

From the PC or Mac, both at once: pipx upgrade amigent, then
amigent-setup --update. Or on the Amiga: unpack the archive, double-click
Amigent-Update in its drawer, then run pipx upgrade amigent on the PC.

Afterwards restart the AI app and the remote desktop. Key, certificate and
the start line in S:User-Startup stay. Should the new amigentd not start, the
old one starts again. One side alone keeps working as before; the new
features need both. From 1.3.0 on the PC tells you when the Amiga is ahead.


REQUIREMENTS

AmigaOS 2.04 or newer, any CPU from the 68000 on, a network connection (a
plipbox on the parallel port is enough). Encrypted connections need OS 3.0
and AmiSSL 5.1 or newer. Updating on the Amiga needs ARexx (RexxMast). On
the PC: Windows, macOS or Linux with Python 3.10 or newer.


SECURITY

amigentd asks every connection for a key that amigent-setup creates, and with
AmiSSL it encrypts the connection with TLS, pinned to the certificate made for
this Amiga. Keep it in your home network; do not open its port to the
internet.


Source, documentation and the wire protocol:
https://github.com/akustikrausch/amigent

Licence: MIT.
"""


def aminet_readme() -> str:
    text = AMINET_HEAD + AMINET_BODY
    short = text.splitlines()[0].split(":", 1)[1].strip()
    assert len(short) <= 40, "Aminet allows 40 characters in Short:"
    assert all(len(line) <= 78 for line in text.splitlines()), "keep readme lines short"
    return text


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
    if (BIN / "amigent-monitor").exists():
        files["amigent-monitor"] = BIN / "amigent-monitor"
        files["amigent-monitor.info"] = BIN / "amigent-monitor.info"
    if (BIN / "amigentd.000").exists():
        files["amigentd.000"] = BIN / "amigentd.000"
        files["amigentd.000.info"] = BIN / "amigentd.info"
    amiga = ROOT / "src" / "amigent" / "amiga"
    for name in ("Amigent-Update", "Amigent-Update.info", "amigent-update.rexx"):
        files[name] = amiga / name
    for name, source in files.items():
        ctl.write_file(f"{folder}/{name}", source.read_bytes())
    ctl.write_file(f"{folder}/ReadMe", README.encode("iso-8859-1"))
    ctl.write_file(f"{stage}/Amigent.info", (BIN / "Amigent.info").read_bytes())
    for name in ("amigentd", "amigentd.000", "amigent-status", "amigent-monitor"):
        if name in files:
            ctl.exec(f"Protect {folder}/{name} +rwed")
    archive = "amigent.lha"
    result = ctl.exec(f"LhA -r -x -q a {stage}/{archive} Amigent Amigent.info", cd=stage, timeout=120)
    if result.rc > 5:
        raise SystemExit(f"LhA said {result.rc}:\n{result.output}")
    data = ctl.read_file(f"{stage}/{archive}")
    ctl.exec(f"Delete >NIL: {stage} ALL QUIET")
    out = ROOT / "dist" / archive
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(data)
    (out.parent / f"amigent-{__version__}.lha").write_bytes(data)
    (out.parent / "amigent.readme").write_text(aminet_readme(), encoding="iso-8859-1")
    print(f"{out} ({len(data)} bytes), amigent-{__version__}.lha for GitHub, amigent.readme")


if __name__ == "__main__":
    main()
