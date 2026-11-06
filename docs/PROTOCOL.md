# amigentd protocol

TCP port 6801. The client sends commands, the service answers each one once.
All numbers are big endian. One connection shows the screen and takes mouse and
keyboard; up to four more run commands and move files, see
[command channels](#command-channels).

## starting

    amigentd [PORT <n>] [SETUP] [KEY <file>] [QUIET] [TLSONLY] [NOTEXT] [NODEBUG]

Running, it answers on the ARexx port `AMIGENT`, see [arexx](#arexx).

`PORT` picks another port, handy for running a second copy next to the first.
`SETUP` switches on PUT and RUN, see below. `KEY` names the key file, default
`S:amigent.key`, see [key](#key). `QUIET` keeps it from saying anything, errors
included. `TLSONLY` closes the plain port to other machines while the TLS port is
open, see [TLS](#tls). `NOTEXT` and `NODEBUG` leave out recording the text
programs draw and their debug output, see [hooks](#hooks). The service runs at priority 2, waits at boot until
`bsdsocket.library` is there, and quits on Ctrl-C.

Started from Workbench, the same come from the icon's tool types: `PORT=<n>`,
`SETUP`, `KEY=<file>`, `QUIET`, `TLSONLY`, `NOTEXT`, `NODEBUG`. Tool types in brackets, like `(SETUP)`, are
off. When it can't start (no OS 2.04, port taken, out of memory) it says so in a
requester, unless `QUIET` is set. A port that is taken is tried for a minute
first, as at boot.

What it needs: a 68020 or better for `bin/amigentd`, any 68000 for
`bin/amigentd.000` (built with `build-amigentd CPU 68000 TO amigentd.000`; no
68020 instructions, no word or long access at odd addresses), and for a Vampire
`bin/amigentd.080`, built by vbcc for the 68080 (`CPU 68080`); that one is not
tried on a real Vampire yet, `bin/amigentd` runs there as well, OS 2.04 or newer, and a TCP stack with
`bsdsocket.library` 4. Graphics cards are optional: chipset screens are read
through `graphics.library`, `cybergraphics.library` is only used when it is there. AmiSSL 5 is
only needed for [TLS](#tls).

On Kickstart 2.04 (V37) everything works that doesn't need a graphics card or
AmiSSL: key, command channels, files, EXEC, ARexx, input, SCREENS, UITREE,
MENUS and [planar](#planar) frames, which are the way to go there. Colours come
with 4 bits a gun, as the chipset has them. The classic frame, format 2 and
FRAME_RGB read pixels by hand there and want memory: the classic picture is
two bytes a pixel of the whole screen, FRAME_RGB three bytes a pixel of its
region. When that isn't to be had, the classic frame answers with no tiles,
format 2 with width and height 0, FRAME_RGB with `0`×`0`. Measured on an A500+
(68000 at 7 MHz, Kickstart 37.175, Workbench 2.04, 1 MB chip, no fast memory,
640×256 with 4 colours): planar full frame 0.69 s, unchanged 0.17 s, a typed
line 424 bytes; FRAME_RGB 320×64 in 1.9 s; the classic frame did not fit with
two Shells open.

## connections

A new connection first gets the hello. It may send AUTH, and then decides what it
is with its next command:

- CHANNEL (20) makes it a [command channel](#command-channels).
- Any other command makes it the screen client. There is one screen client at a
  time, and the newcomer always wins: the old one is reset at once, even in the
  middle of a frame, and whatever was still queued for it is thrown away. Mouse
  buttons still held by the old client are let go. The command that decided it
  is then carried out as usual.

So a connection that only sends its key, or asks for a command channel, leaves
the screen client alone. Up to four connections can wait undecided; a fifth
pushes out the one waiting longest. One that stays undecided for 120 seconds is
dropped, and so is a key that takes longer than 5 seconds to arrive.

A screen client that sends nothing for 120 seconds is dropped. A single send or
receive that stalls for 30 seconds drops it too.

New connections are accepted and greeted while a frame is still going out, so a
busy screen client doesn't hold them up. On a slow link they still share the line
with it: the hello waits behind the frame data already queued.

## hello

Service to client, 8 bytes: `AMGT`, `u16 version`, `u16 flags`. The version is
the protocol version, currently `5`. Flags, added up: `1` when a key is required
(see [key](#key)), `2` when AmiSSL is open, so [TLS](#tls) works as soon as a
certificate is there, `4` when the plain port is closed to other machines
(`TLSONLY`): a plain connection from another machine is closed right after the
hello, use PORT+1. Over TLS the hello shows `4` as well, and the connection stays.

## key

If the key file exists and its first line is not empty, every connection has to
send AUTH before anything else, command channels included. The key is that first line, at most 64 bytes,
with trailing spaces and tabs cut. The file is read again for every new
connection, so a key that arrives by PUT during SETUP counts from the next
connection on.

AUTH (17) sends `x` = key length, then the key right after the command. The
reply is `0` on a match. A wrong key, or any other command before a successful
AUTH, gets `1`, and the connection is closed about one second later. The service
waits that second before it does anything else, so there is no faster way to
guess. The comparison takes the same time whatever matches.

Without a key file AUTH always answers `0` and nothing else changes, SETUP
included.

## command

Always 10 bytes: `u8 command`, `u8 arg`, `u16 x`, `u16 y`, `u16 w`, `u16 h`.

| nr | command | arg | fields | reply |
|---|---|---|---|---|
| 1 | INFO | | | info |
| 2 | FRAME | 1 = everything, 2 = planar welcome, 4 = more methods, 8/16 = deflate (protocol 5) | region | frame, or [planar](#planar) with 2 |
| 3 | MOVE | | x, y | ok |
| 4 | BUTTON | mask: 1 left, 2 right, 4 middle | | ok |
| 5 | RAWKEY | 1 = press | x = rawkey, y = qualifier | ok |
| 6 | PING | | | ok |
| 7 | CHAR | | x = Latin-1 char, y = extra qualifier | ok |
| 8 | PUT | | x = path length, w/h = size high/low | ok |
| 9 | RUN | | x = command length | return code |
| 10 | SETUP_END | | | ok |
| 11 | SCREENS | | | screens |
| 12 | SCREEN_FRONT | | x = handle high word, y = handle low word | ok |
| 13 | FRAME_RGB | 1 = packed | region | rgb |
| 14 | MOVE_REL | | x = dx, y = dy, signed | ok |
| 15 | UITREE | | | text |
| 16 | MENUS | | | text |
| 17 | AUTH | | x = key length | ok |
| 18 | CLIENT | | x = id high word, y = id low word | ok |
| 19 | PAUSE | | x = seconds, 0 ends the pause | ok |
| 20 | CHANNEL | | | ok |
| 21-36 | | | | see [command channels](#command-channels) |

ok is a single byte, `0` for success and `1` for failure. Unknown commands get a
`1` as well. PUT sends the path and then the data right after the command, RUN
sends the command line. Both work only in `SETUP` mode.

## info

`u16 width`, `u16 height`, `u16 tile size`, `u16 paused`. Size of the front
screen, tile size `64`, and from protocol 4 the seconds the screen pause still
runs, rounded up; `0` without a pause. Before protocol 4 that field was always
`0`.

## pause

PAUSE (19, protocol 4) stops all screen reading for `x` seconds, at most 65535:
until then every FRAME and FRAME_RGB is answered as if its region lay off the
screen, so it carries no tiles, and nothing reads the screen in the background
either. A test or benchmark on the Amiga then has the CPU and the chip bus to
itself. Mouse, keys, SCREENS, UITREE, MENUS and the command channels go on as
usual. A new PAUSE replaces the running one, `x` = 0 ends it. The pause belongs
to the Amiga, not to a connection: it holds for every client and outlives the
one that set it. It works on a command channel too, and through ARexx (PAUSE,
RESUME). INFO, SYSINFO (`screen_paused`) and the ARexx STATUS (`paused`) tell
the seconds left. After the pause the next frame sends what changed meanwhile.

## frame

`u32 tile count`, then for each changed tile:
`u16 x`, `u16 y`, `u16 w`, `u16 h`, `u32 bytes`, data.

Data is RGB332, row by row, as pairs of `u8 length-1`, `u8 colour`. If packing
doesn't help, `bytes` equals `w*h` and the data is raw.

The service remembers the last frame across connections, so a dropped link doesn't
cost a full transfer. A client without a picture asks with arg 1.

That only works while one client gets all the frames. CLIENT tells which one is
asking: right after connecting, a client sends an id it picked at random, the
same for as long as it keeps its picture. The reply is `0` if the frames since
its last one all went to it, so its picture matches and arg 0 is safe, and `1`
otherwise: then it throws its picture away and asks with arg 1. A connection
that never sends CLIENT can't own the picture; anyone who gets changes after it
hears `1`.

The fine print:

- Tiles sit on a fixed 64 pixel grid starting at 0,0. A frame holds every tile
  that touches the region. Tiles at the right and bottom edge are smaller when
  the screen size isn't a multiple of 64.
- `w` or `h` of `0` reach to the edge of the screen. The region is clipped to the
  screen; a region off screen gives zero tiles.
- Colour byte layout: red in bits 0-2, green in bits 3-5, blue in bits 6-7.
- A run is at most 256 pixels and may carry on into the next row of the tile.
  Packed data is always shorter than `w*h`, so the length alone tells raw from
  packed.
- "Changed" means different from what the service last sent, compared in RGB332.
  A tile counts as sent once it is queued, whichever client got it.
- After each frame the service reads the same tiles again in the background, at
  priority -1, so only spare time goes into it. The next frame then just checks
  what changed, and a quiet screen answers in about one network round trip.
  Such an answer can show the screen up to half a second old. Fresh reads
  happen for arg 1, for tiles older than that, and for everything read less
  than a quarter of a second after the last MOVE, BUTTON, RAWKEY or CHAR, so a
  look right after a click shows what the click did.
- A new screen size (another screen in front) starts over: the next frame sends
  every tile.
- Screens on the chipset are read through their palette, HAM6 and HAM8
  included.

## planar

FRAME with arg bit `2` (so `2`, or `3` for everything) says the client can take
the bitplanes as they are. For a chipset screen with 1 to 8 planes that is what
it gets; for anything else (graphics card, more than 8 planes) the classic frame.
The reply starts with a format byte:

- `0`: a classic [frame](#frame) follows, exactly as for arg 0 or 1.
- `1`: planar. `u8 depth`, `u8 flags`, `u8 0`, `u16 width`, `u16 height`, then,
  with flag `2`, the palette: `u16 count`, `count` times `u8 r`, `u8 g`, `u8 b`.
  Then `u32 tile count`, and for each tile `u16 x`, `u16 y`, `u16 w`, `u16 h`,
  `u8 method`, `u32 bytes`, data.

Flags: `1` HAM (depth 6 or 8), `2` palette follows, `4` Extra Halfbrite (depth 6,
32 colours sent; pens 32-63 are the same colours at half brightness). The palette
comes with arg 1, on a new screen, and whenever it changed; keep it otherwise.

A tile unpacks to `depth` planes, plane 0 first; each plane is `h` rows of
`(w+7)/8` bytes, leftmost pixel in the top bit, pen bit `p` from plane `p`.
Methods:

- `0`: raw, those bytes as they are.
- `1`: PackBits. A byte `n` below 128 is followed by `n+1` literal bytes; above
  128 by one byte that repeats `257-n` times; 128 is never sent.
- `2`: PackBits of the XOR with the tile the client already has: unpack, then
  XOR into the old bytes. Only sent for tiles the client got before.

HAM pixels are worked out along the whole row from the left edge, starting from
colour 0, so a client keeps all planes and decodes rows across tile borders.

With arg bit `4` as well (`6`, or `7` for everything) the method byte is a set of
bits, and the service picks whatever comes out shortest for each tile:

- bits 0-1: `0` raw, `1` PackBits, `2` PackBits of an XOR with the old picture.
- `4`: row XOR. After unpacking, each row was XORed with the row above it in the
  same plane; undo it from the top: `row[y] ^= row[y-1]`, per plane.
- `8`: shifted (only with `2`). The data start with an `s16 dy`, then the PackBits.
  The XOR partner is not the tile's own old rows but the old picture `dy` rows
  further down, same columns: row `y` of the tile pairs with old row `y+dy`.
  Rows outside the screen count as zeros. That is a scrolled window.

The order to undo: unpack, undo the row XOR, then XOR with the partner. "Old
picture" always means the picture as it was before this frame, also for tiles
earlier in the same frame, so a client copies its planes when a frame starts and
takes the partners from that copy. Methods `0`, `1` and `2` mean exactly what
they meant without bit `4`.

With bit `4`, screens that aren't planar (graphics cards, more than 8 planes)
answer in format `2` instead of `0`:

- `2`: chunky. `u8 8`, `u8 0`, `u8 0`, `u16 width`, `u16 height`, `u32 tile
  count`, then tiles as in planar: `u16 x`, `u16 y`, `u16 w`, `u16 h`,
  `u8 method`, `u32 bytes`, data. A tile is `h` rows of `w` RGB332 bytes, the
  colour byte of the classic [frame](#frame). No palette.

The method bits are the same, a row being `w` bytes, plus:

- `16`: indexed, only with `0` or `1` in bits 0-1. The data start with `u8 k`
  (1 to 16) and `k` RGB332 colours; the rest is the tile as indices into them,
  `1` bit a pixel for up to 2 colours, `2` for up to 4, `4` for up to 16, the
  leftmost pixel in the top bits, each row starting on a new byte. Bits 0-1 and
  `4` apply to these index rows: unpack, undo the row XOR, then look up.

From protocol 5, arg bit `8` (fast) or `16` (thorough) with bit `4` lets the
service send a tile deflated when that comes out shorter: method bit `32`. The
data are then a raw deflate stream (RFC 1951) of exactly what the other method
bits describe; inflate it first, then go on as usual. Planar and chunky alike.
Worth it only when the Amiga packs much faster than the line carries; SYSINFO
tells how fast it packs.

Format `2` shares its picture with the classic frame (CLIENT and arg 1 work the
same way), and like that one reads ahead in the background. A desktop tile has
a handful of colours more often than not: the 640×256 Workbench above came to
5.7 instead of 11.4 KB. A 1920×1080 graphics card Workbench over plipbox took
87.6 KB and 3.0 to 4.7 s, against 127.8 KB and 9.6 to 13.1 s for the classic
frame, measured in turns.

The service looks for a scroll with one changed row of a tile the client
already has and checks that eight rows in a row agree; it looks up to 256 rows
either way. Measured on Workbench 3.1, 640×256 with 4 colours: a full frame with
two Shell windows went from 10.7 to 5.0 KB, a Shell that scrolled by one line
from 5.7 to 1.9 KB. Fresh text gains little, it is information after all.

What the planar path does differently, besides the format:

- No reading ahead: the service compares the planes in chip memory against what
  the client has, when asked, and sends what differs. A quiet screen costs one
  pass over the planes; between frames nothing runs at all.
- Skipping unchanged tiles and XOR need the client to hold the picture: the
  same connection, or the same CLIENT id. Anyone else gets everything, arg 1 or
  not. Classic and planar frames keep separate pictures.
- Tiles around the pointer come first, then the rest top to bottom.
- Sending starts once 8 KB are packed, while later tiles are still being packed.
  Not earlier: a slow link pays for every packet, and small sends make half
  empty ones.

Measured on a 68000 at 7 MHz (WinUAE, cycle exact, Workbench 3.1, 640×256 with
4 colours): a full frame went from 15.4 s to 0.64 s and from 8484 to 7136 bytes, an
unchanged screen from 13.1 s to 80 ms, a line of typed text 160-330 bytes.

## input during a reply

MOVE, BUTTON, RAWKEY, CHAR and MOVE_REL that arrive while a reply is still
going out (a frame, say) are carried out right away, between two tiles. Their
answers follow that reply, in the order the commands came, so the wire looks
the same as before; only the pointer moves sooner. Any other command waits its
turn as usual.

## screens

`u16 count`, then for each screen, front to back: `u32 handle`, `u16 width`,
`u16 height`, `u16 depth`, `u16 flags`, `u16 title length`, title (Latin-1).
Flags: `1` frontmost, `2` Workbench screen, `4` graphics card (RTG), `8` HAM. The
title is the screen's default title, or its current one if it has none. The
handle is the screen's address; it means nothing but the screen, and only as
long as that screen stays open.

SCREEN_FRONT brings the screen with that handle to the front, after checking that
it is still open. `1` if it isn't. The next FRAME then reads everything fresh.
The answer comes at once; while a menu is open, Intuition only switches screens
once it closes.

## rgb

FRAME_RGB answers `u16 w`, `u16 h`, then `w*h*3` bytes RGB888, rows top to
bottom. The region is clipped to the front screen like FRAME's, `w` or `h` of `0`
reaching to the edge. A region of more than 512×512 = 262144 pixels after
clipping gets `w` = `h` = `0` and no data; any shape that fits counts, 1920×136
as well as 700×300. It reads straight from the screen and leaves FRAME's
picture and reference alone. Chipset and HAM screens come in full 24 bit.

With arg 1 the pixels come packed: `u16 w`, `u16 h`, `u32 bytes`, data. The data
are runs of equal pixels, `u8 count-1`, `u8 r`, `u8 g`, `u8 b`, row by row over
the whole region; a run is at most 256 pixels and carries on into the next row.
If packing doesn't make it shorter, as with gradients and photos, `bytes` is
`w*h*3` and the data is raw RGB888 as with arg 0. So the length alone tells which
it is, and nothing ever gets bigger than raw. Text and windows mostly pack to a
tenth or less: a 512×512 piece of the desktop went from 786432 to 47080 bytes.

## text

UITREE and MENUS answer `u32 length`, then Latin-1 text, one record per line,
fields separated by tabs, coordinates in pixels of the screen. Tabs and line ends
inside names become spaces.

UITREE describes the front screen:

    S  handle  width  height  title  held
    W  window  x  y  w  h  flags  title
    G  window  gadget  x  y  w  h  type  flags  label

- `held`, from amigentd 1.3.0: `m` while a menu is open (Intuition locks the
  screen's layers then, also while a window is dragged), `h` while Intuition
  holds back new windows and screens without such a lock, as behind the pop-up a
  short right click leaves open on a DOpus desktop; empty when neither. amigentd
  tells `h` from a ScreenToFront of the front screen, which changes nothing, by a
  helper process: it counts when the call hasn't come back after 0.4 s.
  Older amigentd send no such field.
- Windows come front to back. `window` is the window's address in 8 hex digits,
  `handle` the screen's, as in SCREENS. Window flags: `a` active, `b` backdrop.
- `gadget` counts the window's gadgets from `0`, in list order, system gadgets
  included. It stays the same while the window keeps its gadgets.
- Types: `button`, `check` (a toggling button), `string`, `prop`, `sys`, and
  `other` for everything else, which includes BOOPSI, ReAction and MUI gadgets.
  For `sys` the label says which: `close`, `depth`, `zoom`, `size`, `drag`,
  `sdepth`, `sdrag`.
- Flags: `d` disabled, `s` selected.
- The label comes from the gadget's text, a plain string, or an `itexticlass`
  image. Underscores that mark shortcuts are dropped. GadTools keeps the text of
  its buttons in a private image, so those labels stay empty.
- Boxes follow the gadget's relative flags. Requesters are not listed.
- Pop-up menus that are windows, like the one on the DOpus desktop, show up as
  windows without a title.

MENUS describes the menu strip of the active window:

    A  window  flags  title
    M  menu  x  y  w  h  flags  title
    I  menu  item  x  y  w  h  flags  key  text
    U  menu  item  sub  x  y  w  h  flags  key  text

- The `A` line names the active window; flag `r` means it takes the right button
  for itself (RMBTRAP), so its strip never shows. Without an active window the
  answer is empty.
- Numbers count from `0`, as Intuition does. Flags: `e` enabled, `c` checked.
  `key` is the Amiga-key shortcut, if any. Items drawn as images, separators
  among them, have the text `(image)`.
- Boxes are where Intuition draws them: titles in the screen's title bar, items
  under their title while that menu is open, sub items next to their item while
  that item is highlighted. Checked on OS 3.2.
- Programs that handle the right button themselves never show their strip as a
  bar, even without RMBTRAP; the DOpus desktop opens its own pop-up instead. Menu
  patches that draw menus elsewhere aren't seen either.

## input

All input goes to the front screen, through `input.device`, as if it came from a
real mouse and keyboard.

- MOVE places the pointer at screen pixels x, y.
- BUTTON sends the whole mask, not a change. The service presses and releases
  whatever differs from the last mask.
- RAWKEY passes rawkey and qualifier as they are, see `devices/inputevent.h`.
- CHAR turns the character into keys through the system keymap, dead keys
  included, and adds `y` to each key's qualifier. Answers `1` if the keymap has no
  way to type it.
- MOVE_REL moves the pointer by dx, dy as a raw mouse event with the buttons
  held so far, for programs that read mouse movement and ignore positions.
- A short right click on the DOpus desktop leaves its pop-up menu open. While a
  menu is open Intuition holds back every new window and screen, so programs
  seem to hang on opening. Esc (rawkey `0x45`) closes it. For menus, hold the
  right button, move, and let go over the item. UITREE's `held` field and the
  ARexx command BUSY tell when this is the case.

## command channels

A connection that sends CHANNEL (after AUTH, if a key is set) becomes a command
channel. It gets `0` and from then on runs in a process of its own on the Amiga:
it never takes over or holds up the screen client, a new screen client doesn't
drop it, and a long command on one channel doesn't stop the others. Up to four
channels can be open. A fifth pushes out the one that has been idle longest; if
all four are busy it gets `1` and is closed. Channels have no idle limit.

Requests use the same 10 byte header, followed by their payload. Every reply
starts with `u8 status`: `0` for success, followed by what the command returns;
otherwise `u16 length` and a Latin-1 error text, and nothing else. The text is
the AmigaDOS one for the error where there is one (in the Amiga's language, so
`Objekt nicht gefunden` on a German system). Status `2` means a timeout, `1`
everything else.

| nr | command | arg | fields | payload | success returns |
|---|---|---|---|---|---|
| 21 | EXEC | | x = command length, y = directory length, w = timeout in seconds, 0 = none | command, directory | `i32 return code`, `u32 length`, output |
| 22 | READ | | x = path length | path | `u32 size`, data |
| 23 | WRITE | 1 = append | x = path length, w/h = size high/low | path, data | |
| 24 | DIR | | x = path length | path | text |
| 25 | STAT | | x = path length | path | text |
| 26 | DELETE | | x = path length | path | |
| 27 | RENAME | | x = old length, y = new length | old, new | |
| 28 | MAKEDIR | | x = path length | path | |
| 29 | CHECKSUM | | x = path length | path | `u32 CRC-32` |
| 19 | PAUSE | | x = seconds, 0 ends it | | |
| 30 | SYSINFO | | | | text |
| 31 | VOLUMES | | | | text |
| 32 | TEXT | | | | text, see [screen text](#screen-text) |
| 33 | WAIT | what, see [wait](#wait) | x = pattern length, w = timeout in seconds | pattern | text |
| 34 | COMPRESS | 0 off, 1 fast, 2 best | | | |
| 35 | TREE | 1 = with CRC-32 | x = path length, w = most entries (0: 50000) | path | text |
| 36 | DEBUGLOG | | w/h = count to start from, high/low | | `u32 count`, `u32 length`, bytes |

PING and [PAUSE](#pause) work on a channel too. Other screen commands (1-18) get status `1`
and the text `not on a command channel`; for PUT, RUN and AUTH the service first
takes the data they carry.

Text is `u32 length`, then Latin-1 lines ending in a line feed, fields
separated by tabs.

- EXEC runs the line like a Shell would, through `SystemTags` in a process of
  its own, with input from `NIL:` and output and errors collected in a file in
  `T:`, which goes away afterwards. The command finds programs along the
  command path of the Shell that started amigentd (`Path`), so `rx` and the
  like work. Started from Workbench it takes the path of the Workbench process
  (the Shell that ran `LoadWB`); if that has none, `SYS:Utilities`, `SYS:Rexxc`,
  `SYS:System`, `S:`, `SYS:Prefs`, `SYS:WBStartup`, `SYS:Tools` and
  `SYS:Tools/Commodities`, those that exist. `C:` is always searched last. The directory is the
  current directory for the command; without one it is the one amigentd was
  started in. A volume that isn't there is an error at once, never an "insert
  volume" requester waiting for someone at the Amiga: amigentd, its channels
  and every command they run have `pr_WindowPtr` set to -1, from 1.3.0 on. A
  program that wants its requesters back sets its own. The output
  comes after the command has finished, all at once. Return code `-1` with a
  failure status: the line could not run at all. When the timeout runs out the
  command gets Ctrl-C and the reply is status `2`, text `timeout`; the command
  may keep running if it ignores the break, and its output is lost.
- READ sends the size first. If the file can't be read to the end after that,
  the channel is closed.
- WRITE takes all the data before it answers, even after an error. A new file
  (arg 0) is written under a temporary name in the same directory and only
  renamed when everything is there, so a broken transfer never leaves half a
  file; a file of the same name is replaced and its protection bits with it.
  Append (arg 1) creates the file if needed. A directory of that name is never
  touched.
- DIR lists the directory, one line per entry:
  `FILE` or `DIR`, name, size (`0` for directories), protection as 8 hex
  digits, date as `YYYY-MM-DD HH:MM:SS`. Soft links count as files. STAT is
  the same line for the object itself; for a volume the name is the volume's.
- DELETE removes one file or an empty directory. RENAME also moves between
  directories of one volume.
- CHECKSUM is the zlib CRC-32 of the whole file.
- SYSINFO has `key=value` lines: `chip_free`, `chip_total`, `chip_largest`,
  `fast_free`, `fast_total`, `fast_largest`, `total_free` in bytes, and
  `exec_version`, `kickstart` (exec's version and the Kickstart revision, such
  as `47.115`), `bsdsocket` and `graphics` as `version.revision`, `cpu` (68000,
  68010, 68020, 68030, 68040 or 68060, from the CPU bits exec knows) and
  `attn_flags`, exec's AttnFlags as a number, for bits newer than that list,
  and from protocol 4 `screen_paused`, the seconds of [pause](#pause) left.
  From amigentd 1.3.0 also `uptime`, the seconds since the Amiga started (its
  E clock), `daemon_uptime`, the seconds since amigentd started, and
  `last_alert`, exec's last alert in 8 hex digits, when it kept one from before
  the last reset. A client compares the uptimes with its last look to tell a
  restart of the Amiga or of amigentd from a network fault.
- VOLUMES has one line per mounted volume: name with colon, used, free,
  capacity in bytes (also past 4 GB), block size.
- From protocol 5 SYSINFO also has `deflate_fast`, `deflate_best` and
  `inflate`, how many KB a second this Amiga packs (levels 1 and 2) and
  unpacks, measured once on text-like data at the first SYSINFO; `text` and
  `debug`, `1` while amigentd records screen text and debug output.
- TREE lists a folder and everything below it in one answer, lines as DIR has
  them with the path below the folder as name (`sub/file`), parents before
  children; with arg 1 a CRC-32 in 8 hex digits follows each file (`-` when
  it can't be read). A folder that can't be listed gives `ERR`, its path and
  the error number; more than the most entries end with a line `MORE`.
- WRITE with arg bit `2` takes a raw deflate stream (RFC 1951, any block
  type) of `w/h` bytes and writes what it unpacks to; bad data are refused
  with the text `bad compressed data`, the stream is taken in full anyway.

### packed replies

After COMPRESS with level 1 or 2, the bulk of these replies comes packed: the
data of READ, the output of EXEC and every text reply (DIR, STAT, SYSINFO,
VOLUMES, TEXT, WAIT, TREE). The length field before them still gives the
unpacked length; what follows is one raw deflate stream in frames, `u32
length` and that many bytes, ended by a frame of length `0`. Status, error
texts, CHECKSUM and DEBUGLOG never come packed. COMPRESS 0 goes back to plain
replies; the setting belongs to the channel.

### screen text

TEXT answers lines for the front screen:

    I  seconds_recorded  evicted  used  size
    W  window  x  y  w  h  flags  title
    L  window  x  y  w  h  text

`I` tells for how long amigentd records (`-1`: not at all), how many older
runs it dropped to make room, and its store. `W` lines come front to back as
in UITREE, flags `a` active, `b` backdrop, `o` open before recording started
(its text may be missing until it redraws). `L` lines are the rows of text
of the window above them, in screen pixels, top to bottom: runs on one line
close together are joined, ones further apart than twice the font height
stay separate rows with boxes of their own. Rows of a window behind another
are listed too; the client can tell from the `W` boxes which are covered.

Only what goes through graphics.library's Text() is seen: not text drawn as
pictures (many buttons, icons, games), not text a program renders into a
bitmap of its own and copies over, and not windows on other screens.

### wait

WAIT holds the channel until something happens on the Amiga, which looks
ten times a second; the line stays quiet meanwhile. arg:

- `1`: a window whose title matches the pattern is open (any screen); with
  `+8` only one that was not open when the wait began. The answer is its
  `W` line, flag `h` when it is on a screen behind the front one.
- `2`: no window matches any more. Empty answer.
- `3`: a row of the front screen's text contains the text, any case. The
  answer is its `L` line.
- `4`: no row contains it any more. Empty answer.

The pattern is an AmigaDOS pattern (`*` works as `#?`), or plain text that
may appear anywhere in the title, as for ACTIVATEWINDOW. At most 3600
seconds; when they run out the status is `2` with the text `timeout`.

### hooks

To see text and debug output amigentd patches graphics.library Text,
RectFill, EraseRect, SetRast, ScrollRaster and ScrollRasterBF,
layers.library DeleteLayer and exec's RawPutChar (as Sashimi does). Each
patch is a small stub in memory of its own: it saves all registers, calls
amigentd while it runs and goes on to the original. When amigentd quits it
puts the vectors back; if a program patched the same function later, the
stub stays in the chain and passes calls through. Stubs are never freed and
the next amigentd takes them over (a semaphore named `amigent hooks 1`).
Only one amigentd at a time records; a second one on another port takes
over once the first is gone. RawPutChar output still reaches the serial
port; DEBUGLOG keeps the last 64 KB. `NOTEXT` and `NODEBUG` leave the
patches out.

## tls

With AmiSSL 5 (`amisslmaster.library` 5) and the file `S:amigent-tls.pem`, the
service also listens on PORT+1 for TLS. Inside it is the same protocol, hello
first, key and command channels included. The file holds the certificate and
its private key, both PEM; an EC key (prime256v1) keeps the handshake quick,
about 0.2 s here. The service never makes keys itself, and doesn't check
clients: the client pins the certificate.

The file is looked at every 10 seconds: when it appears the TLS port opens, when
it changes it is loaded again (connections already open keep the old one), and
when it goes the port closes. Without AmiSSL, or with a file that doesn't load,
there is simply no TLS port; a file that doesn't load is tried again every 10
seconds. It may be written during SETUP: the port opens within about 10 seconds.
Up to eight TLS connections at once.

Each TLS connection is relayed to the plain port on 127.0.0.1, so it counts as
an ordinary connection there.

With `TLSONLY`, while the TLS port is open and amigentd is not in SETUP mode, a
new plain connection from any address but 127.0.0.1 gets the hello with flag `4`
and is closed at once. Connections already open stay. Without a certificate, or
without AmiSSL, there is no TLS port and the plain port works as usual, so a lost
certificate never locks you out. In SETUP mode the plain port stays open, so
`amigent-setup` can deliver the certificate; after SETUP_END new plain connections
from outside are turned away. TLS 1.2 and 1.3, as AmiSSL offers them.

## arexx

amigentd opens the ARexx port `AMIGENT`; a second copy on another port, while
the first has the name, gets `AMIGENT.<port>`, say `AMIGENT.6811`. No
`rexxsyslib.library`, no port; the rest runs regardless. Commands, any case:

| command | result |
|---|---|
| STATUS | `key=value` lines, see below |
| CLIENTS | a line per connection: kind, address, seconds connected, tab separated |
| VERSION | `amigentd 1.3.0 (06.11.2026)` |
| PAUSE [seconds] | stops screen reading for that long, see [pause](#pause); the seconds left, without a number just those |
| RESUME | ends the pause, `0` |
| ACTIVATEWINDOW pattern | brings the first window whose title matches to the front and gives it the keys; the title, return code 5 when none matched or a menu or pop-up holds Intuition up (see BUSY) |
| TYPE text | types the text through the Amiga's keymap, keys in angle brackets; return code 5 at the first unknown key or character |
| KEY name | presses one key, written as inside the brackets of TYPE |
| BUSY | `menu` while a menu is open, `held` while Intuition holds back new windows (a pop-up left open), empty when neither; the `held` field of [UITREE](#text) |
| HELP | the command names |
| QUIT | `bye`, then amigentd ends as on Ctrl-C |

STATUS: `version`, `protocol`, `port`, `uptime` in seconds, `tls`, `tls_only`,
`setup` and `key` as `on` or `off` (`tls` is on while the TLS port listens,
`key` while a key file is in force), `screen` (0 or 1), `channels`,
`tls_clients`, `pending` (connections that haven't said yet what they are),
`paused` (seconds of screen pause left).

CLIENTS kinds: `screen`, `channel`, `pending`, and `tls` for each TLS connection
with the address it came from. A TLS connection reaches the plain port through
127.0.0.1, so it shows twice: as `tls` with its real address, and as `screen`
or `channel` with `127.0.0.1`.

ACTIVATEWINDOW takes an AmigaDOS pattern, any case, with `*` as `#?`; plain
text without pattern characters matches any part of the title. Windows are
searched screen by screen, front screen first.

TYPE goes to the active window, as typed at the keyboard, so umlauts come out
as the keymap has them. Keys go in angle brackets: `<return>`, `<enter>` (the
number pad's), `<tab>`, `<esc>`, `<backspace>`, `<del>`, `<space>`, `<help>`,
`<up>` `<down>` `<left>` `<right>`, `<f1>` to `<f10>`, or one character, each
after qualifiers `shift`, `ctrl`, `alt`, `lamiga`, `ramiga` separated by spaces
or `+`: `<ctrl c>`, `<lamiga+q>`, `<shift tab>`. `<<` types a `<`. One pair of
quotes around the whole argument is taken off. Text typed ahead of a program
that is still busy waits in its input as it would from the keyboard.

Results come back with `options results`; an unknown command gives return code
10. The answer comes between two looks at the network, also between two tiles
of a frame going out.

    /* who is on? */
    options results
    address AMIGENT
    'CLIENTS'
    say result

`amigent-status` shows the same in a window and asks again every two seconds:
GadTools, OS 2.04 or newer, any 68000. It needs `rexxsyslib.library` but not
RexxMast. `amigent-status PORT AMIGENT.6811`, or the tool type `PORT=`, watches
a second copy. Esc or the close gadget ends it.

## setup

- PUT writes the file in one go. If it can't (no SETUP mode, bad path, disk
  full) the service still reads all the data, answers `1` and removes any half
  written file.
- RUN runs the line like a Shell would, with input and output on `NIL:`. The
  reply is the return code, `255` if the line didn't run at all or SETUP is off.
- SETUP_END leaves SETUP mode and answers `0`. The service keeps running as a
  plain screen service.
