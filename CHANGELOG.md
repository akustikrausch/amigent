# changelog

Versions follow [semver](https://semver.org). The number lives in
`src/amigent/__init__.py`, everything else reads it from there.

## 0.3.0 (2026-11-02)

- Amigent as a Windows program: `amigent-viewer <address> --install` puts it
  into the Start menu, on the desktop and into Installed apps, with an
  uninstaller. Starts without a console, quits when its window closes.
- A second start opens the window of the viewer that already runs, including
  the one Claude opened.
- New look: Workbench grey around a dimmed stage, a toolbar for the main
  actions, the side panel as a slice of Amiga keyboard with LED modifiers.
- Own icon.
- `amigent-viewer --version`, version shown in the viewer's status bar.

## 0.2.0 (2026-11-02)

- The viewer: live screen, mouse and keyboard, Amiga shortcuts, file upload
  with a folder picker. Opened by Claude through `amiga_viewer`, or with
  `amigent-viewer`.

## 0.1.0 (2026-11-02)

- First release: MCP server, `amigentd`, `amigent-setup`, inventory and repair
  scripts.
