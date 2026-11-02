# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 1.0.1 (2026-11-02)

- `amigentd` written from scratch, from the protocol alone. Same wire format, so
  nothing changes for clients.
- Chipset screens are read through their palette, so games and demos on a
  native screen show up in the picture too.
- `amigentd PORT <n>` runs a second copy on another port, next to the first.
- [docs/PROTOCOL.md](docs/PROTOCOL.md) spells out the fine print: tile grid,
  colour bits, runs, takeover, timeouts, what SETUP does when something fails.
- `amiga/build-amigentd` takes `SOURCE` and `TO`.

## 1.0.0 (2026-11-02)

The first full release. Everything from the 0.x line, now considered stable:

- **Control by AI**: MCP server with Shell, files, screen, mouse and
  keys on a real Amiga over the LAN, through `amigactld` and our own `amigentd`.
- **You at the keyboard**: Amigent, the live viewer with mouse, keyboard, Amiga
  shortcuts, file upload and the machine's details, in twelve languages.
- **On Windows**: installed like a program, with Start menu, desktop and
  Installed apps entries and an uninstaller.
- **Install in three steps**: one small file on the Amiga, the rest done by
  `amigent-setup` over the network.
- **Repair after system updates**: `amigent-repair` and a module per program.

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
