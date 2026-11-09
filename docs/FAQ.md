# faq

**`amigent-setup` can't find my Amiga.**
Check that `amigentd SETUP` is running, then give it the address directly:
`amigent-setup 192.168.1.50`. The search only covers your own /24.

**How do I get amigentd onto the Amiga without a USB stick?**
Run `amigent-setup --serve` on the PC. It prints an address; open it in the
Amiga's browser and save `amigentd` to `RAM:`, or fetch it with wget as shown.
Files saved from a browser may lack the execute bit: `Protect RAM:amigentd +e`.
The first time, Windows may ask whether Python may accept connections.

**What runs on the Amiga?**
Only `amigentd`, started from `S:User-Startup`. It handles the screen, commands
and files on one port. Older installs also had amigactld; it keeps working.

**How do I see on the Amiga who is connected?**
Double-click `amigent-status` in the Amigent folder (`SYS:amigent` after setup).
Its window shows amigentd's version, uptime, encryption and every connection
with its address, refreshed every 2 seconds. Scripts get the same from the
ARexx port `AMIGENT`: `STATUS`, `CLIENTS`, `VERSION`, `PAUSE`, `RESUME`,
`ACTIVATEWINDOW`, `TYPE`, `KEY`, `BUSY`, `HELP` and `QUIT`.

**Can I watch on the Amiga what the agent does?**
Double-click `amigent-monitor` in the Amigent folder. Its window lists every tool
the agent calls and the commands, files, clicks and keys that reach amigentd, as
they happen. It never takes the input focus, so the agent's typing still goes where
it should. Beside the log it shows the data rates of the last minute, totals and how
much the agent did.

**Does Amigent get along with other AI bridges, such as amimcp?**
They use other ports, so both can be installed. What they share is the one mouse and
keyboard: if two agents drive the Amiga at the same time, clicks land elsewhere and
typing mixes. `amiga_status` says when such a program runs (it knows amimcp's
amiagent), and the agent is told once before it clicks or types. Amigent never
stops or removes the other one.

**Can I start amigentd from Workbench?**
Yes, from the icon in the archive. Its tool types work like the Shell options:
`SETUP`, `KEY=S:amigent.key`, `PORT=6801`, `QUIET`. Commands started through it
use the Workbench's command path, so `rx` and the like are found as in a
Shell.

**Does it run on OS 2.04?**
Yes, from Kickstart 2.04 on: commands, files, the remote desktop and the ARexx
port. Encryption needs OS 3.0 with AmiSSL. On a 1 MB machine the chipset screen
goes out as bitplanes, which needs no extra memory; the full-colour region tool
may refuse there for lack of memory. Workbench 2.04 has no `C:Reboot`, so
`amiga_restart` says so instead of restarting; copy `C:Reboot` over from a newer
Workbench, or restart by hand.

**What about PowerPC: AmigaOS 4, MorphOS, or a PPC board?**
A PowerPC board (WarpOS or PowerUP) on a classic Amiga is fine: amigentd runs on
the 68k side as usual, and Amigent names the PowerPC software it finds, so the
agent can pick the matching version of a program. AmigaOS 4 and MorphOS run
amigentd only through their 68k emulation. Commands, files, mouse and keys
should work there, the screen text probably not; it is untested, and Amigent
says so when it meets one of them.

**Does it run on a 68000?**
Use `amigentd.000`; `amigent-setup` picks it by itself. On a 7 MHz 68000 a whole
640×256 Workbench screen takes about 0.6 s, an unchanged one 80 ms, and
watching costs the Amiga under 10 % of its CPU.

**The AI agent waits for a command.**
A command holds its channel until it ends. Long work belongs in `amiga_job`,
which runs at low priority and keeps the line free.

**Is the connection encrypted?**
If `amigent-setup` found `openssl` on the PC and AmiSSL is installed on the
Amiga, yes: it then says so at the end. Run it again after installing either one.
On Windows, Amigent finds the `openssl` that comes with Git for Windows. `AMIGENT_TLS=off`
switches encryption off for a test.

**A second computer says amigentd takes only encrypted connections.**
That Amiga was set up for TLS only. Copy the files for it from
`~/.config/amigent/` on the computer that set it up (the key and
`tls-<name>.pem`) to the same folder on the other one, or run `amigent-setup`
there again with `amigentd SETUP` running.

**How do I update?**
Both halves, the PC and the Amiga. Paste this into your AI agent:

    Update Amigent for me, both halves: run "pipx upgrade amigent" on this
    computer, then "amigent-setup --update" for the amigentd on my Amiga.
    Then tell me to restart you, so you get the new tools.

By hand: [Updating](GUIDE.md#updating) in the guide. Updated only the Amiga,
with *Amigent-Update*? The PC says so from 1.3.0 on; run the prompt anyway.

**How do I add a second Amiga?**
Run `amigent-setup --name A500` with its address. Each Amiga gets its own key and
shows up in `amigent-fleet`. The AI agent works with one Amiga per MCP server
entry, so register a second entry with `AMIGENT_HOST=A500`.

**The Amiga got a new address.**
Nothing to do: Amigent searches the home network and takes the Amiga with your
certificate or key. In another network, type the address into the field the
remote desktop shows, or tell it to the AI agent.

**Directory Opus says it has waited long enough for a program.**
Something in `SYS:WBStartup` never exits. Services go into `S:User-Startup`.

**Files are gone after pulling the plug.**
PFS3 commits lazily, so the last minutes can be lost, while the volume stays fine.
Use `amiga_restart`, which commits first. Or yourself: `Lock SYS: ON`, `Lock SYS: OFF`.

**No network after a boot.**
Some setups need a cold start: 30 seconds without power. Set `AMIGENT_POWER_CMD`
to a script for your smart plug and `amiga_power('cycle')` does it for you.
With it set, `amiga_restart` also cycles the power by itself when the Amiga has
not answered two minutes after a restart.

**How do I add a program to the AmiStart menu?**

    rx menu-add.rexx AUDIO|My App|SYS:Utilities/MyApp/MyApp

Section, name, program, and optionally the stack size. It goes in sorted by name.
Don't use the AmiStart ARexx `SAVE` command for this: it rewrites the whole file.

**Does the AI agent need a screenshot for everything?**
No. From 1.3.0 amigentd records the text programs draw, and the agent reads
it with `amiga_screen_text`: a Shell's output or a requester's message costs a
few hundred bytes instead of a picture. It takes a screenshot for layout and
graphics, and when the text says something is missing. Start amigentd with
`NOTEXT` to leave the recording out.

**Is the network faster with packing?**
When the Amiga packs much faster than its network carries, yes: with a
PiStorm accelerator behind a plipbox, command output came 11 times as fast and a
whole 1920x1080 screen 2.4 times. A plain 68000 stays unpacked, it would
only slow things down. Amigent measures both sides and decides by itself;
`amiga_link` shows the numbers, `AMIGENT_COMPRESS=off` (or `fast`, `best`)
decides it by hand.

**Can an installation be taken back?**
Ask the agent to make it undoable: `amiga_install_begin` records the system
folders and copies them aside on the Amiga, `amiga_install_end` lists what
came and changed, `amiga_install_undo` puts it all back.

**Screenshots of a graphics-card screen look a bit rough.**
They're 256 colours on purpose. It's plenty for reading a screen and keeps the
link free. Chipset screens come in their exact colours, and
`amiga_screenshot_color` fetches any region in full colour.

**A game doesn't show up in the screenshot.**
`amigentd` reads the frontmost Intuition screen. Software that takes over the
hardware won't be in there.

**The viewer window doesn't show up.**
The AI agent gets the address as well. Open it in any browser on the same PC. Under
WSL the window is started on the Windows side, which needs Edge (it's always
there) and localhost forwarding, which WSL does by default.

**How do I press the Amiga keys in the viewer?**
The side panel has them as sticky buttons, plus the usual combinations ready to
click. In full screen the browser hands over the Windows keys: left Windows is
left Amiga, right Windows or the menu key is right Amiga. On a Mac both Cmd keys
are right Amiga, so Cmd+O and the like work as Amiga shortcuts.

**How do I copy text between the PC and the Amiga?**
Ctrl+V (Cmd+V on a Mac) in the remote desktop types the PC's clipboard into the
active window.
The *Clipboard* buttons in the side panel copy the clipboard itself in either
direction; on the Amiga it is pasted with Right Amiga+V. Copying text out of a
Shell window needs ConClip running, which Workbench starts by default.

**Ctrl+W closed the viewer.**
Browsers keep a few shortcuts for themselves outside full screen. Go full screen,
or use the sticky Ctrl button.

**The mouse pointer in the viewer is my PC's.**
That's on purpose. The Amiga's pointer is a sprite and not part of the screen
picture, and your own pointer reacts without waiting for the network.

**Can I run `amigent-viewer` while the AI agent is working?**
It's fine: a second start opens the window of the viewer that already runs. If
the agent is connected but hasn't opened the viewer yet, better ask the agent to
open it, so both share one connection to the screen service.

**Keys in a game don't work.**
Switch the keyboard to *Direct*. Text mode presses and releases every character
at once, which is right for typing and wrong for games.

**Where does Amigent live on Windows?**
The shortcuts are in the Start menu and on the desktop, the launcher, icon and
uninstaller in `%LOCALAPPDATA%\amigent`, the entry for *Installed apps* in
`HKEY_CURRENT_USER`. No admin rights needed. Settings such as `AMIGENT_FLUSH_VOLUMES` are taken over
from the shell you ran `--install` in. Changed something? Run `--install` again.

**Where does Amigent live on macOS?**
`~/Applications/Amigent.app`, with its settings in `~/.config/amigent/app.json`.
`amigent-viewer --uninstall` removes both.

**Amigent on the Mac says the Amiga does not answer, but Terminal reaches it.**
macOS asks each program once for the local network. Allow Amigent in System
Settings → Privacy & Security → Local Network.

**Amigent does nothing when I click it.**
Run `amigent-viewer <address>` in a terminal once and read what it says. Under
WSL the shortcut starts your default distribution's Python, so Amigent must be
installed there.

**"amigentd refused the key" or "amigentd wants a key".**
The key on the PC (in `~/.config/amigent/`, or `AMIGENT_KEY`) must
match `S:amigent.key` on the Amiga. Copy one over the other, or run `amigent-setup`
again with `amigentd SETUP` running. Deleting `S:amigent.key` switches the key
off.

**amigactld no longer answers this PC.**
Only for older installs that still use amigactld: it only accepts the addresses in `S:amigactld.conf`. If your PC got a new
address, add an `ALLOW` line for it there (or delete the file) and restart the
Amiga. `amigent-setup` keeps a copy in its folder on the Amiga.

**After a right click nothing opens any more.**
On a Directory Opus desktop a short right click leaves Opus' popup menu open,
and Intuition holds back every new window and screen until it is closed. Press
Esc. Menus are opened by holding the right button, moving and releasing. From
1.3.0 `amiga_ui_tree` says when a menu or such a popup holds things up, and
`amiga_start` closes the popup with Esc before it starts a program.

**Which programs give their buttons a name in amiga_ui_tree?**
Classic Intuition gadgets and GadTools checkboxes, string gadgets and sliders.
BOOPSI, ReAction and MUI gadgets, and requesters changed by tools such as
ReqAttack, show up with their box but without a label. The agent then gets a
small picture of each one with its number and clicks by number.
