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

**The viewer window doesn't show up.**
Claude prints the address as well. Open it in any browser on the same PC. Under
WSL the window is started on the Windows side, which needs Edge (it's always
there) and localhost forwarding, which WSL does by default.

**How do I press the Amiga keys in the viewer?**
The side panel has them as sticky buttons, plus the usual combinations ready to
click. In full screen the browser hands over the Windows keys: left Windows is
left Amiga, right Windows or the menu key is right Amiga.

**Ctrl+W closed the viewer.**
Browsers keep a few shortcuts for themselves outside full screen. Go full screen,
or use the sticky Ctrl button.

**The mouse pointer in the viewer is my PC's.**
That's on purpose. The Amiga's pointer is a sprite and not part of the screen
picture, and your own pointer reacts without waiting for the network.

**Can I run `amigent-viewer` while Claude is working?**
Better not. Both would fight over the screen service. While Claude Code is open,
ask Claude for the viewer instead: it shares one connection with Claude.

**Keys in a game don't work.**
Switch the keyboard to *Direct*. Text mode presses and releases every character
at once, which is right for typing and wrong for games.

**Where does Amigent live on Windows?**
The shortcuts are in the Start menu and on the desktop, the launcher, icon and
uninstaller in `%LOCALAPPDATA%\amigent`, the entry for *Installed apps* in
`HKEY_CURRENT_USER`. No admin rights needed. Settings such as `AMIGENT_FLUSH_VOLUMES` are taken over
from the shell you ran `--install` in. Changed something? Run `--install` again.

**Amigent does nothing when I click it.**
Run `amigent-viewer <address>` in a terminal once and read what it says. Under
WSL the shortcut starts your default distribution's Python, so amigent must be
installed there.
