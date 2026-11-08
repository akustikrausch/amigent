# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 1.3.0 (2026-11-08)

### Text instead of pictures

- amigentd records what programs draw as text, window by window, and
  `amiga_screen_text` hands it to the AI agent with positions: Shell output,
  requester messages, lists, status lines. A few hundred bytes instead of a
  screenshot. Rows behind other windows are left out, and it says plainly
  when the text is not enough (buttons drawn as pictures, games, windows
  open before amigentd started), so the agent takes a picture of just that
  part. `amiga_screenshot(text=True)` gives both. The remote desktop stays
  a picture.
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

- Errors from AmigaDOS carry their number (`Objekt ist in Gebrauch [202]` on
  the wire), so the PC explains the common ones in any language: a file
  another program has open for writing, a volume or assign that is gone
  after a restart.
- `amiga_button` says that programs reading the mouse buttons straight from
  the hardware (ProTracker, many games) don't see them.
- A volume that isn't there makes a command or a file transfer fail at once.
  Before, AmigaDOS opened an "insert volume" requester that waited for
  someone at the Amiga and held up the command until then.
- `amiga_wait_change` said the link was slow when its time ran out on a quiet
  screen over a fast link; now it says there was no change.
- Writing a file that a program watches or keeps locked, such as
  `ENV:Sys/ahi.prefs` while AHI runs, failed with "object already exists";
  now the new content goes into the file in place.
- Reading a file that opens but can't be read gives an error instead of
  dropping the connection.
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
