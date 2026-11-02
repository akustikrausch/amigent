# faq

**`amigent-setup` can't find my Amiga.**
Check that `amigentd SETUP` is running, then give it the address by hand:
`amigent-setup 192.168.1.50`. The search only covers your own /24.

**Claude says amigactld isn't answering.**
It does one command at a time. If something long is running, it waits. Long work
belongs in `amiga_job`, which runs at low priority and keeps the line free.

**Directory Opus says it has waited long enough for a program.**
Something in `SYS:WBStartup` never exits. Services go into `S:User-Startup`.

**Files are gone after pulling the plug.**
PFS3 commits lazily, so the last minutes can be lost, while the volume stays fine.
Use `amiga_restart`, which commits first. Or by hand: `Lock SYS: ON`, `Lock SYS: OFF`.

**No network after a boot.**
Some setups need a cold start: 30 seconds without power. Set `AMIGENT_POWER_CMD`
to a script for your smart plug and `amiga_power('cycle')` does it for you.

**How do I add a program to the AmiStart menu?**

    rx menu-add.rexx AUDIO|My App|SYS:Utilities/MyApp/MyApp

Section, name, program, and optionally the stack size. It goes in sorted by name.
Don't use the AmiStart ARexx `SAVE` command for this: it rewrites the whole file.

**Screenshots look a bit rough.**
They're 256 colours on purpose. It's plenty for reading a screen and it keeps the
link free.

**A game doesn't show up in the screenshot.**
`amigentd` reads the frontmost Intuition screen. Software that takes over the
hardware won't be in there.
