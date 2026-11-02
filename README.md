```
     _____            .__                            __
    /  _  \    _____  |__|   ____    ____    ____  _/  |_
   /  /_\  \  /     \ |  |  / ___\ _/ __ \  /    \ \   __\
  /    |    \|  Y Y  \|  | / /_/  >\  ___/ |   |  \ |  |
  \____|__  /|__|_|  /|__| \___  /  \___  >|___|  / |__|
          \/       \/     /_____/       \/      \/

     complete remote control for a real Amiga, by AI or manually
```

<p align="center"><img src="src/amigent/web/icon.png" width="96" alt="Amigent icon"></p>

Amigent connects your PC or Mac to your real Amiga over the home network. Then you can:

- **let an AI agent do the work**, for example Claude Code or Codex: install
  software, update programs, change settings, copy files, click through
  programs. Just tell it what you want.
- **use the Amiga yourself from your computer**, as a remote desktop like
  TeamViewer or VNC: a window with the live screen, your own mouse and
  keyboard, file transfer and a shared clipboard.

Both at the same time: you see every click the agent makes and can take over at
any moment.

![Amigent with a Workbench screen and the keyboard panel](docs/screenshots/viewer.png)

## Installation

### On the Amiga

**1.** Copy [`amigentd`](amiga/bin/amigentd) to the Amiga (`amigentd.000` on a
68000), or unpack the archive from the
[releases](https://github.com/akustikrausch/amigent/releases).

**2.** Start it with `amigentd SETUP` in a Shell, or double-click its icon.

### On the PC or Mac

**3.** Paste this into your AI agent:

    Install Amigent from https://github.com/akustikrausch/amigent for me.
    If git or Python 3.10 or newer is missing on this computer, install it
    first (Windows: winget, macOS: Homebrew or python.org, Linux: the package
    manager). Clone the repository, create a virtual environment .venv in it,
    run pip install -e . there, then run amigent-setup from it. It finds my
    Amiga by itself and remembers it. Register the MCP server "amigent" with
    the full path of amigent-server in .venv. Then run amigent-viewer
    --install so the remote desktop shows up as a program. Tell me if you
    need to be restarted.

The agent sets up the Amiga over the network; from then on amigentd starts
with it.

**Windows:** Amigent appears in the Start menu. For an encrypted connection
`openssl` is needed, which comes with Git for Windows.

**macOS:** Amigent appears in `~/Applications`. On its first start, allow it
access to the local network, or it cannot reach the Amiga. Cmd is the right
Amiga key.

**Linux:** there is no program entry; start the remote desktop with
`amigent-viewer`.

Without an AI agent: see the [manual installation](docs/GUIDE.md#manual-installation).

## What you need

- An Amiga with AmigaOS 3.x, any CPU from the 68000 on, a network connection
  (a plipbox on the parallel port is enough) and a TCP/IP stack such as Roadshow.
  The faster the CPU, the smoother the screen.
- A PC or Mac with Windows, macOS or Linux and Python 3.10 or newer.

## What you can ask for

    "Check which programs on the Amiga are outdated."
    "Install the game from my Downloads folder and add it to the Games menu."
    "Copy the demo to the Amiga and start it."
    "Open the remote desktop."

## Good to know

- **Slow networks are fine.** Only the parts of the screen that change are sent.
- **New address after a restart?** Amigent finds the Amiga again by itself. It
  takes only the one that proves to be yours.
- **Several Amigas?** `amigent-fleet` shows them side by side with a preview.
- **After a big OS update**, `amigent-repair` puts back what the update removed.
- **Safe at home.** A key keeps other computers on the network out, and the
  connection can be encrypted. Don't open the Amiga to the internet.

More in the [guide](docs/GUIDE.md) and the [FAQ](docs/FAQ.md).

## Other projects

- [amimcp](https://github.com/thomas-luebker/amimcp) by Thomas Luebker also lets
  an AI agent control a real Amiga, from OS 2.04 on, with amifleet as a native
  Mac app for several machines. Use it if your Amiga runs OS 2.x or you want
  nothing but Python's standard library on the PC.
- [MCP-AmigaOS4](https://github.com/derfsss/MCP-AmigaOS4) is for AmigaOS 4.1.
- For emulators such as WinUAE and Amiberry there are MCP servers of their own.

Amigent is the one to pick if you want to use the Amiga yourself as well, have a
slow network, or want help keeping the machine in shape.

## Thanks

tbdye for amigactld, Volker Barthelmann for vbcc, and the PiStorm, Emu68, AmiKit
and Aminet people.

## License

MIT, see [LICENSE](LICENSE). By Andreas Wendorf (Akustikrausch).
Changes are in the [changelog](CHANGELOG.md).
