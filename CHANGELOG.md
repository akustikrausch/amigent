# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

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
