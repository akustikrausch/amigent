# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 1.0.0 (2026-11-02)

The first full release. Everything from the 0.x line, now considered stable:

- **Control by AI**: MCP server with Shell, files, screen, mouse and keys on a
  real Amiga over the LAN.
- **One service on the Amiga**: `amigentd` handles screen, commands and files
  on one port. amigactld is no longer needed; older installs keep working
  with it, and `amigent-setup --with-amigactld` still installs it.
- **68000 to 68060**: `amigentd.000` for a plain 68000, picked by
  `amigent-setup` on its own.
- **You at the keyboard**: Amigent, the remote desktop with mouse, keyboard,
  Amiga shortcuts, files in both directions, the clipboard in both directions
  and the machine's details, in twelve languages.
- **On Windows and macOS**: installed like a program, with Start menu,
  desktop and Installed apps entries on Windows and an app in
  `~/Applications` on macOS.
- **Several Amigas**: `amigent-fleet` shows them side by side with a preview
  and opens each one's remote desktop.
- **Amigas by name**: each one is remembered under a name and found again on
  the home network when its address changes, by its certificate or key.
  `AMIGENT_HOST` is optional; `amiga_find` lets the agent search.
- **A signed Mac app**: `Amigent.app` carries a Developer ID signature and is
  notarized; Cmd works as the Amiga key, Cmd+V pastes, Option types @ and €.
- **Install in three steps**: one small file on the Amiga, or the `.lha`
  archive with icons, the rest done by `amigent-setup` over the network.
  `amigent-setup --serve` offers amigentd to the Amiga's browser when there is
  no other way to get it there.
- **Restart without worry**: `amiga_restart` writes the disks first and, with a
  smart plug set up, cycles the power by itself if the network does not come
  back.
- **Repair after system updates**: `amigent-repair` and a module per program.

Tools for the AI agent beyond Shell and files: windows and gadgets as text with
click by label or number (unlabelled gadgets come as small numbered pictures),
menus by name, a full-colour region screenshot, waiting for a change without
pictures, a list of screens with switching, mouse moves and held buttons,
relative mouse movement for games, ARexx to any program, and Ctrl-C for
running commands. The remote desktop gets a screen switcher and a mouse mode
for games.

Security: `amigentd` asks every connection for a key, one per Amiga, created by
`amigent-setup`. With AmiSSL on the Amiga and `openssl` on the PC the
connection is encrypted with TLS, and the PC accepts only the certificate it
made for that Amiga. `amigent-repair` restores key and certificate after an OS
update.

`amigentd` in this release:

- Written from scratch, from the protocol alone
  ([docs/PROTOCOL.md](docs/PROTOCOL.md) spells out tile grid, colour bits,
  runs, takeover, timeouts and what SETUP does when something fails).
- An unchanged screen costs about one network round trip (~5 ms) to check.
- A new connection takes over at once, even in the middle of a frame.
- Chipset screens are read through their palette, HAM6 and HAM8 included, so
  games and demos on native screens show up too.
- Screen list and switching, full-colour regions, relative mouse, windows,
  gadgets (GadTools button labels included) and menus as data.
- Up to four command channels next to the screen client; a long command holds
  up neither the screen nor the other channels.
- TLS on the next port from `S:amigent-tls.pem`, picked up while running.
- Starts from Workbench, with its settings as tool types.
- Runs on OS 3.0 or newer; RTG is optional.
- `amigentd PORT <n>` runs a second copy on another port;
  `amiga/build-amigentd` takes `CPU`, `SOURCE` and `TO`.

## 0.5.0 (2026-11-02)

- Twelve languages: English, German, Polish, French, Spanish, Italian, Finnish,
  Swedish, Danish, Czech, Slovak and Hungarian. Picked from the system
  settings, changeable in About. One JSON file per language, checked by tests.
- The machine at a glance: OS and CPU in the toolbar, free memory in the status
  bar, Kickstart, Workbench, FPU, memory, network stack and volumes in About.
- Screenshots in the README.

## 0.4.0 (2026-11-02)

- About Amigent in the toolbar: version, connected Amiga, credits, a few tips,
  links to the project page and this changelog.
- Shell and mouse buttons in the panel no longer wrap.

## 0.3.0 (2026-11-02)

- Amigent as a Windows program: `amigent-viewer <address> --install` puts it
  into the Start menu, on the desktop and into Installed apps, with an
  uninstaller. Starts without a console, quits when its window closes.
- A second start opens the window of the viewer that already runs, including
  the one the AI agent opened.
- New look: Workbench grey around a dimmed stage, a toolbar for the main
  actions, the side panel as a slice of Amiga keyboard with LED modifiers.
- Own icon.
- `amigent-viewer --version`, version shown in the viewer's status bar.

## 0.2.0 (2026-11-02)

- The viewer: live screen, mouse and keyboard, Amiga shortcuts, file upload
  with a folder picker. Opened by the AI agent through `amiga_viewer`, or with
  `amigent-viewer`.

## 0.1.0 (2026-11-02)

- First release: MCP server, `amigentd`, `amigent-setup`, inventory and repair
  scripts.
