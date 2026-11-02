```
                    .__                       __
  _____    _____    |__|  ____    ____   _____/  |_
  \__  \  /     \   |  | / ___\ _/ __ \ /    \   __\
   / __ \|  Y Y  \  |  |/ /_/  >\  ___/|   |  \  |
  (____  /__|_|  /  |__|\___  /  \___  >___|  /__|
       \/      \/      /_____/       \/     \/

            your amiga. claude at the keyboard.
```

Sit at your PC, tell Claude Code what you want, and it happens on your real Amiga.
It opens a Shell, moves files around, looks at the screen, clicks the requester
away, types into the tracker, puts the new game where it belongs and adds it to
the start menu. No emulator. The real thing, over your LAN.

    "check what's outdated on the Amiga"
    "the game in my Downloads folder: install it, sort it in, clean up"
    "put that demo on the desktop and run it"

code and madness by **Akustikrausch**

## what you need

Amiga side: AmigaOS 3.x, 68040 or better (PiStorm is perfect), an RTG graphics
card, a TCP/IP stack such as Roadshow, and a network connection of any kind.
Yes, a plipbox on the parallel port is enough.

PC side: Python 3.10 or newer and [Claude Code](https://claude.com/claude-code).

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

## how it works

Two small services on the Amiga, one MCP server on the PC.

```
  Claude Code --- MCP --- amigent-server ---+--- :6800 --- amigactld   commands, files
                                            +--- :6801 --- amigentd    screen, mouse, keys
```

`amigentd` is ours, a tiny native daemon in C. It keeps the last frame around and
sends only the 64x64 tiles that changed, run-length packed in 256 colours. So a full
screen takes seconds even on a slow link, and looking again takes a fraction of a
second. Nothing is streamed. Claude looks when it needs to look.

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

## one word of warning

Whoever reaches these ports owns your Amiga. Keep it on your home network, never
forward them to the internet. `amigactld` takes `ALLOW` lines in
`S:amigactld.conf` if you want it locked to your PC. `amigentd` only takes files
and commands while it runs in `SETUP` mode, and `amigent-setup` switches that off
when it's done.

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

## license

MIT, see [LICENSE](LICENSE). `amigactld` is its own program under GPLv3. It is
fetched from its author at install time and is not part of this repository.
