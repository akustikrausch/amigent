# amigentd protocol

TCP port 6801. The client sends commands, the service answers each one once.
All numbers are big endian.

## starting

    amigentd [PORT <n>] [SETUP] [KEY <file>]

`PORT` picks another port, handy for running a second copy next to the first.
`SETUP` switches on PUT and RUN, see below. `KEY` names the key file, default
`S:amigent.key`, see [key](#key). The service runs at priority 2, waits at boot
until `bsdsocket.library` is there, and quits on Ctrl-C.

What it needs: a 68020 or better (the code also builds for a 68000 with
`build-amigentd CPU 68000`), OS 3.0 or newer, and a TCP stack with
`bsdsocket.library` 4. Graphics cards are optional: chipset screens are read
through `graphics.library`, `cybergraphics.library` is only used when it is there.

## connections

One client at a time. A new connection always wins: the old one is reset at once,
even in the middle of a frame, and whatever was still queued for it is thrown
away. Mouse buttons still held by the old client are let go.

A client that sends nothing for 120 seconds is dropped. A single send or receive
that stalls for 30 seconds drops it too.

## hello

Service to client, 8 bytes: `AMGT`, `u16 version`, `u16 flags`. The version is
the protocol version, currently `2`. Flags: `1` when a key is required (see
[key](#key)), otherwise `0`.

## key

If the key file exists and its first line is not empty, every connection has to
send AUTH before anything else. The key is that first line, at most 64 bytes,
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
| 2 | FRAME | 1 = everything | region | frame |
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
| 13 | FRAME_RGB | | region | rgb |
| 14 | MOVE_REL | | x = dx, y = dy, signed | ok |
| 15 | UITREE | | | text |
| 16 | MENUS | | | text |
| 17 | AUTH | | x = key length | ok |
| 18 | CLIENT | | x = id high word, y = id low word | ok |

ok is a single byte, `0` for success and `1` for failure. Unknown commands get a
`1` as well. PUT sends the path and then the data right after the command, RUN
sends the command line. Both work only in `SETUP` mode.

## info

`u16 width`, `u16 height`, `u16 tile size`, `u16 reserved`. Size of the front
screen, tile size `64`, reserved `0`.

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

## screens

`u16 count`, then for each screen, front to back: `u32 handle`, `u16 width`,
`u16 height`, `u16 depth`, `u16 flags`, `u16 title length`, title (Latin-1).
Flags: `1` frontmost, `2` Workbench screen, `4` graphics card (RTG), `8` HAM. The
title is the screen's default title, or its current one if it has none. The
handle is the screen's address; it means nothing but the screen, and only as
long as that screen stays open.

SCREEN_FRONT brings the screen with that handle to the front, after checking that
it is still open. `1` if it isn't. The next FRAME then reads everything fresh.

## rgb

FRAME_RGB answers `u16 w`, `u16 h`, then `w*h*3` bytes RGB888, rows top to
bottom. The region is clipped to the front screen like FRAME's, `w` or `h` of `0`
reaching to the edge. A region wider or taller than 512 pixels after clipping
gets `w` = `h` = `0` and no data. It reads straight from the screen and leaves
FRAME's picture and reference alone. Chipset and HAM screens come in full 24 bit.

## text

UITREE and MENUS answer `u32 length`, then Latin-1 text, one record per line,
fields separated by tabs, coordinates in pixels of the screen. Tabs and line ends
inside names become spaces.

UITREE describes the front screen:

    S  handle  width  height  title
    W  window  x  y  w  h  flags  title
    G  window  gadget  x  y  w  h  type  flags  label

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
  right button, move, and let go over the item.

## setup

- PUT writes the file in one go. If it can't (no SETUP mode, bad path, disk
  full) the service still reads all the data, answers `1` and removes any half
  written file.
- RUN runs the line like a Shell would, with input and output on `NIL:`. The
  reply is the return code, `255` if the line didn't run at all or SETUP is off.
- SETUP_END leaves SETUP mode and answers `0`. The service keeps running as a
  plain screen service.
