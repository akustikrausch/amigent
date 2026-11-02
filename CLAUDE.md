# Amigent

MCP bridge to a real Amiga. Start with [README.md](README.md).

| path | what |
|---|---|
| `src/amigent/` | MCP server, clients, installer, viewer |
| `src/amigent/web/` | the viewer's page (plain HTML, CSS, JS) and the app icon |
| `amiga/` | `amigentd.c`, scripts, prebuilt binary in `bin/` |
| `tests/` | run without an Amiga |
| `private/`, `CLAUDE.local.md`, `.mcp.json` | personal setup, not in git |

Anything about one specific machine goes into `private/`. Public code stays generic
and takes machine details from environment variables.

Rebuild `amiga/bin/amigentd` with `amiga/build-amigentd` on the Amiga after changing the C code.

When working on an Amiga:

- Long commands go through `amiga_job`, never through `amiga_exec`.
- Every install gets a repair module, see `amiga/repair.d/example.repair.txt`.
- Prefer `amiga_restart` over cutting the power.
- When the user wants to see or drive the Amiga themselves, call `amiga_viewer`.
  For a Start menu entry on Windows run `amigent-viewer <address> --install`.

Releases: raise `__version__` in `src/amigent/__init__.py`, add the changelog entry,
tag `vX.Y.Z`, then run `--install` again so Installed apps shows the new number.
- AmiStart's `sm.prefs` is changed with `amiga/menu-add.rexx` only.

Spelling: the product is **Amigent** with a capital A in text and UI. Lowercase only
where it is a technical name: the Python package, commands such as `amigent-viewer`,
the MCP server id, folders, URLs, and the Amiga daemon `amigentd`.
