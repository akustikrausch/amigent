# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 1.1.0 (2026-11-03)

Made for slow machines and slow wires. Everything in this release is about a
plain Amiga on a plipbox feeling quick, for the AI agent and for you at the
mouse. The numbers and how they were taken are in
[docs/PERFORMANCE.md](docs/PERFORMANCE.md).

- **The screen travels as the Amiga keeps it.** Chipset screens go out as
  bitplanes, graphics-card screens as tiles with their own small palette, each
  tile packed as the difference to the last picture, to the row above or to
  the same picture scrolled. On a 7 MHz 68000 a whole 640×256 screen takes
  0.64 s instead of 15.4 s, an unchanged one 80 ms instead of 13 s; a
  1920×1080 graphics-card screen over a plipbox about 3 s instead of 10.
- **Your input comes first.** Mouse, clicks and keys are carried out between
  the tiles of a picture still on its way, and the area around the pointer is
  sent before the rest. A click over a 15 KB/s link shows its result in about
  0.3 s instead of 5.8 s.
- **The Amiga keeps its time.** While the PC only watches, Amigent costs a
  68000 under 10 % and a 14 MHz 68020 2–3 % of its CPU.
- **Faster for the AI agent.** Whole folders in one stream, unchanged files
  skipped, folders fetched from the Amiga too, screenshots a tenth of the size,
  and `amiga_wait_change` keeps its time limit on a busy link.
- **From Kickstart 2.04 on:** amigentd runs on OS 2.04, tested on an A500+
  with a 7 MHz 68000 and 1 MB of chip RAM: commands, files, ARexx, windows,
  menus and the remote desktop. Encryption still needs OS 3.0 and AmiSSL.
- **On the Amiga:** the ARexx port `AMIGENT` (STATUS, CLIENTS, VERSION, HELP,
  QUIT), the window `amigent-status` showing who is connected, a build for the
  68080, and the clock taken from the E clock so time limits survive a clock
  sync.
- **TLS only:** once encryption works, `amigent-setup` closes the plain port
  to other computers (`--allow-plain` keeps it open), and `amigent-repair`
  keeps it that way.
- **Several Amigas:** one command for all of them in `amigent-fleet`, details
  per Amiga, and a restart that writes the disks first.
- **Installed with pipx:** `pipx install amigent`, the Amiga programs ride
  inside the package. `amigent-setup --serve` hands amigentd to the Amiga's
  browser when there is no other way.
- **Windows without WSL** works with a plain Windows Python, encryption uses
  the `openssl` that comes with Git for Windows.
- **Restart without worry:** with a smart plug set up, `amiga_restart` cycles
  the power by itself if the network does not come back.
- Tested with Claude Code and Codex; tests run on Windows, macOS and Linux for
  every change.
