```
     _____            .__                            __
    /  _  \    _____  |__|   ____    ____    ____  _/  |_
   /  /_\  \  /     \ |  |  / ___\ _/ __ \  /    \ \   __\
  /    |    \|  Y Y  \|  | / /_/  >\  ___/ |   |  \ |  |
  \____|__  /|__|_|  /|__| \___  /  \___  >|___|  / |__|
          \/       \/     /_____/       \/      \/

     complete remote control for a real Amiga, by AI or manually
            tuned for slow Amigas and slow networks
```

<p align="center"><img src="https://raw.githubusercontent.com/akustikrausch/amigent/main/src/amigent/web/icon.png" width="96" alt="Amigent icon"></p>

Amigent connects your PC or Mac to your real Amiga over the home network. Then you can:

- **let an AI agent do the work**, for example Claude Code or Codex: install
  software, update programs, change settings, copy files, click through
  programs. Just tell it what you want.
- **use the Amiga yourself from your computer**, as a remote desktop like
  TeamViewer or VNC: a window with the live screen, your own mouse and
  keyboard, file transfer and a shared clipboard.
- **and both of these quickly, even on a slow machine.** Amigent is tuned for
  slow Amigas and slow networks: a 7 MHz 68000 sends its whole screen in
  under a second, and over a plipbox a click shows its result in a fraction of a second.
  [How it stays fast](https://github.com/akustikrausch/amigent#made-for-slow-machines-and-slow-wires).

Both at the same time: you see every click the agent makes and can take over at
any moment.

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

### Updating

Paste this into your AI agent:

    Update Amigent for me: run "pipx upgrade amigent", then
    "amigent-setup --update", which puts the new amigentd on my Amiga over
    the network. Tell me if you need to be restarted.

Or in a terminal:

    pipx upgrade amigent
    amigent-setup --update

At the Amiga instead: unpack the new archive from the
[releases](https://github.com/akustikrausch/amigent/releases) or Aminet and
double-click *Amigent-Update* in its drawer. Key, encryption and the start
line stay as they are. New features such as the screen pause need both sides
updated; one side alone keeps working as before.

## What you need

- An Amiga with AmigaOS 2.04 or newer, any CPU from the 68000 on, a network connection
  (a plipbox on the parallel port is enough) and a TCP/IP stack such as Roadshow.
- A PC or Mac with Windows, macOS or Linux and Python 3.10 or newer.

## What you can ask for

    "Check which programs on the Amiga are outdated."
    "Install the game from my Downloads folder and add it to the Games menu."
    "Copy the demo to the Amiga and start it."
    "Open the remote desktop."
    "Install the newest HippoPlayer from Aminet, so I can take it back if it doesn't work."
    "Build my program, run it on the Amiga and show me its debug output."

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
- **Text where text is enough.** The AI agent reads what programs write on
  the screen as text, a few hundred bytes, and takes a picture only of the
  part where it needs one. It waits on the Amiga itself for a window or a
  line of text instead of looking again and again.
- **Packed when the Amiga is fast.** With an accelerator behind a slow
  network, replies, files and screen tiles travel packed; a slow CPU on a
  fast network stays unpacked. Amigent measures both and decides.

Measured with a 640×256 Workbench screen:

| Amiga | whole screen | unchanged screen | CPU while watching |
|---|---|---|---|
| 68000, 7 MHz | 0.6–1.0 s | 80–95 ms | under 10 % |
| A1200, 68020 at 14 MHz | 0.1 s | 15 ms | 2–3 % |

On a 1920×1080 graphics-card screen over a plipbox (about 50 KB/s):

![Seconds per screen update over a plipbox, bar lengths to scale: whole screen as raw pixels 124 s each time; Amigent 1.9 s for the first picture (64 times faster), 170 ms for a typed line (730 times), 7 ms when nothing changed (18,000 times)](https://raw.githubusercontent.com/akustikrausch/amigent/main/docs/screenshots/speed.svg)

The orange bars are what moving the whole screen as raw 24-bit pixels costs on
that wire, every time; the blue ones are Amigent, measured on a real A1200.
All numbers, how they were taken and how to repeat them are in
[docs/PERFORMANCE.md](https://github.com/akustikrausch/amigent/blob/main/docs/PERFORMANCE.md).

## Good to know

- **New address after a restart?** Amigent finds the Amiga again by itself. It
  takes only the one that proves to be yours.
- **Several Amigas?** `amigent-fleet` shows them side by side with a preview.
- **Running a test or a benchmark?** *Pause the picture* in the remote desktop
  (or `PAUSE` by ARexx) gives the Amiga's CPU back to it for a while; mouse,
  keys and files keep working.
- **After a big OS update**, `amigent-repair` puts back what the update removed.
- **Safe at home.** A key keeps other computers on the network out, and the
  connection can be encrypted. Don't open the Amiga to the internet.

More in the [guide](https://github.com/akustikrausch/amigent/blob/main/docs/GUIDE.md) and the [FAQ](https://github.com/akustikrausch/amigent/blob/main/docs/FAQ.md).

## License

MIT, see [LICENSE](https://github.com/akustikrausch/amigent/blob/main/LICENSE). By Andreas Wendorf (Akustikrausch).
Changes are in the [changelog](https://github.com/akustikrausch/amigent/blob/main/CHANGELOG.md).
