# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 1.6.0 (unreleased)

- Amigent says which video output the picture is on. With a graphics card, a
  chipset screen in front moves the picture to the Amiga's own output (on a
  PiStorm the RGB port, not HDMI): `amiga_screenshot`, `amiga_screens` and
  `amiga_status` say so with the screen mode, and the remote desktop shows a
  note while it lasts. amigentd 1.6.0 adds each screen's mode ID and name to
  SCREENS (arg `1`); an older one leaves them out. (#1)
- A program without the OS, such as a WHDLoad install, is no longer taken for a
  crash. Started by the agent, it leaves a note: a lost connection says what
  runs and which key quits it, `amiga_restart` and `amiga_power` ask first
  (`force=True` goes ahead), and the remote desktop shows it instead of
  "not answering". (#2)
- An optional USB HDMI capture device for the Amiga's own video output, read
  with ffmpeg: the remote desktop switches to it for programs without the OS
  and chipset screens, and `amiga_screenshot` and `amiga_wait_change` fall back
  to it. `amiga_capture` lists and sets the device. (#3)
- `AMIGENT_REQUIRE_TLS=1`: TLS with the pinned certificate or no connection.
  No plain dial, no `amigactld` fallback, no network sweep for a moved Amiga,
  no key on a plain line, and no plain hello read to explain a refused TLS
  port; `amiga_restart` waits on the TLS port, with time for a slow handshake,
  and `amiga_machines` and the overview check reachability there.
- `AMIGENT_READ_ONLY=1`: `amigent-server` lists and runs only `amiga_list`,
  `amiga_screen_text`, `amiga_tasks`, `amiga_loaded`, `amiga_notes` and
  `amiga_installs`, on every saved Amiga, refuses every other name, and will
  not start with anything else registered. Its channel sends only the commands
  those tools need; `amiga_tasks` leaves out the PowerPC section, which runs
  commands. There is no update check either, and the server's instructions to
  the AI name only those six tools.
  `amiga_list` lists one directory at a time there. With mcp 1.7 or later
  these six tools carry the MCP hint `readOnlyHint`; the default server gives
  no hints, as before.

## 1.5.0 (2026-11-23)

### Updates

- Amigent notices a new release by itself: at most once a day it asks PyPI
  which versions there are, in the background and with a short timeout, so an
  offline PC notices nothing. It sends nothing else.
- The AI agent hears of a new version once per session, in `amiga_status`.
  `amiga_update()` tells what is new and what an update would do;
  `amiga_update(apply=True)`, once the user agrees, runs `pipx upgrade amigent`
  and then `amigent-setup --update` for every saved Amiga, each in a process of
  its own, and says what to restart. A pip installation works the same way; a
  source checkout is told to `git pull`.
- The remote desktop shows a slim banner with the new version, *Update now*,
  *Later* and *Skip this version*; after *Later* the offer waits in the ⋯ menu
  and in *About*, with a few lines of what is new. *Update now* closes it,
  updates the PC and the Amigas, renews the Start menu entry or the app on the
  Mac, and opens the remote desktop again, which then says how it went.
- `python -m amigent.update check|on|off|apply`; `AMIGENT_NO_UPDATE_CHECK=1`
  switches the check off
  ([#15](https://github.com/akustikrausch/amigent/issues/15)).

### Remote desktop

- A new layout. The Amiga menu at the left of the toolbar shows the machine,
  its state, OS and CPU, and switches between saved Amigas, the overview, a
  one-off address and the manager; the overview has the same menu. The screen
  list sits in the middle; rarer actions moved into a ⋯ menu.
- The keyboard panel has tabs: Keys (now with cursor keys), Programs (menu
  shortcuts with their keys, clipboard), Mouse & games and Shell. The modifier
  keys also apply to the panel's own keys. In a narrow window the panel becomes
  a sheet at the bottom.
- *Game mode* captures the mouse and sends keys by position in one switch; a
  note over the picture says so, and Esc leaves it.
- *About* has tabs for Amigent, this Amiga, the connection (port, TLS,
  protocol, link speed, ping, amigentd version) and the keyboard shortcuts. The
  status bar is labelled and opens the connection details.
- *About* also names the connected daemon: which build the file it started
  from is, its target CPU, the protocol and what it can do, apart from the
  Amiga's own OS and CPU. The file is recognised by size and CRC on the Amiga,
  so nothing is sent over the link (#51).
- A dark appearance, following the system or chosen in the ⋯ menu.
- Questions such as replacing a file or restarting from the overview are
  dialogs in the page's style. *Get file* selects a file first, then gets it.
- Every text in the pages is translatable; the machine manager and the
  overview use the same language as the remote desktop.

### Repair

- `amigent-setup --update` renews `amigent-repair` and its helper scripts in
  the Amigent folder. They used to stay as the first installation left them,
  so a repair after a system update could run a script from many versions ago.
- `amigent-setup --update --spare FOLDER` keeps a second Amigent folder on
  another disk: the scripts (repairing from that folder), this amigentd, the
  key, the certificate and the repair modules. The Amiga remembers it, and
  every later update renews it too.

### Several Amigas

- Saved Amigas now keep credentials, settings and history under a stable ID.
  Migration keeps the original files. After a downgrade, older versions see
  the pre-migration history. Later writes remain under the machine ID.
- One MCP server drives every saved Amiga; each tool takes `machine` (a saved
  name or ID). Without it a tool uses `AMIGENT_HOST`, then the saved default,
  then the only saved Amiga. With several saved and none of these, the tool
  refuses and says to name one; `amigent-fleet --default NAME` or *Set default*
  in **Manage Amigas…** sets the default. (#12, #13)
- `AMIGENT_POWER_CMD` and `AMIGENT_GDB` belong to the Amiga named by
  `AMIGENT_HOST`. With several saved and no `AMIGENT_HOST`, `amiga_power`
  refuses, even for the default, and `amiga_restart` no longer cycles the
  power; put the power command into an MCP entry with `AMIGENT_HOST`, and the
  debugger address into the Amiga's `"gdb"` in `amigas.json`. (#40)
- `amigent-viewer` without an address opens **Your Amigas** unless exactly one
  Amiga is saved, also when none is (it used to stop with a message). Give a
  name to open one directly. (#40)
- Addresses an Amiga had earlier no longer find it; only its name, ID, current
  address or an earlier name do. A tool call with an unknown name or address
  is refused; an address given to `amigent-viewer` opens a one-off connection
  that is not searched for when the Amiga moves. Use the name in
  `AMIGENT_HOST` and in shortcuts. (#40)
- `amigas.json` is checked strictly. A damaged file is reported with its path
  and what is wrong, and tools refuse until it is mended, instead of acting as
  if no Amiga were saved. (#40)
- Key and certificate are read from `key-<id>` and `tls-<id>.pem` in
  `~/.config/amigent/`. The files named after the name or address, the common
  `key`, and `amigas.pre-id.json` (the registry before the IDs) stay where they
  were. The common `key` now only serves an address that is not saved; on
  migration it is copied to the Amiga only when just one was saved. (#40)
- `amiga_follow` copies into `follow/<id>/` in `~/.config/amigent/` by default,
  one folder per Amiga, instead of `follow/` itself. (#40)

### Classic PiStorm

- `amiga_pistorm` manages the original PiStorm emulator under Linux on the
  Raspberry Pi over SSH, with your own key and the system's `ssh`; Amigent
  stores no passwords. It shows how the emulator is started (systemd unit or
  by hand) and which `.cfg` it reads, shows and changes that file with a copy
  on the Pi and a one-call undo, restarts the emulator, reads its log, and
  tells the Pi's temperature and under-voltage state. Read-only unless
  `confirm=True`. `amigent-fleet --pi NAME ADDRESS` keeps the Pi with the
  Amiga's entry
  ([#25](https://github.com/akustikrausch/amigent/issues/25)).

### Installing and updating

- `amigent-setup` picks the amigentd build from a catalogue: the 68000 build
  where the 68020 one is missing, and it tells why when C:CPU gives no answer.
  `--update` never replaces a newer amigentd or a build of its own; with the
  same version the build date decides, and `--force` replaces it anyway. (#38)
- ApolloOS is told from AmigaOS on the same Vampire (exec 51 and the 68080
  bit), for an Apollo build of amigentd once there is one; until then nothing
  changes there. ApolloOS and AROS are no longer called AmigaOS 4. (#31)
- `amiga_install_begin` no longer copies the whole system by default. It
  records the system folders (name, size, date) and copies the named
  folders, S:Startup-Sequence, S:User-Startup, the files given in `touch`,
  and what `amiga_aminet_install` or `amiga_put` overwrite while the
  installation is open; `full=True` copies everything as before. First comes
  an estimate (files, bytes, time) and a check of the free space, and above a
  limit it asks for `confirm=True`. The copying runs as a job that
  `amiga_break` can stop, and a backup without its completion marker is never
  used. `amiga_install_end` and `amiga_install_undo` name the system files
  that changed without a copy. Older records still undo. (#39)

### Security

- With `TLSONLY`, amigentd now turns away plain connections from other
  computers even when its TLS port did not open, instead of falling back to
  plain. A broken AmiSSL or certificate therefore locks remote PCs out until it
  is mended at the Amiga; Amigent says which of the two it is and how to
  recover. (#27)

### Emu68

- `amiga_emu68` tells what runs on the Raspberry Pi of a PiStorm or another
  Emu68 board: Emu68's version and variant, the Pi's model and memory, the
  boot options from `cmdline.txt`, temperature, ARM clock and core voltage,
  whether the Pi reported under-voltage or throttling since power on, and the
  JIT cache and counters. It names PiStormBoot and Emu68PWM when they are there.
  `amiga_status` mentions under-voltage and throttling by itself, and
  `amiga_cpu` adds the real MIPS and JIT cache misses. Read only; on an Amiga
  without Emu68 it says so and touches nothing
  ([#16](https://github.com/akustikrausch/amigent/issues/16)).
- `amiga_emu68_jit` shows and changes the JIT settings Emu68 documents as
  changeable at run time (chip and DBF slowdown, blitter wait, CCR scan depth,
  instruction depth, inlining, loop count, soft flush), with no restart and
  nothing written to the SD card. `restore=True` puts back the values from
  before the first change, which the PC keeps. `program=` runs a program with
  settings and the Amiga puts the old ones back when it ends; `remember=True`
  keeps them as a note for the next start
  ([#17](https://github.com/akustikrausch/amigent/issues/17)).
- amigentd 1.5.0: new command EMU68 (48), `emu68` in SYSINFO, and
  `amigentd EMU68JIT reg:value:mask,...` for job scripts. See
  [docs/EMU68.md](docs/EMU68.md).

### FPU

- Amigent tells the FPU from exec's AttnFlags instead of the text of `C:CPU`:
  68881, 68882, the FPU of a 68040, 68060 or 68080, or none (such as a
  68EC020 or 68LC040). On a 68040/060 it also checks that
  `68040.library` or `68060.library` is loaded. Emu68 on a PiStorm, known by
  its `devicetree.resource`, runs every FPU instruction itself and needs no
  such library; started with `nofpu` it has no FPU. `amiga_status`, the inventory
  and the remote desktop's About show it, such as "68040 with FPU
  (68040.library loaded)" or "68LC040, no FPU".
- A line-F guru or Software Failure says whether the Amiga has no FPU (the
  program needs one) or has one without its support library. The FPU
  exceptions of the 68040/060 have names too.
- `amiga_build(fpu="auto")` builds FPU code for the FPU the Amiga has (vbcc
  `-fpu=68881` with `m881.lib`, or `-fpu=68040` with `m040.lib`) and is
  refused on an Amiga without one. The default stays without FPU code.
- `amiga_run` looks for FPU instructions in a program's code before starting
  it on an Amiga without an FPU, and does not start one that has them
  (`check_fpu=False` runs it anyway); on a 68040/060 without its library it
  warns. The check is a heuristic.
  ([#42](https://github.com/akustikrausch/amigent/issues/42))

### Emu68 on the SD card

- `amiga_emu68_card` reads Emu68's boot partition: the kernel with its Emu68
  version and board variant, the Kickstart file (size, header, checksum), FPGA
  files named in `config.txt` (shown, never written), every `cmdline.txt`
  option explained, overlays, free space and problems such as a file
  `config.txt` names that is missing; `check=True` reads every file once and
  measures the speed. It says what `sd.unit0=rw` means.
- `amiga_emu68_option` sets, changes or removes one option in `cmdline.txt`,
  only an option Emu68 knows with a valid value (`force=True` for an unknown
  one); `amiga_emu68_kickstart` boots another Kickstart after checking it, and
  the old ROM stays on the card; `amiga_emu68_undo` takes a change back
  ([#18](https://github.com/akustikrausch/amigent/issues/18)).
- `amiga_emu68_update` updates Emu68 from a release zip on the PC: it checks
  the version, the board variant and whether VideoCore.card fits (Emu68 1.1
  needs 1.5), backs up the whole boot partition on the card, with a README on
  how to go back, and on the PC, with checksums; it writes the new files next
  to the old ones and swaps them in only once all are verified, and confirms
  after the restart that the new Emu68 runs. If it does not start, the rescue
  is the card in a PC: `python -m amigent.emu68card restore <backup> <card>`
  on Windows, macOS or Linux
  ([#19](https://github.com/akustikrausch/amigent/issues/19)).
- A `config.txt` with one kernel per board, as AmiKit ships it (`[gpio24=0]`
  for PiStorm32-lite, `[gpio24=1]` for the CPLD PiStorm), is read and changed:
  the update replaces only the kernel for the board of the release, a new
  Kickstart goes into every `initramfs` line, and comment lines stay.
- Every writing call is a dry run until `confirm=True`, writes only on a card
  where the user created `amigent-write-ok.txt`, refuses when the backup
  fails, reads back what it wrote, and touches only the files its plan names.
  A second SD card with a copy of the system is the place for a first try.

### PowerPC boards

- `amiga_ppc` reads a CyberStorm PPC or Blizzard PPC: the card from
  expansion.library, the 68k CPU beside it, the kernel in memory (WarpOS or
  PowerUP, with versions, ppclibemu and `powerpc.library` V7 told apart), the
  flash ROM's version, 68040/68060.library, PPC patches at boot, and
  combinations known not to work. With WarpOS running, the PowerPC and its
  clock from WarpOS's `showinfo`. Read only: nothing is loaded that was not
  active. ([#23](https://github.com/akustikrausch/amigent/issues/23))
- A WarpOS or PowerUP crash window is read by `amiga_software_failure`,
  `amiga_ppc("crash")` and `amiga_run`: the exception in words, the task,
  SRR0 and LR named like a 68k crash address; `dismiss` closes the window.
  ([#23](https://github.com/akustikrausch/amigent/issues/23))
- `amiga_build(target="warpos")` builds a PowerPC program with vbcc's WarpOS
  target; `amiga_tasks` lists the PowerPC tasks with their load (WarpOS's
  `stat`); `amiga_run` warns when a WarpOS program meets an Amiga without
  WarpOS. ([#24](https://github.com/akustikrausch/amigent/issues/24))

### Building

- A GitHub workflow builds amigentd, amigent-status and amigent-monitor with
  GCC 16 whenever `amiga/` changes. (#26)

### Fixes

- A viewer that takes over from another one keeps the screen picture instead
  of starting from black. (#29)
- On ApolloOS, `amiga_status` takes the system from amigentd's own report when
  the version text alone looks like AmigaOS 4, so the FPU is described again.
  (#49, thanks to AmanitaHouse)
- `amigent-setup --update` leaves the amigentd on an ApolloOS machine in place
  when the package has no Apollo build; `--force` replaces it all the same.
- Jobs no longer pipe through amigentd's FOLLOW helper when the path amigentd
  reports for itself does not exist; they run as before 1.4.0 then.
- A screen pause of 120 s is reported as 120 s, not 121. (#32)
- `amigent-doctor --help` exits with 0. (#34)
- The remote desktop no longer sends the reset chord to Kickstart 1.3 and
  older, which cannot reset that way and kept the keys down. (#28)
- Auto packing waits for a real measurement of the line on a fast CPU instead
  of a guess that never ended. (#30)
- An amigentd that does not report free Chip RAM shows it as unknown, not as
  0 bytes. (#33)
- When the remote desktop cannot reach the Amiga it gives one of a few fixed
  reasons in the page's language, including a TLS certificate that does not
  match; the full text goes to the terminal only. (#36)
- Auto packing never has a 68000 or 68010 deflate its screens or replies, not
  even as a try now and then: there it cost more time than it saved bytes. A
  packing level chosen by hand still applies. (#43)
- amigentd turns screens into tiles faster: RGB888 to RGB332 through lookup
  tables, a tighter PackBits and colour index pass, and no try of a method
  that cannot beat the best one so far. The tiles on the wire stay the same.
  (#43)
- amigentd's tile buffers were 14 bytes short for PackBits of noise in a full
  64×64 chunky or 8-plane tile; the last bytes ran past them. (#43)
- `amiga_screen_text` reads `amigent-monitor` again, and other programs that
  draw text into a bitmap of their own and copy it to their window against
  flicker. The monitor's window is marked; `amiga_wait_text`,
  `amiga_click_text` and a search with `find` leave it out unless it is named,
  since its log repeats the agent's own calls. (#41)
- `amigent-monitor` runs on a 68000 again: 1.4.0 shipped a 68020 build, which
  stopped an A500 with an illegal instruction. On chipset Amigas, closing the
  monitor or resizing its window could also crash the Amiga a moment later,
  because the monitor freed its drawing buffer while the blitter was still
  clearing it. (#41)

### Tools and languages

- `amiga_hrtmon` loads the HRTmon debugger from Aminet for the user at the
  Amiga. The agent never enters it: that halts the system. (#37)
- `amiga_screenshot` says when the display shows something other than the
  front Intuition screen: a chipset screen on the chipset output next to a
  graphics card Workbench, the card's screen next to a chipset one, or a view
  a program loaded itself. `screen=` takes a screen from `amiga_screens` and
  reads it from its bitmap, in front or not, without moving it. Needs the new
  amigentd; older ones picture the front screen as before. (#44)
- `amiga_memory` reads a running program's memory by hunk:
  `ProTracker:hunk1+$120`, or `task=`, `hunk=` and `offset=`. The read stays
  inside the hunk. (#44)
- `amiga_wait_task` waits until a task or program runs, or until it has
  ended, for programs a script started with `Run`. (#45)
- `amiga_amigus_status` tells whether an AmiGUS card is there, whether
  amigus.library and the AHI driver are loaded, and who holds PCM, wavetable
  and codec. It reads amigus.library's records and reserves nothing. (#45)
- `amiga_audio_levels` reads an AmiGUS card's left and right output level for
  a few seconds, with the small program `amigent-levels` on the Amiga. (#45)
- `amiga_pi_network` tells, on a PiStorm with Emu68, which of the Pi's
  network drivers are there (`genet.device` for the Ethernet of a Pi 4 or
  CM4, `wifipi.device` for Wi-Fi; a missing one is named with where it comes
  from), the TCP/IP stack, Roadshow's interfaces and the link speed. With
  `confirm=True` it adds the Pi's Ethernet or Wi-Fi as a second Roadshow
  interface next to the existing one, on probation: a script on the Amiga
  takes it down again unless the PC reaches the Amiga through the new address
  in time, and a restart forgets it. Once reached, it is kept, the speed
  before and after is measured, and Amigent moves to the new address;
  `undo` takes it back. The Pi's boot partition is never touched. (#20)
- The Pi's serial debug output (Emu68) can be recorded on the PC through a
  USB serial adapter, with time stamps: `amiga_serial_log('start')` or
  `amigent-serial`, with the optional `amigent[serial]` (pyserial).
  `amiga_debug_log` includes it, also while the Amiga is frozen, and
  `amiga_status` gives the last lines when the Amiga does not answer.
  `amiga_serial_log('narrow', low=..., high=...)` limits Emu68's JIT debug
  output to an address range at run time. Without an adapter nothing
  changes. (#21)
- `amiga_graphics` tells the graphics board: Picasso96 or CyberGraphX, the
  card and chip driver with versions, the board's Zorro bus, and Warp3D and
  MiniGL. `amiga_screen_modes` lists the Picasso96 screen modes with the colour
  depths switched on, explains a missing mode, and switches single depths on or
  off with a copy first and undo in one call. On a Zorro II card the remote
  desktop asks for smaller pieces and looks less often. (#22)
- The remote desktop in Russian. (#35)
- Greek and Turkish for the remote desktop and the overview.

### Thanks

To reinauer, midwan and AmanitaHouse for the pull requests in this release.

## 1.4.0 (2026-11-18)

### After a crash

- `amiga_status` puts the guru in words: part of the system, kind of error,
  dead end or not, and the task or address (`last_alert_where`, protocol 6).
- The PC keeps the agent's last calls for each Amiga. After a restart or a
  guru, `amiga_status` lists the last ones before the Amiga went away and the
  programs that ran then but don't now.

### What runs, memory and code

- amigentd 1.4.0 speaks protocol 6: LOADED lists libraries, devices and
  resources with version, open count and base, the hunks of running programs
  and the resident modules; PEEK reads up to 64 KB of RAM exec manages or of
  the Kickstart ROM, never custom chips, CIAs or boards.
- `amiga_loaded` shows what is in memory, such as the AHI driver that really
  runs. `amiga_where` names an address: program, hunk and offset; library
  function through its jump table; resident module or ROM.
- `amiga_memory` reads memory as hex, text or 68k code; calls through a
  library base come with their names (`jsr -132(a6)` is `exec Forbid`), from
  tables of 76 libraries, devices and resources. Addresses can be given as
  `dos.library/Write`. Needs the capstone disassembler, now a dependency.

### Jobs

- `amiga_job` numbers its jobs, and several run at once. `amiga_jobs` lists
  them with their return codes, `amiga_job_output` reads what one printed so
  far, `amiga_break(job=n)` stops one. A return code other than 0 is said.
- With amigentd 1.4.0 a job's output is readable while it runs:
  `amigentd FOLLOW` copies it from a pipe into its file piece by piece.
- A job of several lines runs as a script of its own, so all of its output
  is caught, not only the last line's.
- Without `PIPE:` (a bare Workbench disk has no queue-handler) jobs run as
  before; a job whose output helper could not start still ends.

### Debugging programs

- `amiga_trace` sets tracepoints in a running program: when it gets to the
  address, amigentd keeps its registers and the top of its stack, puts the
  instruction back and the program runs on. A TRAP #15 with a trap handler of
  amigentd's own for that task; at most 8, each fires once, one per address.
  Tested on a 68040 (PiStorm) and a 68000.
- `amiga_tasks` lists every task with priority, state and the stack it uses
  now, warning near full; `amiga_task` changes a priority or sends Ctrl-C to F.
- `amiga_memory_write` patches RAM, refused unless amigentd runs with
  `ALLOWPOKE`.
- `amiga_debug_log` names the code at addresses in Enforcer, MuForce and
  CacheCop hits: program and hunk, or library function.
- `amiga_gurus` explains guru codes, the last one and those in MCP's
  S:GuruHistory, with MCP's own texts where it has them.
- `amiga_state` saves memory, open counts, tasks and ports and later says what
  changed: what a program left behind.
- `amiga_screen_diff` compares the screen with a saved picture: share of
  changed pixels, boxes of the changed areas, a picture of them.
- `amiga_create_project` writes a C program and a vbcc build script onto the
  Amiga, `amiga_build` compiles it there.
- `amiga_everywhere` (and `amigent-fleet --run`) runs one command on every
  known Amiga and compares the results.
- `amigent-doctor` checks protocol 6, the disassembler and an emulator's GDB
  server too.

### Hangs, stutter and requesters

Wishes from measuring window dragging on a real A1200:

- `amiga_loaded("programs", name)` lists every hunk of that program with its
  address and size; addresses take `prog hunk 4 +$10`, `prog:hunk4+0x10` and
  `prog hunk 4`.
- `amiga_task(name, stack=N)` says which signals a task waits for and has, and
  names up to N return addresses on its stack: only longwords right behind a
  JSR or BSR, read in one go.
- `amiga_locks` shows who holds the layer locks of a screen and its windows
  and who waits for them, looked at under Forbid only (LOCKS, protocol 6).
- `amiga_cpu` measures the CPU load per task on the Amiga itself, sampled at
  priority 30 with no network traffic meanwhile (CPU).
- `amiga_drag_measure` drags a window by its title bar at priority 21 and
  times every step: median, p95 and max per step, steps the window did not
  follow, how far it lagged (DRAG).
- amigent-monitor draws only when something changed, and nothing at all while
  amigentd measures; `amiga_quiet_monitor` keeps it still by hand (QUIET).
  Its drawing held its layer, and Intuition waited for it.
- `amiga_drag` says which window moved, or that none did and which one was
  frontmost at the start point.
- `amiga_ui_click` finds buttons whose label is drawn as text, such as those
  of system requesters, and clicks the text when no gadget carries it.
  `amiga_software_failure` reads a Software Failure requester, puts the guru
  in words and clicks Suspend or Entfernen, never Reboot.
- `amiga_memory` reads the vectors below $400, ExecBase at 4 among them, and
  `amiga_status` gives `execbase`.
- `amiga_start(..., unique=True)` starts nothing when the program runs
  already, and `amiga_tasks` marks programs that run twice.

### Remote desktop

- When the remote desktop cannot reach the Amiga, the page says why (a key
  that doesn't match, no answer at the address, ...) and where it connects
  to, and `amigent-viewer` prints each change in the terminal. Before, every
  failure looked like a hang
  ([#4](https://github.com/akustikrausch/amigent/issues/4)).
- Restart on an Amiga older than OS 3.0 without `C:Reboot`: the viewer
  flushes the disks and holds Ctrl and both Amiga keys; amigentd sees the
  three keys down within five seconds and calls ColdReboot(). Needs the 1.4.0
  amigentd. (#14)

### Emulators

- `amiga_emulator` talks to WinUAE's GDB server (WinUAE 6.1): status, pause,
  resume, registers with the PC named, memory read by the emulator even when
  the Amiga hangs, a postmortem, and saving or restoring the whole machine.
  Set `"gdb": "host:port"` in the Amiga's entry in `amigas.json`.
- With it set, `amiga_install_begin` saves the whole machine as well.
- `amiga_emulator` also starts WinUAE with a configuration and its GDB server
  (and quits only the one it started), puts a disk into DF0:, and shows the
  custom chips as the emulator keeps them: Paula's four channels with sample,
  length, rate and volume, the copper list decoded, sprites and DMA.

### README and help

- Set up in three steps at the top, and a diagram of how the parts fit.
- The Amiga archive has an AmigaGuide help, `Amigent.guide`, next to the
  ReadMe: installation, updating, tool types, ARexx, the monitor, what to
  check when it doesn't work.

### Building

- amigentd also builds with GCC (`m68k-amigaos-gcc -noixemul`), next to vbcc.
  The library calls amigentd declares itself (AmiSSL) and `cmp_longs()` have
  GCC versions; vbcc builds as before. See GUIDE.md, Building amigentd. (#7)

### Updating

- Replacing a running amigentd (Amigent-Update, `amigent-setup --update`)
  failed when the old one had served many connections: the TCP stack
  (AmiNetXDuo here) keeps the port taken for about a minute after it quits,
  while the new amigentd tried for 60 s and the script waited 15 s for it,
  then put the old one back. The new amigentd now tries for 90 s, the script
  waits 100 s and `amigent-setup --update` 180 s. (#8)

### Packing

- A packing mode set with `amiga_link` (or `set_mode`) did not stay when
  another amigent process ran for the same Amiga, such as a second session or
  the remote desktop: each one kept the mode it had read at its start and
  wrote it back into link.json with every line measurement, so `best` set
  earlier came back minutes after `auto` replaced it. Saving now keeps the
  mode link.json has, and a running process goes by it from its next save
  on, unless its mode came from `AMIGENT_COMPRESS` or amigas.json. (#10)
- Packing could switch itself on for good on a fast line. Screens counted as
  line samples, but a screen arrives as fast as the Amiga turns it into tiles
  (a whole one at about 235 KB/s on a 68060 with a ZZ9000, on a line of
  1.7 MB/s). Slow samples could pull the estimate below twice the Amiga's
  packing speed (this PC had it at 152 KB/s); from then on every reply came
  packed, and packed replies never measured the line again: downloads ran at
  0.58 MB/s and whole looks took 3.0 s. (#9)
- Screens no longer guess from the line: they measure. How fast a screen's
  tiles arrive is timed both ways, packed and not, and the faster way wins;
  the other gets one screen in thirty, in case things changed. A packed reply
  on the command channel, timed from its request, can raise the line estimate
  but never lower it. The same session as above recovered after its first
  download (1.75 MB/s) and after one packed look (2.2 s). (#9)
- The line estimate in link.json is written whole, also when the screen and
  command connections save it at once. (#9)

### ZZ9000

- amigentd reads a ZZ9000's screen through the card's own encoder instead of
  over the Zorro bus: the card compares the screen with what it told amigentd
  last and sends the changed tiles only. On a 68060 at 1280×720 a look at a
  screen where little changed went from 0.73 s to 55 ms, a whole picture from
  2.0 to 0.9 s. Needs zz9k.library 2 and ZZ9000 firmware 2.9.0 or newer (the
  console encoder, service 0x8200); without them, or on any other screen,
  nothing changes.
  SYSINFO says `zz9000=1` when the last look went through the card. Numbers in
  PERFORMANCE.md. (#6)

### Monitor

- amigent-monitor draws its updates into a bitmap of its own and copies only
  what changed into the window, so the side panel no longer flickers when it
  is redrawn. Without the memory for it, it draws directly as before. (#11)

### Fixes

- `amiga_break(job=n)` stops a job of several lines as a whole. Ctrl-C alone
  only ended the line that ran, and on a slow Amiga the job went on with the
  next one.
- `amiga_status` also recognizes a second kind of AI development bridge on the
  Amiga, by its message port, task, TCP port, files and S:User-Startup line,
  and says so before the first click, as for the others.
- For an emulator on this PC, the TCP check for other bridges is skipped:
  there the port belongs to the PC, and only the Amiga's own names count.
- amigentd gives the memory of its screen pictures back when nobody looks at
  the screen any more: when the screen link closes, breaks off or is dropped
  after 120 seconds of silence and no other client takes its place. Until now
  they stayed until amigentd quit: two copies of the bitplanes of a chipset
  screen, two bytes a pixel for a graphics card screen or the classic frame,
  and up to 325 KB for packing tiles. With all three in use, a 640x256
  Workbench in 4 colours held 767 KB in WinUAE's A1200. The next picture
  starts from scratch, as after a change of screen size: every tile is sent
  once and CLIENT answers 1. A client that takes over from another still gets
  only the changes. (#5)
- QUIT and Ctrl-C free the planar picture (the bitplanes of a chipset screen)
  as well; it was left behind. (#5)

### Thanks

To kaffeine1, midwan, reinauer and AmanitaHouse for the pull requests in this
release.

## 1.3.0 (2026-11-09)

### Text instead of pictures

- amigentd records what programs draw as text, window by window, and
  `amiga_screen_text` hands it to the AI agent with positions: Shell output,
  requester messages, lists, status lines. A few hundred bytes instead of a
  screenshot. Rows behind other windows are left out, and it says plainly
  when the text is not enough (buttons drawn as pictures, games, windows
  open before amigentd started), so the agent takes a picture of just that
  part. `amiga_screenshot(text=True)` gives both. The remote desktop stays
  a picture.
- amigent-monitor's own log lines are not part of the screen text, so
  waiting for or clicking on a text never finds the monitor's copy of it.
- `amiga_click_text` clicks on visible text, such as a button label that
  `amiga_ui_tree` can't name.
- Gadget labels that Intuition draws itself (an IntuiText in the gadget, as
  most programs without GadTools have) appear in the screen text at their
  place. GadTools button labels on OS 3.2 sit in a private image class and
  stay invisible to it: take a picture of that part.
- `amiga_drag` moves and sizes windows. It goes the way as mouse movement
  in small steps with the button held, as a hand on the mouse would; a jump
  of the pointer left drag bars and size gadgets where they were, and
  programs that follow the mouse themselves, such as ProTracker 4's window,
  ignored it. Before it presses the button it walks the way once with the
  button up and measures where the pointer really went, so the Amiga's
  mouse acceleration doesn't make it overshoot. On chipset screens the
  distance is converted to Intuition's mouse units (lores, superhires, no
  interlace). `relative=False` jumps as before.
- `amiga_mouse_relative` moves the mouse by a distance, says where the
  pointer is afterwards and with `exact=True` lands on the spot. UITREE's
  screen line carries the pointer position (`mousex`, `mousey`).
- `amiga_ui_tree(screen_id=...)` looks at a screen behind the front one
  without bringing it forward.
- `amiga_mouse_move` and the clicks put a point off the screen on its edge
  instead of failing, as a window partly outside the screen gives such points.
- `amiga_ui_click` with a window title that several windows share takes the
  frontmost one.
- `amiga_wait_window` and `amiga_wait_text` wait on the Amiga itself until a
  window opens or closes, or a text shows up or goes; the Amiga looks ten
  times a second and the line stays quiet.
- `amiga_screenshot(changed=True)` shows only the parts of the screen that
  changed since the last look, each with its place.
- `amiga_do` carries out several steps in one call (clicks, keys, typing,
  menus, waits, commands) and ends with one look: text, the changed parts or
  a screenshot. Saves a round trip per step.

### Watching the agent on the Amiga

- amigent-monitor opens a window on the Amiga that shows, line by line, what
  the agent is doing: each tool it calls and the commands, files, clicks and
  keys that reach amigentd, with the time. Beside it the link: data rates
  with the last minute as a graph, totals, how much the agent did, free
  memory. Dark green on RTG screens, the Workbench's own colours on a 4 or 8
  colour screen; it fits itself to a 640x256 Workbench and runs on OS 2.04
  and a 68000. The window never takes the input focus. NOTE (37) carries
  the tool names; it has no answer and costs no round trip.
- The amigent-monitor window can be sized; the log and the side panel
  follow the new size, and below 56 columns the side panel gives way to the
  log. WIDTH and HEIGHT set the size it opens with. The zoom gadget switches
  to a compact size in place, and the window never ends up partly off the
  screen.

### Another agent on the same Amiga

- `amiga_status` says when other remote control software for AI agents runs
  on the Amiga too, such as amimcp's amiagent, or is installed there; while
  one runs, the first click or key of a session carries a note that two
  agents would share one mouse and keyboard. Nothing is stopped or removed.
  amigentd's SYSINFO lists the public ports and tasks for it (`ports=`,
  `tasks=`).
- The viewer no longer says the Amiga is not answering while an agent works
  on it at the same time. Only one screen connection can exist, so each agent
  action pushes the viewer out for a moment; it now checks on the command
  line that the Amiga is there and comes back a second later.

### Faster on slow networks

- Replies, file transfers, command output and screen tiles travel packed
  (deflate) when the Amiga packs clearly faster than its network carries,
  such as a PiStorm or a 68080 behind a plipbox. amigentd measures its own
  speed once, the PC measures the line, and a slow CPU on a fast network
  stays unpacked. With a PiStorm accelerator behind a plipbox: command
  output 11 times as fast, writing 4.6 times, a whole 1920x1080 screen 2.4
  times. `amiga_link` shows the numbers and sets it by hand (auto, off, fast,
  best), as does `AMIGENT_COMPRESS` or `"compress"` in amigas.json.

### Installing and developing

- `amiga_install_begin`, `amiga_install_end` and `amiga_install_undo` make
  an installation undoable: the system folders (and the program's own) are
  recorded and copied aside on the Amiga, what came, changed and went is
  listed afterwards, and undo puts everything back. A repair module and a
  copy of the new files are written for the program store.
- `amiga_aminet_search`, `amiga_aminet_readme` and `amiga_aminet_install`
  fetch from Aminet on the PC; the archive travels packed and the Amiga's own
  LhA, UnLZX or UnZip unpacks it (the PC does it for .lha and .zip when the
  Amiga lacks the tool). Disk images (.adf, .adz) are read on the PC, OFS and
  FFS, and their files sent; .dms is refused with a hint.
- `amiga_debug_log` shows what went through exec's RawPutChar: kprintf
  output and Enforcer or MuForce hits, as Sashimi catches them.
- `amiga_run` uploads a program if it is local, runs it and reports return
  code, output, its debug output, requesters that came up and, if the Amiga
  dropped off, whether it restarted or left a guru.
- `amiga_clipboard` reads and sets the Amiga's clipboard for the AI agent.
- `amiga_note` and `amiga_notes` keep what the agent learned about a program
  on this Amiga (the stack it needs, the requester that comes up) for the
  next session; `amiga_start` shows them when it starts that program.

### Scripts on the Amiga

- amigentd's ARexx port brings windows to the front and types into them:
  `ACTIVATEWINDOW <pattern>` finds a window by its title (AmigaDOS patterns,
  or a part of the title) and gives it the keys, `TYPE <text>` types through
  the Amiga's keymap with keys in angle brackets (`<return>`, `<ctrl c>`,
  `<lamiga q>`), `KEY <name>` presses one key, `BUSY` tells whether a menu
  or a popup holds Intuition up.

### Crashes and things that hold the Amiga up

- `amiga_status` tells whether the Amiga started again since the last look
  and how long ago, or only amigentd did, and the guru code exec kept from
  before the last reset. After a lost connection that tells a crash from a
  network fault. amigentd's SYSINFO has `uptime`, `daemon_uptime` and
  `last_alert` for it.
- An open menu, and a popup left open as a short right click leaves it on a
  Directory Opus desktop, hold back every new window until they close.
  amigentd now notices both (UITREE's new `held` field); `amiga_ui_tree`
  says so, and `amiga_start` closes such a popup with Esc before it starts
  the program, or lets go of a right button it holds itself.
- `amiga_type` with several lines types them one at a time and waits after
  each Return until the window has been still for a moment (up to `settle`
  seconds, 10 by default), so a Shell shows each command's output before the
  next one comes.
- `amiga_follow` copies a growing log to the PC while it grows, a file or the
  debug output, so it survives a hang: the copy holds everything up to that
  moment, also what the filesystem had not yet written to disk, and marks
  where the Amiga stopped answering. READ can start at an offset for it.

### Fixes

- `amiga_status` names the system: AmigaOS 3.x, AmigaOS 4 or MorphOS, the CPU,
  and the PowerPC software of a WarpOS or PowerUP board, read from the
  libraries' version strings without opening them. On AmigaOS 4 and MorphOS
  it adds that amigentd runs there under 68k emulation and is untested. The
  remote desktop shows the PowerPC software next to the CPU.
- The PC notices when the Amiga was updated on its own, with a newer amigentd
  than it knows: the agent's next answer starts with a note, `amiga_status`
  has it under `update_pc`, and the remote desktop shows a message. The
  update instructions say in every place that both halves need it.
- A screen without a title, such as SysInfo's, is listed in the remote
  desktop as "Untitled screen" instead of "?".
- Errors from AmigaDOS carry their number (`Objekt ist in Gebrauch [202]` on
  the wire), so the PC explains the common ones in any language: a file
  another program has open for writing, a volume or assign that is gone
  after a restart.
- `amiga_button` says that programs reading the mouse buttons straight from
  the hardware (ProTracker, many games) don't see them.
- A volume that isn't there makes a command or a file transfer fail at once.
  Before, AmigaDOS opened an "insert volume" requester that waited for
  someone at the Amiga and held up the command until then. Programs started
  through amigentd get the same: they see an error instead of the requester.
- `amiga_wait_change` said the link was slow when its time ran out on a quiet
  screen over a fast link; now it says there was no change.
- Writing a file that a program watches or keeps locked, such as
  `ENV:Sys/ahi.prefs` while AHI runs, failed with "object already exists";
  now the new content goes into the file in place.
- Reading a file that opens but can't be read gives an error instead of
  dropping the connection.
- `amigent-setup --update` goes on when the Amigent folder can't take
  amigent-status or amigent-monitor (gone, full or write-protected) and says
  so; before, that stopped the update. Amigent-Update on the Amiga now also
  brings the two windows into the Amigent folder.
- Copying a folder to the Amiga failed when a new folder held only other new
  folders (`a/b/file` with neither `a` nor `a/b` there yet).
- Archives unpacked on the PC keep their folders: LhA names with
  backslashes became single files named `Dir\File` before. They can't
  write outside their temporary folder,
  also on Windows with backslashes or drive letters in member names; a disk
  image whose directory contains itself ends with an error.
- Repair modules written after an installation also put back files it
  changed (when they are missing or of another size), and restore new folders
  as a whole.
- 256-colour graphics card screens, as games open them, come through in their
  real colours. They came out as a few shades of blue or black: some RTG
  drivers convert such screens through a palette other than the one on
  display. amigentd now reads the pens itself and looks them up in the
  screen's palette.

### For developers

- `scripts/regress.py HOST` runs every tool against a real Amiga or an
  emulator before a release and prints PASS, FAIL or SKIP per check;
  `scripts/pointer_test.py` waits for slow Amigas and skips screen modes the
  chipset doesn't have.
- Protocol 5: command channels get TEXT, WAIT, COMPRESS, TREE (a whole
  folder in one answer, with CRCs) and DEBUGLOG; FRAME may deflate tiles;
  SYSINFO tells the packing speeds. amigentd patches Text, RectFill,
  EraseRect, SetRast, ScrollRaster(BF), DeleteLayer and RawPutChar through
  small stubs that stay valid after it quits; `NOTEXT` and `NODEBUG` leave
  them out. See docs/PROTOCOL.md.
- `tests/test_deflate_c.py` builds amigentd's deflate and inflate with gcc
  and checks them against zlib both ways.

## 1.2.0 (2026-11-05)

### Pause the picture

- The Amiga can stop reading its screen for a while, so a test, a benchmark or
  a demo has the CPU and the chip bus to itself: *Pause the picture* in the
  remote desktop (1, 5 or 15 minutes, *Resume* ends it), `amiga_pause_screen`
  for the AI agent, and `PAUSE <seconds>` / `RESUME` on the ARexx port
  `AMIGENT` for scripts on the Amiga.
- While paused every frame comes back empty without touching the screen, for
  every client; the remote desktop keeps its last picture and shows the time
  left, screenshots and `amiga_wait_change` say the screen is paused. Mouse,
  keys, commands and files carry on as usual.
- amigentd 1.2.0, protocol 4: the PAUSE command (19) on the screen connection
  and on command channels, the seconds left in INFO, SYSINFO (`screen_paused`)
  and the ARexx STATUS (`paused`). Older clients work unchanged; the pause
  needs the new amigentd.

### Updating the Amiga

- `amigent-setup --update` puts the new amigentd on an Amiga that already runs
  one, by network and with nobody at the Amiga. Key, certificate and the start
  line (`TLSONLY` too) stay; if the new amigentd does not start, the Amiga
  starts the old one again.
- On the Amiga, *Amigent-Update* in the archive's drawer does the same with a
  double-click. Both run the same script, `amigent-update.rexx`.
- amigentd started a second time on the same port says at once that one runs
  already and points to *Amigent-Update*, instead of trying for a minute.

## 1.1.1 (2026-11-03)

The first release of Amigent: complete remote control for a real Amiga from a
PC or Mac, by hand or by an AI agent, tuned for slow Amigas and slow networks.
The measurements behind the speed are in
[docs/PERFORMANCE.md](docs/PERFORMANCE.md).

### Remote desktop, like TeamViewer or VNC

- The Amiga's screen live in a window, your own mouse and keyboard on it,
  typing through the Amiga's keymap (umlauts and AltGr included) or as direct
  key presses for games, and a mouse mode for games that need movement.
- A side panel with the Amiga keys, sticky modifiers and common shortcuts, and
  a list of all screens to bring one to the front.
- Files in both directions: drop them on the window or pick a folder on the
  Amiga; transfers are checked with a checksum. The clipboard is shared both
  ways.
- The machine's details at a glance: OS, CPU, FPU, memory, network stack and
  the free space of every volume. Twelve languages.
- A program of its own: Start menu and *Installed apps* on Windows, a signed
  and notarized `Amigent.app` on macOS, a browser window on Linux.

### AI agent over MCP

- Claude Code, Codex or any MCP client gets the Amiga's Shell, files, long
  jobs, ARexx, screenshots, mouse and keyboard, plus windows, gadgets and menus
  as text so it clicks by name instead of by pixel. Gadgets without a readable
  label come as small numbered pictures.
- Whole folders in one stream in both directions, unchanged files skipped,
  screenshots kept small, and waiting for a change without sending pictures.
- You see every click the agent makes in the remote desktop and can take over
  at any moment.

### Made for slow machines and slow wires

- Chipset screens go out as the bitplanes the Amiga keeps anyway, graphics-card
  screens as tiles with their own small palette, and only the tiles that
  changed, packed as the difference to the last picture, the row above or the
  same picture scrolled.
- Mouse, clicks and keys are carried out at once, even while a picture is still
  on its way, and the area around the pointer comes first.
- On a 7 MHz 68000 a whole 640×256 screen takes under a second and an unchanged
  one 80 ms, at under 10 % of the CPU while the PC watches; a 14 MHz 68020
  spends 2–3 %. A 1920×1080 graphics-card screen over a plipbox takes about
  3 seconds.

### On the Amiga

- One small service, `amigentd`, for screen, commands and files, from
  Kickstart 2.04 on and any CPU from the 68000 (`amigentd.000`) up; a Vampire's
  68080 runs the plain `amigentd`. Starts from the Shell or Workbench, with
  its settings as tool types.
- The ARexx port `AMIGENT` (STATUS, CLIENTS, VERSION, HELP, QUIT) and the window
  `amigent-status` showing who is connected.
- `amigent-repair` puts back what a big OS update removed, with a small module
  per installed program; a version scan compares installed programs with the
  last scan.

### Security

- Every connection needs a key, one per Amiga, created by `amigent-setup`.
- With AmiSSL on the Amiga the connection is encrypted with TLS, pinned to the
  certificate made for that Amiga, and the plain port is closed to other
  computers (`--allow-plain` keeps it open).
- The remote desktop and the overview listen only on the PC itself.

### Several Amigas

- Each Amiga is remembered under a name and found again on the home network
  when its address changes, recognised by its certificate or key.
- `amigent-fleet` shows them side by side with a preview, runs one command on
  all of them and restarts each with its disks written first.

### Installation

- `pipx install amigent` on the PC or Mac; the Amiga programs ride inside the
  package. `amigent-setup` finds the Amiga, installs amigentd over the network
  and picks the 68000 build where needed; `--serve` hands amigentd to the
  Amiga's browser when there is no other way to get it there.
- Windows works with WSL or a plain Windows Python, encryption with the
  `openssl` of Git for Windows; macOS with Homebrew Python and its own LibreSSL.
- `amiga_restart` writes the disks first, makes sure the Amiga really went
  down (Workbench 2.04 has no `C:Reboot`, which it now says), and with a smart
  plug cycles the power by itself if the network does not come back.

Tested on a real A1200 (PiStorm, RTG, plipbox), in WinUAE on a 68000 with
Kickstart 3.1 and 2.04 and on a 14 MHz 68020, with Claude Code and Codex, and
on Windows, macOS and Linux for every change.
