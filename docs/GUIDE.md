# guide

Everything the [README](../README.md) leaves out. The measurements behind the speed are in
[PERFORMANCE.md](PERFORMANCE.md).

## Requirements in detail

Amiga: AmigaOS 2.04 or newer (encryption needs OS 3.0 and AmiSSL), a TCP/IP
stack with `bsdsocket.library` such as
Roadshow, and a network connection. A plipbox on the parallel port is enough.
`amigentd` comes in builds for the 68020 and up and, as `amigentd.000`, for a
plain 68000; `amigent-setup` picks the right one. A Vampire's 68080 runs the
plain `amigentd`. An RTG graphics card is optional: chipset screens, HAM included, are
read too. A plain 68000 at 7 MHz sends a whole 640×256 Workbench screen in
about 0.6 s; a faster CPU mainly helps with large graphics-card screens.

PC: Python 3.10 or newer on Windows, macOS or Linux. The remote desktop uses Edge
or Chrome. For encrypted connections `openssl` on the PC and AmiSSL 5.1 or newer
on the Amiga.

## Manual installation

### On the Amiga

Copy [`amigentd`](../src/amigent/amiga/bin/amigentd) to the Amiga, or unpack
`amigent-1.0.0.lha` from the releases. If the Amiga has a browser or wget but
no other way in, `amigent-setup --serve` (see below) offers both builds on the
network for 30 minutes and prints the address to open. Then start it:
double-click the icon, or `amigentd SETUP` in a Shell (`amigentd.000 SETUP` on a
68000).

### Windows

Python 3.10 or newer from python.org or `winget install Python.Python.3.13`.
In a terminal (PowerShell):

```powershell
py -m pip install --user pipx; py -m pipx ensurepath
```

```powershell
pipx install amigent
```

Open a new terminal, then:

```powershell
amigent-setup
```

```powershell
amigent-viewer --install
```

Inside WSL the Linux steps below apply, and `--install` still puts Amigent into
the Windows Start menu; `--serve` then runs its server in Windows Python,
because the network does not reach servers inside WSL.

### macOS

```bash
brew install pipx && pipx ensurepath
```

```bash
pipx install amigent
```

```bash
amigent-setup
```

```bash
amigent-viewer --install
```

Allow Amigent access to the local network when macOS asks on its first start.

### Linux

Install `pipx` from the package manager, then `pipx install amigent` and
`amigent-setup`. There is no `--install`; start the remote desktop with
`amigent-viewer`.

`uv tool install amigent` works instead of pipx everywhere. To work on Amigent
itself, clone the repository and run `pip install -e .` in a virtual
environment.

### What setup does

Without an address, `amigent-setup` searches your own /24 network; with one,
for example `amigent-setup 192.168.1.50`, it goes straight there. It copies
`amigentd` (the 68000 build on a 68000) and the scripts to the Amiga, adds
`amigentd` to `S:User-Startup`, creates the key and, with `openssl` available,
the certificate. It remembers the Amiga under a name, "Amiga" unless you give
`--name A1200`.

Register the MCP server with your agent:

```bash
claude mcp add amigent -- amigent-server
```

```bash
codex mcp add amigent -- amigent-server
```

If the agent does not find `amigent-server`, give it the full path that
`pipx list` shows.

Codex asks before every tool call. To let it work on the Amiga on its own,
add to its entry in `~/.codex/config.toml`:

```toml
[mcp_servers.amigent]
default_tools_approval_mode = "approve"
```

Antigravity reads its servers from `~/.gemini/antigravity/mcp_config.json`, in
the usual `mcpServers` format. Other agents need the command `amigent-server`.
One MCP server can operate every registered Amiga. The agent calls
`amiga_machines()` to discover names, stable IDs, addresses and reachability,
then passes `machine` on each machine-specific tool call. For example:

```python
amiga_aminet_install(machine="Amiga3000", path="util/misc/example.lha", target="Work:Utilities")
amiga_aminet_install(machine="Amiga4000", path="util/misc/example.lha", target="Work:Utilities")
```

Read the package readme and check compatibility on each target first. Wrap
each machine's installation in `amiga_install_begin` and `amiga_install_end`;
run any required installer separately. Results identify the target, and each
machine keeps its own installation and undo records. A failure on one machine
does not roll back a successful installation on another.

Calls for different machines can run concurrently. Calls for the same machine
are serialized. Screen caches, connections, background log followers and
window tracking stay with their machine; there is no shared active-machine
switch. An omitted `machine` uses `AMIGENT_HOST` first, then the saved default,
then the sole registered machine. If several machines have no unique default,
pass the name or ID explicitly. Default-target calls retain their usual output and JSON format.
With several machines registered, an explicit target adds its name and address
to the reply. `amiga_everywhere` runs on all registered machines.

`AMIGENT_POWER_CMD` remains bound to the machine named by `AMIGENT_HOST` when
multiple machines are registered. It cannot switch another machine's outlet.

After updating Amigent, reconnect the MCP client so it reloads the tool schemas.
Existing separate MCP server entries with `AMIGENT_HOST` continue to work.

### When the address changes

Amigent keeps each saved Amiga under a stable internal ID in
`~/.config/amigent/amigas.json`. Renaming it preserves its credentials, notes,
installation records and inventory. Older registries migrate automatically;
the original registry and credential files are retained for recovery.

If a saved Amiga stops answering, Amigent searches the local /24 network and
accepts a new address only when its certificate or key matches. To change an
address yourself, use **Manage Amigas… → Edit address**. Duplicate addresses are flagged
and must be resolved before selecting either saved machine.

Typing an IP address into **Connect** opens a one-off connection. It does not
change a saved machine's address, even when another machine was selected.
Previous addresses are migration hints, not aliases that redirect connections.

### Updating

Easiest with the AI agent. Paste this:

    Update Amigent for me, both halves: run "pipx upgrade amigent" on this
    computer, then "amigent-setup --update" for the amigentd on my Amiga.
    Then tell me to restart you, so you get the new tools.

It updates the PC side and the Amiga, whichever is behind, and needs nobody at
the Amiga. By hand:

Update the PC side as you installed it, for example `pipx upgrade amigent`.
When a new version brings a new amigentd, bring the Amiga along by network:

```bash
amigent-setup --update
```

It needs nobody at the Amiga. With several Amigas, give the name:
`amigent-setup --update A1200`. Afterwards restart the AI app (in Claude Code
`/mcp`, or the app itself) and the remote desktop, so they load the new version.

At the Amiga it works without the PC: unpack the new archive from Aminet and
double-click *Amigent-Update* in its drawer. (Double-clicking amigentd itself
only says that one runs already.) That updates only the Amiga half: run
`pipx upgrade amigent` on the PC as well and restart the AI app. From 1.3.0 on
the PC notices a newer amigentd: the agent's next answer starts with a note,
`amiga_status` lists it under `update_pc`, and the remote desktop shows a
message.

From 1.5.0 Amigent notices a new release itself, at most once a day: the AI
agent mentions it once (`amiga_status` lists it under `amigent_update`) and
updates both halves with `amiga_update(apply=True)` once you agree; the remote
desktop shows a slim banner with *Update now*, *Later* and *Skip this version*
(the ✕); after *Later* the offer stays in the ⋯ menu and in *About*. *Update
now* closes the remote desktop, updates the PC and every saved Amiga, renews the
Start menu entry and opens the remote desktop again; what happened is in
`update.log` next to `amigas.json`. Both work for pipx and pip installations; a
source checkout is told to `git pull`. The check asks PyPI for the list of
releases and the GitHub release for its notes, nothing else: no name, no Amiga,
no identifier goes out. `python -m amigent.update off` (or
`AMIGENT_NO_UPDATE_CHECK=1`) switches it off, `python -m amigent.update check`
asks at once.

`amigent-setup --update` also renews `amigent-repair` and its helpers in the
Amigent folder (from 1.5.0). With `--spare FOLDER` it keeps a second Amigent
folder on another disk, with the scripts, amigentd, the key, the certificate and
the repair modules, to repair from when the first disk is gone; the Amiga
remembers the folder, so later updates renew it without the option.

`amigent-setup --update` never puts an older amigentd over a newer one: with the
same version number the date in its version string decides, and a build of your
own with the same number stays. `--force` replaces it anyway, for instance to go
back to the released build.

Either way key, certificate and the start line in `S:User-Startup` (`TLSONLY`
included) stay, and so does the copy for `amigent-repair` in the scripts folder,
which gets the new amigentd too. Should the new amigentd not start, the Amiga
starts the old one again.

## Tools for the AI agent

| tool | purpose |
|---|---|
| `amiga_exec`, `amiga_job`, `amiga_start` | run commands, long jobs and programs |
| `amiga_jobs`, `amiga_job_output` | the background jobs with number and return code; what a job printed so far |
| `amiga_list`, `amiga_read_text`, `amiga_write_text` | read directories and files |
| `amiga_put`, `amiga_get` | copy files and directory trees in both directions |
| `amiga_mkdir`, `amiga_move`, `amiga_delete` | manage files |
| `amiga_screen_text` | the text on the screen, window by window, with positions; far cheaper than a picture |
| `amiga_click_text` | click on visible text, such as a button label |
| `amiga_screenshot` | look at the screen or a part of it, 256 colours; or only what changed |
| `amiga_screenshot_color` | a region in full colour, for small text and exact colours |
| `amiga_wait_change` | wait until something on the screen changes, without sending pictures |
| `amiga_wait_window`, `amiga_wait_text` | wait on the Amiga until a window or a text appears or goes |
| `amiga_wait_task` | wait until a task or program runs, or until it has ended |
| `amiga_do` | several clicks, keys, waits and commands in one call, then one look |
| `amiga_pause_screen` | stop all screen reading for a while, so a test or benchmark has the CPU |
| `amiga_screens`, `amiga_screen_front` | list all screens and which one the chipset output shows, bring one to the front; `amiga_screenshot(screen=...)` pictures one behind without moving it |
| `amiga_ui_tree`, `amiga_ui_click` | windows and gadgets as text, click a gadget by its label or number |
| `amiga_menus`, `amiga_menu_select` | menus as text, pick an item by name |
| `amiga_click`, `amiga_drag`, `amiga_mouse_move`, `amiga_button` | mouse, including menus and icons |
| `amiga_type`, `amiga_key` | keyboard, through the Amiga's keymap |
| `amiga_mouse_relative` | mouse movement for games that ignore the pointer position |
| `amiga_arexx`, `amiga_rexx_ports` | ARexx commands to any program with an ARexx port |
| `amiga_break` | Ctrl-C (or D, E, F) for a running command or a job by its number |
| `amiga_status`, `amiga_inventory` | memory, volumes, installed programs and versions, the CPU and FPU ("68040 with FPU (68040.library loaded)"); after a crash the guru in words and what happened before |
| `amiga_update` | is a newer Amigent out, and what is new; with `apply=True` (after the user agrees) updates this PC and every saved Amiga |
| `amiga_loaded` | libraries, devices, resources, running programs and resident modules in memory, with versions |
| `amiga_where`, `amiga_memory` | name an address (program and hunk, library function, ROM module); read memory as hex, text or 68k code; `prog hunk 4 +$10` (or `task=`, `hunk=`, `offset=`, kept inside the hunk) and the vectors below $400 (ExecBase at 4) work too |
| `amiga_tasks`, `amiga_task` | all tasks with priority, state and stack, programs that run twice marked; change a priority, send Ctrl-C to F; `stack=N` names what a task waits for and its return addresses |
| `amiga_locks` | who holds the layer locks of a screen and its windows, and who waits |
| `amiga_cpu` | CPU load per task, sampled on the Amiga itself; under Emu68 also the real MIPS |
| `amiga_emu68`, `amiga_emu68_jit` | under Emu68 (PiStorm and other Pi boards): Emu68 version, the Pi, boot options, temperature, under-voltage and throttling, JIT counters; JIT settings while it runs, taken back with one call, per program, see [EMU68.md](EMU68.md) |
| `amiga_graphics`, `amiga_screen_modes` | the graphics board: Picasso96 or CyberGraphX, card and chip driver with versions, the Zorro bus, Warp3D and MiniGL; the Picasso96 screen modes with the colour depths switched on, one switched on or off after a copy (`confirm=True`), undone in one call |
| `amiga_emu68_card` | Emu68's boot partition on the PiStorm's SD card, read only: kernel and version, board variant, Kickstart, `cmdline.txt` options explained, free space, problems; `check=True` reads every file |
| `amiga_emu68_option`, `amiga_emu68_kickstart`, `amiga_emu68_undo` | change one `cmdline.txt` option or the Kickstart file, and take it back; dry run until `confirm=True`, backup on the card and the PC first, see [Emu68's SD card](#emu68s-sd-card) |
| `amiga_emu68_update` | update Emu68 from a release zip on the PC, after checking version, board and VideoCore.card, with a backup of the whole boot partition |
| `amiga_amigus_status`, `amiga_audio_levels` | an AmiGUS card: which driver holds PCM, wavetable and codec, without reserving anything; its left and right output level over a few seconds |
| `amiga_drag_measure` | drag a window by its title bar and time every step: median, p95, max, steps it did not follow |
| `amiga_quiet_monitor` | amigent-monitor stops drawing for a while, so it does not spoil a measurement |
| `amiga_software_failure` | a Software Failure requester: its text, the guru in words (for a line-F guru whether the FPU or its library is missing), and Suspend (never Reboot); a WarpOS or PowerUP crash window in words, and closing it |
| `amiga_ppc` | a PowerPC board, read only: the card, the 68k CPU beside it, WarpOS or PowerUP with versions, the flash ROM, 68040/68060.library, patches at boot, known conflicts; the PowerPC tasks with their load; the crash window |
| `amiga_trace` | tracepoints: the registers of a program when it gets to an address, without stopping it |
| `amiga_memory_write` | patch RAM, only when amigentd runs with `ALLOWPOKE` |
| `amiga_hrtmon` | load HRTmon (Aminet dev/debug/HRTmon) for the user at the Amiga; entering it halts the system, so the agent never does |
| `amiga_gurus` | guru codes in words, the last one and those MCP logged; a line-F guru against this Amiga's FPU |
| `amiga_state` | before and after: memory, open libraries, tasks and ports, to find leaks |
| `amiga_screen_diff` | compare the screen with a saved picture: share changed, boxes, a picture of it |
| `amiga_create_project`, `amiga_build` | a new C program with a vbcc build script, compiled on the Amiga; `fpu="auto"` builds FPU code for the FPU this Amiga has, `target="warpos"` a PowerPC program for WarpOS |
| `amiga_everywhere` | one command on every Amiga Amigent knows, the results compared |
| `amiga_emulator` | for an Amiga in WinUAE 6.1: start and quit it, a disk in DF0:, pause, registers, memory, the custom chips (Paula, copper, sprites), a whole-machine save before installing |
| `amiga_pistorm` | the classic PiStorm emulator under Linux on the Pi, over SSH: its configuration (with undo), restart, log, the Pi's temperature and power |
| `amiga_link` | whether data travel packed, the measured speeds, and setting it by hand |
| `amiga_install_begin`, `amiga_install_end`, `amiga_install_undo`, `amiga_installs` | installations that can be taken back |
| `amiga_aminet_search`, `amiga_aminet_readme`, `amiga_aminet_install` | find and install software from Aminet |
| `amiga_run`, `amiga_debug_log` | run a program under test, with its kprintf and Enforcer output; FPU code is not started on an Amiga without an FPU |
| `amiga_follow` | copy a growing log or the debug output to the PC while it grows, so it survives a hang |
| `amiga_pi_network` | on a PiStorm with Emu68: the Pi's Ethernet or Wi-Fi as a second network interface, switched on probation (see below) |
| `amiga_serial_log` | record the Pi's serial debug output on the PC through a USB serial adapter (see below) |
| `amiga_clipboard` | read or set the Amiga's clipboard |
| `amiga_notes`, `amiga_note` | what the agent learned about programs on this Amiga, for next time |
| `amiga_viewer` | open the remote desktop |
| `amiga_restart`, `amiga_power` | restart, and mains power if a smart plug is set up; both ask first while a program without the OS runs |
| `amiga_capture` | a USB HDMI capture device for the Amiga's own video output (see [Programs without the OS](#programs-without-the-os)) |
| `amiga_find` | find the Amiga again after its address changed, or set its address |

Gadgets without a readable label, such as GadTools buttons or MUI gadgets, come
with a small picture of each one and a number, so the agent can still click the
right one.

Text or picture: for information (a Shell's output, a requester's message, a
list) the agent reads the text amigentd records, a few hundred bytes. For
layout, graphics, icons and games, or when the text says something is missing,
it takes a picture, best of just that part. The remote desktop always shows
the picture.

Packing: when the Amiga packs much faster than its network carries (a PiStorm
or a 68080 behind a plipbox, say), replies, files and screen tiles travel
packed. Amigent measures both and decides; `amiga_link("off")` or
`AMIGENT_COMPRESS=off` switches it off, `fast` or `best` on. For large files, `python -m amigent.transfer` can resume an
interrupted copy.

Jobs: `amiga_job` gives every background job a number. Several can run at
once; `amiga_jobs` lists them with their return codes, `amiga_job_output`
reads what one printed so far (with amigentd 1.4.0 also while it runs; the
Shell passes output on in blocks of a few KB) and `amiga_break(job=n)` stops it.

Installations that can be taken back: `amiga_install_begin(name, folders=...)`
records the folders an installation may touch and copies aside, on the Amiga
itself, what undo needs. The folders named in `folders` are copied whole. The
system folders (C:, LIBS:, DEVS:, S:, L:, ENVARC:, SYS:WBStartup,
SYS:Classes) are only recorded with name, size and date, which takes one
listing instead of copying thousands of files. Of them, only these are
copied: S:Startup-Sequence and S:User-Startup, the files named in `touch`
(for example `touch="LIBS:foo.library"`), and files that
`amiga_aminet_install` or `amiga_put` are about to overwrite while the
installation is open. `full=True` copies the system folders whole, as
before 1.5.0.

A record shows that a file changed, but it can't bring back the old content.
So `amiga_install_end` names every system file that changed or was deleted
without a copy, and `amiga_install_undo` removes what came, restores what it
has a copy of, and names the files it could not restore. When the installer
is known to write system files, name them in `touch`.

Before anything is copied, the answer gives an estimate: files, bytes, and
time, which gets more accurate after each backup on that Amiga. It also checks
the free space on the drive that holds the copies. Above 2000 files, 32 MB
or 5 minutes, nothing is copied until the call is repeated with
`confirm=True`; `estimate_only=True` only gives the estimate. The copying runs
as a background job at low priority, which can be followed with
`amiga_job_output` and stopped with `amiga_break(job=n)`. The job writes a
marker as its last step, only when every copy went well; a backup without
the marker is never used, and `amiga_install_end` refuses until it is there.
Records and copies from earlier versions still work with undo.

After a crash: amigentd reports exec's last alert, and Amigent puts it in
words: `8100000C` is "exec.library: the memory list is insane; dead end". The
PC keeps the agent's last calls for each Amiga, so `amiga_status` also says
what was done and which programs ran just before the Amiga went away.

Memory and code: `amiga_loaded` shows what really runs, such as the AHI
driver in memory rather than the files on disk. `amiga_where` names an
address, for example a guru's or one from a debugging tool: program, hunk and
offset; library function through the library's jump table; or ROM module.
`amiga_memory` reads up to 64 KB of RAM or Kickstart ROM, never custom chips
or boards, as hex, text or 68k disassembly with the names of library calls
(`jsr -132(a6)` becomes `exec Forbid`). An address can be given as
`dos.library/Write`.

Emulators: WinUAE 6.1 has a GDB server. With `"gdb": "127.0.0.1:2345"` in the
Amiga's entry in `amigas.json`, and WinUAE started with
`-s debugging_features=gdbserver -s gdb_port=2345 -s cpu_threaded=false`,
`amiga_emulator` pauses and resumes the emulated CPU, reads registers and memory
even when the Amiga hangs, and saves or restores the whole machine. Then
`amiga_install_begin` also saves a state file, an undo of everything but the
disk changes.

Classic PiStorm: the original PiStorm software runs as the program `emulator`
under Linux on the Raspberry Pi, with `.cfg` files such as `default.cfg`.
`amigent-fleet --pi NAME pi@address` keeps the Pi's address with the Amiga's
entry (`"pi"` in `amigas.json`). `amiga_pistorm` then logs in with the system's
`ssh` and your own key: `BatchMode=yes`, your `known_hosts`, no passwords
stored. Log in once yourself first (`ssh pi@address`) so the host key is
known; a port or key file goes into `~/.ssh/config`. It tells whether the
emulator runs from a systemd unit or was started by hand or a script, which
`.cfg` it reads, and the Pi's temperature and under-voltage state
(`vcgencmd`). It shows the `.cfg`, changes it after showing the difference
(a copy stays next to it on the Pi, and `undo` puts it back), restarts the
emulator the way it was started, and reads its log (the unit's journal, or
the file its output goes to). Nothing changes without `confirm=True`;
restarting and files owned by root need `sudo` without a password on the
Pi. Not for Emu68, which has no Linux.

Debugging a program: `amiga_create_project` writes `main.c` and a vbcc build
script, `amiga_build` compiles it on the Amiga, `amiga_run` runs it with its
kprintf and Enforcer output. Addresses in that output get names: a hit at
`PC: 0A12B4C0` says `myprog hunk 0 +$3D4`. `amiga_trace("set", address)` puts a
tracepoint there: when the program passes, its registers and the top of its
stack are kept and it runs on. `amiga_state` before and after a run lists what
it left: memory, open libraries, tasks, ports. `amiga_tasks` shows how much
stack each task uses, with a warning near the end. `amiga_screen_diff`
compares the screen with a saved picture, for tests of how a program looks.
`amiga_everywhere` runs one command on all known Amigas and compares.

The FPU: Amigent reads it from exec's AttnFlags, which amigentd reports, and
on a 68040 or 68060 also whether `68040.library` or `68060.library` is loaded;
the chip's FPU lacks some instructions (FSIN, FETOX and others) that the
library emulates. Emu68 on a PiStorm (told by its `devicetree.resource`) runs
all of them itself, so there it says "68040 with FPU (Emu68)", and with Emu68's
`nofpu` option no FPU. Without AttnFlags (an older amigentd) it falls back to what
`C:CPU` prints. A line-F guru is then explained: no FPU means the program needs
one; an FPU without its library means the library is missing. `amiga_build`
builds without FPU code by default, so the program runs on every Amiga;
`fpu="auto"` uses vbcc's `-fpu=68881` with `m881.lib` for a 68881/68882 and
`-fpu=68040` with `m040.lib` for a 68040/060, and is refused on an Amiga
without an FPU. Projects created before 1.5.0 need a new build script for it.
Before `amiga_run` starts a program on an Amiga without an FPU, or on a
68040/060 without its library, it looks through the program's code hunks for
FPU instructions; on an Amiga without an FPU it does not start one that has
them (`check_fpu=False` runs it anyway). This is a heuristic: a program may
test for an FPU before using it.

### The Pi's network on a PiStorm

A PiStorm Amiga often reaches the network through a slow adapter at the
parallel or serial port, while the Raspberry Pi behind it has Ethernet or
Wi-Fi. `amiga_pi_network()` only reads and tells what is possible: whether
Emu68 runs, which drivers are in `DEVS:Networks`, which TCP/IP stack is
installed, the interfaces in `DEVS:NetInterfaces` and the speed today.

| driver | for | comes from |
|---|---|---|
| `genet.device` | the Ethernet port of a Pi 4 or CM4 | the emu68-genet project; it needs a recent Emu68. The Pi 3's Ethernet sits on its USB hub and has no Amiga driver |
| `wifipi.device` | the Wi-Fi of a Pi 3, Pi 4 or Zero 2 W | Emu68-tools; many PiStorm distributions have it already |

Emu68 does not tell AmigaOS which Pi it runs on, so the report names what
each driver needs instead.

`amiga_pi_network("setup", "ethernet", confirm=True)` (or `"wifi"`) sets the
Pi's network up as a second Roadshow interface, `pieth` or `piwifi`, with
DHCP. The existing interface is not changed. The steps:

1. The interface file goes to `net/` in the Amigent folder on the Amiga,
   not yet to `DEVS:NetInterfaces`.
2. A probation script starts with `Run`. After the probation time (90 s by
   default, `probation=`) it takes the new interface down
   (`ConfigureNetInterface pieth STATE=DOWN`) unless the PC has confirmed.
   It needs no network, and since nothing in `DEVS:NetInterfaces` changed
   yet, a restart forgets the interface as well.
3. `AddNetInterface` brings the interface up. The PC waits for its address
   and connects there with the Amiga's key or certificate. If that works it
   confirms, measures the speed, copies the file to `DEVS:NetInterfaces`,
   writes a repair module and moves Amigent's entry for the Amiga to the new
   address.

`amiga_pi_network("undo")` takes the interface down, removes only the file
Amigent added, and moves the entry back. Amigent sets up only Roadshow; for
AmiTCP or Miami the report says so. Wi-Fi names and passwords are entered by
the user on the Amiga, where `wifipi.device` expects them; they never pass
through Amigent. The Pi's boot partition (`config.txt`, `cmdline.txt`) is
never read or written.

### The Pi's serial debug output

When an Amiga with Emu68 freezes hard, the Amiga cannot tell anything any
more, but Emu68 still writes its messages to a serial line of the Pi. A USB
serial adapter on the PC can record them:

```
pipx inject amigent pyserial        # or: pip install "amigent[serial]"
amigent-serial --list               # the serial ports this PC has
amigent-serial COM5                 # or /dev/ttyUSB0; Ctrl+C stops
```

`amiga_serial_log("start")` does the same inside the MCP server, and
`"stop"`, `"status"` and `"show"` go with it. Each line gets the PC's time and
goes to `serial/<id>.log` in the config folder. Nothing is ever sent on the
line. `amiga_debug_log` adds what came since its last call, also while the
Amiga does not answer. When the Amiga does not answer, `amiga_status` gives
the last lines, and after a restart it gives the lines from before it.

Wiring, from the PiStorm documentation:

- Use a 3.3 V TTL adapter only. A 5 V adapter or a real RS-232 port (±12 V)
  damages the Pi. Connect GND, and the Pi's data line to the adapter's RX.
  The adapter's TX stays unconnected; Amigent only listens.
- PiStorm32-lite: pin 0 of the debug connector (marked with a square) carries
  the data; GND is on the same connector.
- Classic PiStorm: the line is not on a connector; the data is on GPIO 26
  (pin 37 of the Pi's 40-pin header). It is not GPIO 14/15, the Pi's usual
  UART.
- Speed 921600 baud (Emu68 says 921.6 kBit), 8N1. FTDI adapters are known to
  work at this speed. Emu68's `fast_serial` mode is synchronous and needs an
  FT232H with its own tool; Amigent does not read it.

Emu68 writes JIT messages only with `debug` (and `disassemble`) in
`cmdline.txt` on the Pi's boot partition. Amigent does not write that file;
the user adds the words there. At boot this output can go past 200 MB, so
Emu68 can limit it at run time to a range of 68k addresses through its
control registers DBGCTRL, DBGADDRLO and DBGADDRHI.
`amiga_serial_log("narrow", low="$00F80000", high="$00FFFFFF", verbose=1,
confirm=True)` sets them with a small program written to `RAM:`, and
`"wide"` sets verbosity 0 for every address again. Only code translated
after that follows the new setting; the program clears the JIT cache for
this. Amigent runs it only when Emu68's `devicetree.resource` is loaded,
because on a real 68k CPU these instructions would crash.

Graphics boards: `amiga_graphics` reads what is loaded (`rtg.library`,
`cybergraphics.library`, the `.card` and `.chip` drivers, Warp3D and MiniGL),
the graphics boards in expansion.library's list, and `Version FILE` of
`LIBS:Warp3D.library` and `LIBS:minigl.library`. `amiga_screen_modes` reads
`DEVS:Picasso96Settings` and shows each resolution with its depths;
`need="640x480x32"` says why a program does not get a mode. Switching depths
on or off (`action="on"` or `"off"`, `confirm=True`) first copies the file to
`DEVS:Picasso96Settings.amigent-undo` (the first state also to
`.amigent-orig`); `action="undo", confirm=True` puts it back, a second undo
redoes. Picasso96 reads the file when it starts, so a change counts after a
restart. With CyberGraphX the board is only recognised; modes are managed for
Picasso96. On a card in a Zorro II slot every look at the screen reads the
card over the slow bus, so the remote desktop asks for smaller pieces and looks
less often there, and `amiga_screenshot` suggests a region; a ZZ9000 keeps its
own encoder, and PiStorm, UAE and CPU-slot cards stay as they were. The ZZ9000, Picasso II and IV,
CyberVision 64 and 64/3D, CyberVisionPPC, BlizzardVisionPPC, Merlin, Piccolo,
Spectrum, Retina, Domino, Vampire SAGA, Mediator cards and CyberGraphX are not
tested with this: their names come from the Zorro ID table and the driver
names, and `amiga_graphics` says so.

## Emu68's SD card

On a PiStorm, Emu68 starts from the FAT partition of the Pi's SD card:
`config.txt` names the Emu68 file (`kernel=`) and the Kickstart
(`initramfs kick.rom`), `cmdline.txt` holds Emu68's options. A mistake there
and the Amiga does not start, and nothing on the Amiga can fix it. The
`amiga_emu68_*` tools therefore go carefully:

- `amiga_emu68_card` only reads. It finds the partition among the mounted
  volumes (or takes `volume="BOOT:"`), so the partition has to be mounted on
  the Amiga with a FAT file system. To write to it from the Amiga the card
  driver needs `sd.unit0=rw` (`emmc.unit0=rw` on a Pi 4); that makes the whole
  card writable, partition table included, so set it only while needed.
- Nothing is written unless the user has created a file `amigent-write-ok.txt`
  in the boot partition. Amigent never creates it.
- Every writing call shows its plan and writes nothing until it is called
  again with `confirm=True`.
- Before writing, the files it replaces are copied to `amigent-backup/<id>/`
  on the card and to the settings folder on the PC (`emu68/<machine>/<id>/`),
  each with a `manifest.json` of sizes and checksums, and checked. If that
  fails, nothing is written.
- New files are written next to the old ones as `<name>.amigent-new`, read
  back, and only then swapped in and read back again. On a failure the old
  file goes back. Only the files the plan names are touched; never
  `cmdline.txt` or the Kickstart during an update, never FPGA files, never the
  Amiga partitions.
- An update first checks the Emu68 version on the card against the new one,
  the board variant (the file names changed with Emu68 1.1: `Emu68-pistorm`
  was the CPLD PiStorm in 1.0 and is the PiStorm32-lite in 1.1), and the
  version of `LIBS:Picasso96/VideoCore.card`: Emu68 1.1 needs 1.5, which does
  not work with 1.0. Then it backs up the whole boot partition. After the
  restart, `amiga_emu68_update(after_restart=True)` checks that the new Emu68
  runs, by the versions of the drivers it brings in its ROM. Emu68 1.1 takes
  most options from overlays in `config.txt`; the update names the
  `cmdline.txt` options this concerns.
- When PiStormBoot is installed, use its boot menu for changes by hand;
  Amigent leaves its files alone.

**Try changes on a second SD card with a copy of the system first.**

### Rescue from the PC

If the Amiga no longer starts after a change, a power cycle does not help: the
Pi always starts what is on the card. Then:

1. Switch the Amiga off, take the SD card out of the Pi and put it into a card
   reader on the PC. The boot partition appears as a drive (Windows), under
   `/Volumes` (macOS) or under `/media` (Linux).
2. With Amigent on the PC, list the backups and check one:

   ```
   python -m amigent.emu68card list
   python -m amigent.emu68card verify <backup folder>
   ```

3. Show what a restore would write, then write it:

   ```
   python -m amigent.emu68card restore <backup folder> <card drive>
   python -m amigent.emu68card restore <backup folder> <card drive> --yes
   ```

   The backup folder can also be the one on the card,
   `<card drive>/amigent-backup/<id>`. Files the change added stay on the card;
   they do no harm.
4. Without Amigent: copy everything inside `amigent-backup/<id>/files/` on the
   card to the top of the card, replacing the files there. The `README.txt` in
   that folder says the same.
5. Eject the card safely, put it back into the Pi and switch on.

Never format or repartition the card for this; only files are copied.

PowerPC boards: `amiga_ppc` reads a CyberStorm PPC or Blizzard PPC from
expansion.library's board list (phase5, manufacturer 8512), tells which kernel
is in memory (WarpOS `powerpc.library` or PowerUP `ppc.library`, and the
bridges between them: ppclibemu, `powerpc.library` V7), the flash ROM's version
from its SCSI driver (`cybppc.device`, `blizzppc.device`), the 68040/68060
libraries, PPC patches in the startup scripts (BPPCFix, BK_RemPPCLib,
InitPPCLib, BPPCFixFix, CSPPC233Fix) and combinations known not to work, such
as both kernels at once. It only reads: nothing is opened or loaded that was
not active, and the flash ROM is never written. With WarpOS running it also
runs WarpOS's own tools: `showinfo` for the PowerPC and its clock, `stat` for
the PowerPC tasks and their load, which `amiga_tasks` then lists below the 68k
tasks. They are found along the Shell's path, or in the folder that
`AMIGENT_WARPOS_TOOLS` or `amiga_ppc(tools=...)` names. A WarpOS crash window
(`ENV:PowerPC/crashfile`) is read by `amiga_software_failure` and `amiga_run`:
the exception in words, the task, SRR0, LR and DAR named like a 68k crash
address, and the stack pointer for Aminet's checkstack. `amiga_build(folder,
target="warpos")` builds with vbcc's `+warpos` configuration; vbcc needs its
ppc-warpos target (Aminet `dev/c/vbcc_target_ppc-warpos`) on the Amiga.
PowerUP programs are not built yet. None of this has been tried on a real
PowerPC board; the board numbers, tool output and crash text follow the
documentation and WarpOS 4.0, and reports from owners are welcome.

## Remote desktop

- **Screen.** Only the 64x64 pixel tiles that changed are transferred, the area
  around the mouse pointer first. Chipset screens come in their own colours,
  graphics-card screens with 256. Your mouse and keys always go before the
  picture, so the Amiga reacts at once even while the screen is catching up.
- **Screens.** With two or more open screens, a list in the middle of the toolbar
  shows them and brings the one you pick to the front; it also has *Workbench to
  front* and *Next screen*.
- **Which output.** With a graphics card, a chipset screen in front (a game, a
  tracker, SysInfo) moves the picture to the Amiga's own video output; on a
  PiStorm that is the RGB port, not the Pi's HDMI. The window says so once and
  keeps a small *On the Amiga's video output* note with the screen mode while it
  lasts. The AI agent hears the same in `amiga_screenshot`, `amiga_screens` and
  `amiga_status`.
- **Mouse and keyboard.** Click, drag, right button for menus, mouse wheel.
  Characters are typed through the Amiga's keymap, so umlauts and AltGr
  characters arrive correctly. *Game mode* (tab *Mouse & games*) captures the
  mouse and sends its movement instead of a position, and sends keys by position,
  held as long as you hold them; Esc leaves it. *Advanced* has the two halves as
  separate switches.
- **Panel.** Four tabs: *Keys* (Esc, Tab, Del, Help, Return, F1 to F10, cursor
  keys, and both Amiga keys, Ctrl, Alt and Shift for the next key with one click
  or locked with a double click), *Programs* (menu shortcuts such as Open, Save
  and Quit, and the clipboard), *Mouse & games* and *Shell* (Ctrl-C, Ctrl-D,
  Ctrl-\). In a narrow window the panel becomes a sheet at the bottom.
  *Keyboard shortcuts* in the ⋯ menu lists what the PC keyboard does.
- **Files.** *Send file* in the toolbar, or drag files onto the screen. You
  choose the target folder on the Amiga; the transfer is checked with a checksum
  and asks before replacing a file. *Get file* lists the Amiga's folders with
  their files; select one and *Get* saves it in the PC's downloads.
- **Clipboard.** In the *Programs* tab and the ⋯ menu: *Amiga → PC* copies the Amiga's clipboard to the PC, *PC →
  Amiga* puts the PC's clipboard on the Amiga's, to paste there with Right
  Amiga+V. Text only.
- **Text.** *Type text* sends a block of text; Ctrl+V sends the PC's clipboard.
- **View.** Fit to window or 1:1, full screen (where the Windows or Cmd keys act
  as the Amiga keys), screenshot as PNG, and *Restart Amiga…* in the ⋯ menu,
  which writes pending data to disk first. Light and dark appearance follow the
  system or are set in the ⋯ menu. The status bar shows screen size, link speed,
  ping and free memory; a click opens *About → Connection*.
- **Pause the picture.** *Pause the picture* in the ⋯ menu stops the Amiga
  reading its screen for 1, 5 or 15 minutes, so a test, a benchmark or a demo
  has the CPU and the chip bus to itself. The window keeps the last picture and
  shows the time left; mouse, keys and files keep working. *Resume* ends it
  early. The pause holds for every client: the AI agent's screenshots wait as
  well. On the Amiga, a script can do the same through ARexx:

  ```rexx
  address AMIGENT
  'PAUSE 300'   /* seconds; without a number it tells the seconds left */
  /* ... the test ... */
  'RESUME'
  ```

- **Scripts that type.** amigentd's ARexx port also brings a window to the
  front and types into it, through the Amiga's own keymap:

  ```rexx
  address AMIGENT
  'ACTIVATEWINDOW "Shell*"'   /* rc 5 when no window title matches */
  'TYPE "dir RAM:<return>"'   /* keys in angle brackets: <ctrl c>, <lamiga q> */
  'KEY "esc"'
  'BUSY'                      /* "menu" or "held" while a menu or popup holds Intuition up */
  ```

- **System information.** *About Amigent* in the More menu (⋯), tab *This
  Amiga*, shows Kickstart and Workbench versions, CPU, FPU, the connected
  amigentd, memory, the network stack and the free space of each volume.

![Choosing the target folder on the Amiga](screenshots/send.png)

![System information on the This Amiga tab of the About window](screenshots/about.png)

`amigent-viewer --install` adds Amigent to the Start menu, the desktop and
*Installed apps* on Windows (from WSL or a Windows Python), or puts `Amigent.app`
into `~/Applications` on macOS. Starting it a second time brings the open window
to the front. After an update, run `--install` again. `amigent-viewer
--uninstall` removes everything.

On macOS the app is signed with a Developer ID and notarized, the same app for
every Mac; what differs per Mac is in `~/.config/amigent/app.json`. The first
start asks for access to the local network, which it needs to reach the Amiga.
`scripts/build-mac-app.sh` builds, signs and notarizes it again; without the
signed app, `--install` builds an unsigned one with the Xcode tools.

## Programs without the OS

WHDLoad installs and most demos switch the operating system off. From then on
amigentd and the network are gone until the program quits, which to the PC
looks like a crash. When the AI agent starts WHDLoad or a `.slave`, Amigent
keeps a note of it:

- tools that lose the connection say that the program took the OS along, which
  key quits it (read from the slave, otherwise usually F10) and that it has to
  be pressed on the Amiga's own keyboard;
- `amiga_restart` and `amiga_power('off')` ask first instead of cutting the
  power of an Amiga that is busy playing; `force=True` goes ahead;
- the remote desktop shows what runs and since when, and connects again by
  itself once amigentd is back.

A program started from the Amiga itself, say from iGame, can't be told this
way; the connection is just lost.

### A capture device

To see such programs, the Amiga's own video output has to reach the PC. A USB
HDMI capture stick does that, behind an RGB-to-HDMI converter on a classic
Amiga. Amigent reads it with [ffmpeg](https://ffmpeg.org), which has to be
installed (Windows `winget install ffmpeg`, macOS `brew install ffmpeg`, Linux
the package manager).

Choose the device in the remote desktop under ⋯ → *Capture device…*, or let the
AI agent do it with `amiga_capture(action="devices")` and
`amiga_capture(action="set", device=...)`. The device is kept per Amiga.

- The remote desktop switches to the capture picture by itself while a program
  without the OS runs or a chipset screen is in front, with a note that this is
  the capture picture; *Back to amigentd* and ⋯ → *Show the capture picture*
  switch by hand. Mouse and keys don't go through the capture picture.
- ⋯ → *Capture picture in a corner* keeps it small in a corner next to the live
  picture, for setups where both outputs show something.
- `amiga_screenshot` and `amiga_wait_change` fall back to the capture device
  while a program without the OS runs, and say so. The agent can then see when
  a game reached its title screen or quit back to the Workbench.

Without a capture device, the agent can't see programs without the OS.

## Several Amigas

Run `amigent-viewer` to see **Your Amigas** when several machines are registered.
With one saved machine it opens that desktop directly. An explicit name, such
as `amigent-viewer "Amiga 2"`, opens that machine.

The Amiga menu at the left of the toolbar is in every desktop and in the
overview. It lists each machine with its state, opens **All Amigas at a glance**,
connects to an address, and **Manage Amigas…** provides **Rename**,
**Set default**, **Edit address** and **Remove**. Removing an entry keeps its
credentials and history on disk. The default is used by tools that need one
machine; the viewer still shows the overview when several are registered.

Each Amiga is checked every 20 seconds without taking over another viewer's
screen connection. The fleet overview also samples previews, OS, CPU and memory
while the page is open. Each Amiga needs its own credentials from `amigent-setup`.

The same controls are available from the command line:

```bash
amigent-fleet --add A1200 192.168.1.50
amigent-fleet --rename A1200 "Desk Amiga"
amigent-fleet --default "Desk Amiga"
amigent-fleet --address "Desk Amiga" 192.168.1.51
amigent-fleet --list
amigent-fleet --remove "Desk Amiga"
```

Adding a machine never replaces an existing machine's address. Use the explicit
address control to correct a saved mapping. `amigent-fleet` also opens the
overview directly.

### Saving several and choosing the default

Run `amigent-setup --name NAME` for each Amiga (with its address, for example
`amigent-setup 192.0.2.11 --name A500`), or add one that is already set up with
`amigent-fleet --add A500 192.0.2.11`. The first Amiga saved becomes the
default. Change it with `amigent-fleet --default A500` or **Manage Amigas… →
Set default**. Removing the default makes the first remaining Amiga the
default. `amiga_machines()` lists every saved Amiga with its ID and marks the
default.

### Naming one in a tool call

Every tool that works on one Amiga takes `machine`, a saved name or ID:

```python
amiga_status(machine="A500")
```

Without `machine` a tool uses `AMIGENT_HOST`, then the default, then the only
saved Amiga. With several saved and none of these (for example after editing
`amigas.json` by hand), it refuses and asks to name one. An unknown name or
address is refused too. Earlier addresses of an Amiga no longer find it; its
name, ID, current address and earlier names do.

`AMIGENT_POWER_CMD` and `AMIGENT_GDB` belong to the Amiga that `AMIGENT_HOST`
names. With several saved and no `AMIGENT_HOST`, `amiga_power` refuses, even
for the default, and `amiga_restart` does not cycle the power. For a smart
plug, add a second MCP server entry with `AMIGENT_HOST` and
`AMIGENT_POWER_CMD` for that Amiga; for WinUAE, set `"gdb"` in its entry in
`amigas.json`. `AMIGENT_KEY`, when set, is used for every Amiga, so leave it
unset with several.

### The remote desktop without an address

`amigent-viewer` without an address connects straight away only when exactly
one Amiga is saved. Otherwise, also with none saved, it opens **Your Amigas**,
whatever the default is. `amigent-viewer A500` opens that Amiga. An address,
such as `amigent-viewer 192.0.2.10`, opens a one-off connection, even when it
is a saved Amiga's address: it is not searched for when the Amiga moves. Use the
name in shortcuts and in `AMIGENT_HOST`.

### Files per Amiga

Everything lives in `~/.config/amigent/` (or `$XDG_CONFIG_HOME/amigent/`).
`amigas.json` holds the saved Amigas, each with an ID such as
`amiga-0123456789abcdef0123456789abcdef`. Key and certificate are
`key-<id>` and `tls-<id>.pem`; notes, installation records, the inventory, the
journal, `amiga_follow`'s copies (`follow/<id>/`) and the serial log
(`serial/<id>.log`) are filed under the same
ID. The ID is made on each PC, so the same Amiga has a different ID on another
PC.

`amigas.json` is checked when it is read. A damaged file is reported with its
path and what is wrong, and the tools refuse until it is mended; it is not
treated as empty.

Updating from 1.4 or earlier copies the files named after an Amiga's name or
address (`key-A1200`, `tls-192.0.2.10.pem`) to the new names. The old files
stay where they were, and the registry as it was is kept as
`amigas.pre-id.json`. Files whose name two Amigas could claim, such as an
address both had, are not copied; save the right one as `tls-<id>.pem` by hand.
The common `key` serves only an address that is not saved; on migration it is
copied to the Amiga only when just one was saved.

To use an Amiga from a second PC, copy its key and certificate there as
`key-<address>` and `tls-<address>.pem` and then run
`amigent-fleet --add NAME ADDRESS`; adding copies them to the new ID.

## How it works

```
  AI agent ---- MCP ---- amigent-server ---+
                                           +--- :6801 --- amigentd   screen, mouse, keys,
  browser window --- :6810 ----------------+                         commands, files
       (remote desktop, local only)
```

`amigentd` is a small daemon in C. The screen client and up to four command
channels share one port. For the screen it keeps the last picture and sends only
the 64×64 tiles that changed. Chipset screens go out as the bitplanes the Amiga
keeps anyway, so a 68000 has almost nothing to convert; graphics-card screens go
out as tiles with their own small palette. Each tile is packed, often as the
difference to the picture the PC already has, or to the same picture scrolled.
Mouse and keys sent during a picture are carried out between its tiles. A
helper process at the lowest priority notices changes by comparing the planes
in chip RAM, so asking about an unchanged screen costs one network round trip
and the Amiga only a few percent of its CPU. It also reports
the screens, windows, gadgets and menus that Intuition knows. On a command
channel it runs Shell commands, reads and writes files and reports memory and
volumes; a long command doesn't hold up the screen.

Earlier installs used [amigactld](https://github.com/tbdye/amigactl) by tbdye
for commands and files. Amigent still talks to it when an older `amigentd` is
running; `amigent-setup --with-amigactld` installs it on request. amigactld is
a separate program under GPLv3, downloaded from its author and not part of this
repository.

The wire format is in [PROTOCOL.md](PROTOCOL.md).

## After a system update

A large OS or AmiKit update can remove startup entries. `amigent-repair` restores
what is missing and leaves everything else unchanged.
[`scripts/make_icon.py`](../scripts/make_icon.py) creates a desktop icon for it.
A small module per program in `repair.d` restores installed programs, their
settings and menu entries; see
[`src/native/amiga/examples/repair.d/example.repair.txt`](../src/native/amiga/examples/repair.d/example.repair.txt).

## Settings

| variable | default | meaning |
|---|---|---|
| `AMIGENT_HOST` | the remembered Amiga | name or address of the Amiga |
| `AMIGENT_AMIGA_DIR` | `SYS:amigent` | folder for the scripts on the Amiga |
| `AMIGENT_FLUSH_VOLUMES` | `SYS:` | volumes written to disk before a restart |
| `AMIGENT_POWER_CMD` | | command taking `on` or `off`, for a smart plug |
| `AMIGENT_VIEWER_PORT` | `6810` | port of the remote desktop, on 127.0.0.1 only |
| `AMIGENT_FLEET_PORT` | `6820` | port of the overview, on 127.0.0.1 only |
| `AMIGENT_KEY` | | key for `amigentd`, if not taken from `~/.config/amigent/` |
| `AMIGENT_TLS` | `auto` | `off` talks to `amigentd` unencrypted even with a certificate |
| `AMIGENT_REQUIRE_TLS` | off | `1`: TLS with the pinned certificate or no connection at all, see [Security](#security) |
| `AMIGENT_READ_ONLY` | off | `1`: the MCP server offers only tools that change nothing, see [Security](#security) |
| `AMIGENT_GDB` | | `host:port` of WinUAE's GDB server, when not set as `"gdb"` in `amigas.json` |
| `AMIGENT_WINUAE` | | WinUAE's program, when not set as `"winuae"` in `amigas.json` |
| `AMIGENT_NO_UPDATE_CHECK` | | `1` stops looking for new releases (also `python -m amigent.update off`) |

An Amiga's entry in `amigas.json` can also hold `"gdb"`, `"winuae"`,
`"winuae_config"` (its `.uae` file, for `amiga_emulator("launch")`), `"pi"`
(`[user@]address` of a classic PiStorm's Raspberry Pi, for `amiga_pistorm`) and
`"devpack"` (the folder with vbcc and the NDK, for `amiga_build`).

On the Amiga, `amigentd` takes `SETUP`, `KEY=file`, `PORT=number`, `QUIET`,
`TLSONLY` and `ALLOWPOKE` (writing memory) from the command line or, started from Workbench, from the tool types
of its icon. It answers ARexx on the port `AMIGENT` (`STATUS`, `CLIENTS`,
`VERSION`, `PAUSE`, `RESUME`, `ACTIVATEWINDOW`, `TYPE`, `KEY`, `BUSY`, `HELP`, `QUIT`;
see [the protocol](PROTOCOL.md#arexx)), and `amigent-status` shows the same in a small
window. `amigent-monitor` opens a window that lists what the agent does: each tool
it calls and the commands, files, clicks and keys that reach `amigentd`. Its tool
types: `NAME` (for the greeting), `WIDTH`, `HEIGHT`, `LEFT`, `TOP`, `FONT`, `SIZE`
(a fixed-width font). The window can be sized, and its zoom gadget switches
to a compact size; below 56 columns the side panel gives way to the log. Esc
or the close gadget ends it, C clears the log.

## Security

- `amigentd` asks every connection for a key. `amigent-setup` makes one per
  Amiga and stores it in `S:amigent.key` on the Amiga and in `~/.config/amigent/`
  on the PC. A wrong key costs a one-second pause and closes the connection.
- With `openssl` on the PC and AmiSSL on the Amiga, `amigentd` also accepts TLS
  on port 6802, and the PC accepts only the certificate `amigent-setup` made for
  that Amiga. Once that works, `amigent-setup` starts `amigentd` with `TLSONLY`:
  other computers then get in only encrypted (`--allow-plain` keeps the plain
  port open). Should AmiSSL or the certificate on the Amiga break, they don't
  get in at all until it is mended at the Amiga; see
  [PROTOCOL.md](PROTOCOL.md#tls) for how. Without TLS the connection is
  unencrypted, and someone who can read your network traffic could read the key
  too.
- `AMIGENT_REQUIRE_TLS=1` makes the PC side refuse anything but TLS with the
  pinned certificate: no pin, `AMIGENT_TLS=off`, a failed handshake or another
  certificate end the attempt before a byte reaches a plain port. There is no
  fallback to `amigactld`, no search of the network for a moved Amiga
  (`amiga_find` with an address still works), and a refused TLS port is not
  explained from the plain port's hello. `amiga_restart` waits on the TLS port:
  gone once it takes no connection, back once the pinned `amigentd` says hello,
  with up to 30 seconds for a slow handshake. `amiga_machines` and the overview
  count an Amiga as reachable only there.
- `AMIGENT_READ_ONLY=1` makes `amigent-server` offer only `amiga_list`,
  `amiga_screen_text`, `amiga_tasks`, `amiga_loaded`, `amiga_notes` and
  `amiga_installs`, for every saved Amiga (`machine=`). Every other tool, and
  any tool added later, is neither listed nor run, whatever the AI app's own
  tool switches say, and the server refuses to start if anything else is
  registered. Its command channel sends only the commands those tools need
  (DIR, TEXT, TASKS, LOADED, SYSINFO), it does not fall back to `amigactld`, it
  opens no screen connection (that one carries mouse and keys), and
  `amigent-monitor` gets no line per tool. `amiga_tasks` leaves out the
  PowerPC section (it runs WarpOS's `stat`), and the server does not look for
  a new Amigent release. Its instructions to the AI name only those six tools.
  `amiga_list` lists one directory at a time there, never recursively. With
  mcp 1.7 or later these tools carry the MCP hint `readOnlyHint`; hints are
  for the AI app, the server does the enforcing. These tools do show the
  Amiga's file names, screen text and tasks to the AI.
- What `AMIGENT_REQUIRE_TLS` does not cover: the link to WinUAE's GDB server
  (`amiga_emulator`, `amiga_trace` on an emulator) is plain TCP on the PC;
  `amiga_pistorm` uses ssh and `amiga_serial_log` a local serial port. The
  remote desktop, the overview and `amigent-setup` follow the variable only
  when they see it, so set it for them too when they are started on their
  own.
- Setup commands only work in `SETUP` mode, which `amigent-setup` switches off.
- The remote desktop and the overview listen only on the PC itself and need the
  token in their address.

Keep the Amiga in your home network and do not forward its ports.

## Building amigentd

vbcc and the NDK are needed; AmiKit's DevPack includes both. The AmigaDOS
script [`src/native/amiga/build-amigentd`](../src/native/amiga/build-amigentd) builds it on the Amiga
from `src/native/amiga` (SOURCE defaults to `daemon/amigentd.c`); `CPU 68000 TO amigentd.000` makes the 68000
build, `DEVPACK Work:DevPack` points it to another DevPack folder. `scripts/make_release.py` packs the
archive with LhA on the Amiga. Tests for the PC side: `python3 -m pytest -q`.

A cross compiler on the PC works too: `m68k-amigaos-gcc -O2 -m68020 -noixemul -s
-o amigentd src/native/amiga/daemon/amigentd.c` (GCC 16 from the `amigadev/crosstools:m68k-amigaos`
Docker image; `-m68000` for the 68000 build). The released binaries are vbcc's and stay in `src/amigent/amiga/bin/`.
The `amiga` workflow builds amigentd, amigent-status and amigent-monitor this way
whenever something in `src/native/amiga/` changes and keeps them as an artifact of the run.

### Apollo V4 / 68080 (source-only)

Release packages (wheels, source distributions, and Aminet `.lha` archives) ship
the classic `amigentd`, `amigentd.000`, `amigent-status`, and `amigent-monitor`
executables only. No prebuilt `amigentd.apollo`, `amigent-apollo-probe`, or
`amigent-monitor.apollo` executable is included in upstream release packages.
`amigent-setup` keeps an existing Apollo daemon in place during a normal update
when no packaged `amigentd.apollo` is present (`--force` installs classic `amigentd`).

To build the Apollo binaries (`build/apollo/amigentd.apollo`,
`build/apollo/amigent-apollo-probe`, and `build/apollo/amigent-monitor.apollo`)
from source:

- With ApolloCrossDev (`GCC-6.50-Stable`, which supports `-m68080`):
  `APOLLOCROSSDEV=/path/to/ApolloCrossDev src/native/amiga/build-amigentd-apollo`
  (add `install` to stage locally into `src/amigent/amiga/bin/`).
- With GCC 16 (`amigadev/crosstools:m68k-amigaos`, as run by the `amiga` GitHub
  Actions workflow into the `amiga-gcc` artifact): upstream GCC 16 rejects
  `-m68080`, so CI passes `CPU_FLAG=-m68020`:
  `make -f src/native/amiga/Makefile.apollo PREFIX="$CROSS_ROOT" CC=m68k-amigaos-gcc CPU_FLAG=-m68020`
  (produces 68020-targeted CI build verification binaries with `-DAMIGENT_APOLLO`;
  not hardware-tested).

**Status: untested on hardware.** AUDIO (opcode 49), the Viewer's Hear button
and `amiga_listen` have not yet been run on a physical Apollo V4-SA from this
source. What has been checked is only this:

- Compilation: the Apollo binaries build without warnings with ApolloCrossDev
  GCC 6.5 (Stable and Dev) and with GCC 16 in CI.
- Simulation: the test suite runs the daemon's AUDIO task code against a
  simulated Exec, timer and TLS pump, and the Viewer's Hear against simulated
  PCM and pages.

Neither says the Apollo part works on a V4-SA. Until it has been accepted
there, treat Apollo support as experimental.

## Translations

Each language of the remote desktop is one file in
[`src/amigent/web/locales`](../src/amigent/web/locales). Copy `en.json`,
translate the values, keep the `{placeholders}` and add the language code to
`LANGUAGES` in `ui.js`. The tests report missing texts.
