```
                    .__                       __
  _____    _____    |__|  ____    ____   _____/  |_
  \__  \  /     \   |  | / ___\ _/ __ \ /    \   __\
   / __ \|  Y Y  \  |  |/ /_/  >\  ___/|   |  \  |
  (____  /__|_|  /  |__|\___  /  \___  >___|  /__|
       \/      \/      /_____/       \/     \/

            your amiga. claude at the keyboard.
```

<p align="center"><img src="src/amigent/web/icon.png" width="96" alt="Amigent icon"></p>

Two ways to drive your real Amiga from the PC. No emulator, the real thing,
over your LAN.

**1. Claude takes over, completely.** Tell Claude Code what you want and it
happens on the Amiga. All of it: Shell, files, installing and updating software,
settings, the start menu, and the screen with mouse and keyboard when a program
wants clicking. It opens a Shell, moves files around, clicks the requester away,
types into the tracker, puts the new game where it belongs and tidies up after.

**2. You take over: remote desktop.** Amigent is a window on your PC with the
Amiga's screen live in it. Your mouse and keyboard drive the machine, the Amiga
keys sit in a side panel, and files go over to the folder you pick. Ask Claude
for it, or start it from the Start menu like any other program.

    "check what's outdated on the Amiga"
    "the game in my Downloads folder: install it, sort it in, clean up"
    "put that demo on the desktop and run it"
    "I want to drive myself"

code and madness by **Akustikrausch**

## what you need

Amiga side: AmigaOS 3.x, 68040 or better (PiStorm is perfect), an RTG graphics
card, a TCP/IP stack such as Roadshow, and a network connection of any kind.
Yes, a plipbox on the parallel port is enough.

PC side: Python 3.10 or newer and [Claude Code](https://claude.com/claude-code). For
the viewer, Edge or Chrome. Windows has Edge anyway.

## get it running

Three steps, and you only do the first two by hand.

**1. Get one file to your Amiga**

[`amiga/bin/amigentd`](amiga/bin/amigentd). Eight kilobytes. Browser, USB stick,
SD card, null modem, whatever you've got. Put it in `RAM:`.

**2. Start it from a Shell**

    RAM:amigentd SETUP

**3. Paste this into Claude Code**

    Install amigent from https://github.com/akustikrausch/amigent for me.
    Clone it, run pip install -e . in it, then run amigent-setup. It finds my
    Amiga by itself and prints the address. Register the MCP server with
    claude mcp add amigent -e AMIGENT_HOST=<address> -- amigent-server
    If I'm on Windows, also run amigent-viewer <address> --install.
    Tell me to restart Claude Code when you're done.

That's all. `amigent-setup` finds the machine, pulls the latest
[amigactld](https://github.com/tbdye/amigactl) straight from its project page,
copies everything over and adds both services to `S:User-Startup`. From then on
they come up with the Amiga.

If you'd rather do it yourself:

```bash
pip install -e .
```

```bash
amigent-setup 192.168.1.50
```

```bash
claude mcp add amigent -e AMIGENT_HOST=192.168.1.50 -- amigent-server
```

## take the wheel

Tell Claude "manual control", or "show me the Amiga", and the viewer opens as its
own window. Or skip Claude and start **Amigent** from the Start menu.

![Amigent with a live Workbench and the keyboard panel](docs/screenshots/viewer.png)

- **the screen, live.** Only changed 64x64 tiles travel, the area around your
  pointer gets looked at first. Moving the mouse costs a few milliseconds.
- **mouse and keyboard.** Click, drag, right button for menus, wheel. Text mode
  types through the Amiga's own keymap, so umlauts and AltGr just work. Direct
  mode sends raw key positions and holds keys for as long as you do. For games.
- **a slice of Amiga keyboard** on the right, in A1200 beige: Esc, Tab, Del, Help, Return, the
  wide F-keys, and the two Amiga keys with their solid and hollow A. Click a
  modifier once for the next key, double-click to lock it. Its LED tells you
  which is which. Above that the usual combinations: Workbench to front, next
  screen, Open, Save, Quit, Copy, Paste, Ctrl-C for the Shell.
- **files.** *Send file* in the toolbar, or drop files on the screen. Pick a
  folder on the Amiga in the little browser, `RAM:` is the default. Progress
  bar, checksum at the end, and it asks before it overwrites something.

  ![Sending a file: pick the volume, click into the folder, send](docs/screenshots/send.png)

  Every volume of the Amiga is a chip, folders open with a click, and you can
  also type the path of an existing folder, such as `Work:Games`, into the field.
- **text.** *Type text* sends a whole block, Ctrl+V sends your clipboard.
- **the rest.** Fit to window or 1:1, full screen (there the Windows keys become
  the Amiga keys), screenshot to PNG, and a restart button that writes the disks
  first.
- **the machine at a glance.** OS and CPU next to the name in the toolbar, free
  memory in the status bar. The *i* shows the rest: Kickstart and Workbench
  versions, FPU, Chip and Fast RAM, network stack, every volume with its free
  space. All of it read in well under a second, memory kept fresh every 30 s.

![About Amigent: version, system, memory and volumes](docs/screenshots/about.png)

Claude keeps working while the window is open, and you see every click it makes.

### as a Windows program

```bash
amigent-viewer 192.168.1.50 --install
```

puts **Amigent** with its own icon into the Start menu, on the desktop and into
*Installed apps*, where it can be removed like any other program. It starts
without a console, works from WSL as well as from a Windows Python, and quits
by itself when you close the window. Started twice, it just brings up the window
of the one that's running, and that includes the one Claude opened.

New version? Pull, then run `--install` again: shortcuts and the version shown in
*Installed apps* follow. `amigent-viewer --uninstall` takes it all away.

Elsewhere, or just once, `amigent-viewer 192.168.1.50` opens the window directly.

## how it works

Two small services on the Amiga, one MCP server on the PC.

```
  Claude Code --- MCP --- amigent-server ---+--- :6800 --- amigactld   commands, files
                                |           +--- :6801 --- amigentd    screen, mouse, keys
  your browser window --- :6810-+  (viewer, local only)
```

`amigentd` is ours, a tiny native daemon in C. It keeps the last frame around and
sends only the 64x64 tiles that changed, run-length packed in 256 colours. So a full
screen takes seconds even on a slow link, and looking again takes a fraction of a
second. Claude looks when it needs to look. The viewer keeps looking while it's
open, in stripes, and backs off when nothing moves, so the Amiga keeps its CPU.

`amigactld` by tbdye does the Shell and file work. Great little program, and the
reason this project didn't have to write that part.

## what Claude gets to play with

| tool | does |
|---|---|
| `amiga_exec`, `amiga_job`, `amiga_start` | run commands, long jobs and programs |
| `amiga_list`, `amiga_read_text`, `amiga_write_text` | look into directories and files |
| `amiga_put`, `amiga_get` | copy files and whole trees both ways |
| `amiga_mkdir`, `amiga_move`, `amiga_delete` | keep the place tidy |
| `amiga_screenshot` | see the screen, or zoom into part of it |
| `amiga_click`, `amiga_drag` | the mouse, including menus and icons |
| `amiga_type`, `amiga_key` | the keyboard, through your own keymap |
| `amiga_status`, `amiga_inventory` | memory, volumes, installed versions |
| `amiga_viewer` | the live window for you, see above |
| `amiga_restart`, `amiga_power` | reboot, and mains power if you wired that up |

Huge files? `python -m amigent.transfer` resumes where it stopped.

## after a system update

Big OS or AmiKit update ate your startup entries? There's a script for that.
`amigent-repair` puts back whatever is missing and leaves the rest alone, so
it's safe to run any time. Give it an icon on your desktop with
[`scripts/make_icon.py`](scripts/make_icon.py).

It also looks after whatever else you installed. Every program gets a small
module in `repair.d` that brings back the program, its settings and its menu
entry. See [`amiga/repair.d/example.repair.txt`](amiga/repair.d/example.repair.txt).

## settings

| variable | default | |
|---|---|---|
| `AMIGENT_HOST` | | address of the Amiga |
| `AMIGENT_AMIGA_DIR` | `SYS:amigent` | where the scripts live |
| `AMIGENT_FLUSH_VOLUMES` | `SYS:` | volumes to commit before a reboot |
| `AMIGENT_POWER_CMD` | | a command taking `on` or `off`, for your smart plug |
| `AMIGENT_VIEWER_PORT` | `6810` | where the viewer listens, on 127.0.0.1 only |

## one word of warning

Whoever reaches these ports owns your Amiga. Keep it on your home network, never
forward them to the internet. `amigactld` takes `ALLOW` lines in
`S:amigactld.conf` if you want it locked to your PC. `amigentd` only takes files
and commands while it runs in `SETUP` mode, and `amigent-setup` switches that off
when it's done. The viewer only listens on your own PC and wants the secret in
its address, so other machines and random web pages can't get in.

More in the [FAQ](docs/FAQ.md), the wire format is in
[docs/PROTOCOL.md](docs/PROTOCOL.md).

## building amigentd

You need vbcc and the NDK. On an AmiKit box it's all there, and
[`amiga/build-amigentd`](amiga/build-amigentd) does the job. Elsewhere, fix the
paths at the top.

```bash
python3 -m pytest -q
```

## respect

tbdye for amigactld. Volker Barthelmann for vbcc. Everybody keeping this machine
alive: the PiStorm and Emu68 crews, the AmiKit people, Aminet.

And to all Amigans out there who still believe.

## translations

Each language is one file in [`src/amigent/web/locales`](src/amigent/web/locales).
To add yours, copy `en.json`, translate the values, keep the `{placeholders}`,
and add the code to `LANGUAGES` in `viewer.js`. The tests tell you if a text is
missing.

## versions

What changed when is in the [changelog](CHANGELOG.md). Releases are tagged
`v1.0.0` and so on, `amigent-viewer --version` tells you what you have.

## license

MIT, see [LICENSE](LICENSE). `amigactld` is its own program under GPLv3. It is
fetched from its author at install time and is not part of this repository.
