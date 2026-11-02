```
     _____            .__                            __
    /  _  \    _____  |__|   ____    ____    ____  _/  |_
   /  /_\  \  /     \ |  |  / ___\ _/ __ \  /    \ \   __\
  /    |    \|  Y Y  \|  | / /_/  >\  ___/ |   |  \ |  |
  \____|__  /|__|_|  /|__| \___  /  \___  >|___|  / |__|
          \/       \/     /_____/       \/      \/

     complete remote control for a real Amiga, by AI or manually
```

<p align="center"><img src="https://raw.githubusercontent.com/akustikrausch/amigent/main/src/amigent/web/icon.png" width="96" alt="Amigent icon"></p>

Amigent connects your PC or Mac to your real Amiga over the home network. Then you can:

- **let an AI agent do the work**, for example Claude Code or Codex: install
  software, update programs, change settings, copy files, click through
  programs. Just tell it what you want.
- **use the Amiga yourself from your computer**, as a remote desktop like
  TeamViewer or VNC: a window with the live screen, your own mouse and
  keyboard, file transfer and a shared clipboard.

Both at the same time: you see every click the agent makes and can take over at
any moment.

Amigent is built for the Amiga you actually have: a plain A500 on a plipbox
works, an accelerated one flies.

![Amigent with a Workbench screen and the keyboard panel](https://raw.githubusercontent.com/akustikrausch/amigent/main/docs/screenshots/viewer.png)

## Installation

### On the Amiga

**1.** Copy [`amigentd`](https://github.com/akustikrausch/amigent/blob/main/src/amigent/amiga/bin/amigentd) to the Amiga (`amigentd.000` on a
68000), or unpack the archive from the
[releases](https://github.com/akustikrausch/amigent/releases).

**2.** Start it with `amigentd SETUP` in a Shell, or double-click its icon.

### On the PC or Mac

**3.** Paste this into your AI agent:

    Install Amigent for me: run "pipx install amigent" (if pipx or Python 3.10
    or newer is missing, install them first: Windows winget, macOS Homebrew,
    Linux the package manager). Then run amigent-setup; it finds my Amiga on
    the network by itself. Register the MCP server "amigent" with the command
    amigent-server, and run amigent-viewer --install so the remote desktop
    shows up as a program. Tell me if you need to be restarted.

The agent sets up the Amiga over the network; from then on amigentd starts
with it.

**Windows:** Amigent appears in the Start menu. For an encrypted connection
`openssl` is needed, which comes with Git for Windows.

**macOS:** Amigent appears in `~/Applications`. On its first start, allow it
access to the local network, or it cannot reach the Amiga. Cmd is the right
Amiga key.

**Linux:** there is no program entry; start the remote desktop with
`amigent-viewer`.

Without an AI agent: see the [manual installation](https://github.com/akustikrausch/amigent/blob/main/docs/GUIDE.md#manual-installation).

## What you need

- An Amiga with AmigaOS 3.x, any CPU from the 68000 on, a network connection
  (a plipbox on the parallel port is enough) and a TCP/IP stack such as Roadshow.
- A PC or Mac with Windows, macOS or Linux and Python 3.10 or newer.

## What you can ask for

    "Check which programs on the Amiga are outdated."
    "Install the game from my Downloads folder and add it to the Games menu."
    "Copy the demo to the Amiga and start it."
    "Open the remote desktop."

## Made for slow machines and slow wires

Most of the work in Amigent went into making a slow Amiga on a slow network
feel quick, for the AI agent and for you at the mouse:

- **The screen travels as the Amiga keeps it.** Chipset screens go out as
  bitplanes, graphics-card screens as small palette tiles, and only the tiles
  that changed, often as the difference to what you already have.
- **Your input comes first.** Mouse moves, clicks and keys are carried out
  even while a picture is still on its way, and the area around the pointer is
  sent before the rest.
- **The Amiga keeps its time.** While nothing happens, Amigent asks rarely
  and costs the Amiga only a few percent of its CPU.
- **Files go in one go.** Whole folders travel in a single stream, and files
  that are already there stay where they are.

Measured with a 640×256 Workbench screen:

| Amiga | whole screen | unchanged screen | CPU while watching |
|---|---|---|---|
| 68000, 7 MHz | 0.6 s | 80 ms | under 10 % |
| A1200, 68020 at 14 MHz | 0.1 s | 15 ms | 2–3 % |

On a 1920×1080 graphics-card screen over a plipbox (about 50 KB/s):

![Seconds per screen update over a plipbox: whole screen as raw pixels 124 s each time; Amigent 1.9 s for the first picture, 170 ms for a typed line, 7 ms when nothing changed](https://raw.githubusercontent.com/akustikrausch/amigent/main/docs/screenshots/speed.svg)

The orange bars are what moving the whole screen as raw 24-bit pixels costs on
that wire, every time; the blue ones are Amigent, measured on a real A1200.
`scripts/bench_viewer.py` and `scripts/bench_mcp.py` repeat the measurements
on a simulated slow link.

## Good to know

- **New address after a restart?** Amigent finds the Amiga again by itself. It
  takes only the one that proves to be yours.
- **Several Amigas?** `amigent-fleet` shows them side by side with a preview.
- **After a big OS update**, `amigent-repair` puts back what the update removed.
- **Safe at home.** A key keeps other computers on the network out, and the
  connection can be encrypted. Don't open the Amiga to the internet.

More in the [guide](https://github.com/akustikrausch/amigent/blob/main/docs/GUIDE.md) and the [FAQ](https://github.com/akustikrausch/amigent/blob/main/docs/FAQ.md).

## Related projects

[amimcp](https://github.com/thomas-luebker/amimcp) also connects AI agents to
real Amigas, [MCP-AmigaOS4](https://github.com/derfsss/MCP-AmigaOS4) does it
for AmigaOS 4.1, and WinUAE and Amiberry have MCP servers of their own.

## Thanks

tbdye for amigactld, Volker Barthelmann for vbcc, and the PiStorm, Emu68, AmiKit
and Aminet people.

## License

MIT, see [LICENSE](https://github.com/akustikrausch/amigent/blob/main/LICENSE). By Andreas Wendorf (Akustikrausch).
Changes are in the [changelog](https://github.com/akustikrausch/amigent/blob/main/CHANGELOG.md).
