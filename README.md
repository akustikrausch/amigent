```
     _____            .__                            __
    /  _  \    _____  |__|   ____    ____    ____  _/  |_
   /  /_\  \  /     \ |  |  / ___\ _/ __ \  /    \ \   __\
  /    |    \|  Y Y  \|  | / /_/  >\  ___/ |   |  \ |  |
  \____|__  /|__|_|  /|__| \___  /  \___  >|___|  / |__|
          \/       \/     /_____/       \/      \/

     complete remote control for a real Amiga, by AI or by hand
```

<p align="center"><img src="src/amigent/web/icon.png" width="96" alt="Amigent icon"></p>

Amigent connects your PC to your real Amiga over the home network. Then you can:

- **let an AI agent do the work**, for example Claude Code or Codex: install
  software, update programs, change settings, copy files, click through
  programs. Just tell it what you want.
- **use the Amiga yourself from the PC**, in a window with the live screen, your
  own mouse and keyboard, file transfer and a shared clipboard.

Both at the same time: you see every click the agent makes and can take over at
any moment.

![Amigent with a Workbench screen and the keyboard panel](docs/screenshots/viewer.png)

## Installation

**1.** Copy [`amigentd`](amiga/bin/amigentd) to your Amiga, for example to
`RAM:`. A browser, USB stick or SD card all work, or the PC offers it on the
network (see the [FAQ](docs/FAQ.md)). Or take the archive with icon from the
[releases](https://github.com/akustikrausch/amigent/releases).

**2.** Start it in a Shell, or double-click its icon:

    RAM:amigentd SETUP

On a plain 68000, take `amigentd.000` instead.

**3.** Paste this into your AI agent on the PC:

    Install Amigent from https://github.com/akustikrausch/amigent for me.
    If git or Python 3.10 or newer is missing on this computer, install it
    first (Windows: winget, macOS: Homebrew or python.org, Linux: the package
    manager). Clone the repository, run pip install -e . in it, then run
    amigent-setup. It finds my Amiga by itself and remembers it. Register the
    MCP server "amigent" with the command amigent-server. Then run
    amigent-viewer --install so the remote desktop shows up as a program.
    Tell me if you need to be restarted.

That's it. The Amiga is set up from the PC and Amigent starts with it from now
on. You'll find **Amigent** in the Start menu or the Applications folder.

No AI agent? The [guide](docs/GUIDE.md#installation-by-hand) has the same steps
by hand.

## What you need

- An Amiga with AmigaOS 3.x, any CPU from the 68000 on, a network connection
  (a plipbox on the parallel port is enough) and a TCP/IP stack such as Roadshow.
  The faster the CPU, the smoother the screen.
- A PC with Windows, macOS or Linux and Python 3.10 or newer.

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

Amigent is the one to pick if you want to use the Amiga by hand as well, have a
slow network, or want help keeping the machine in shape.

## Thanks

tbdye for amigactld, Volker Barthelmann for vbcc, and the PiStorm, Emu68, AmiKit
and Aminet people.

## License

MIT, see [LICENSE](LICENSE). By Andreas Wendorf (Akustikrausch).
Changes are in the [changelog](CHANGELOG.md).
