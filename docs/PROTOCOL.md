# amigentd protocol

TCP port 6801. The client sends commands, the service answers each one once.
All numbers are big endian.

## starting

    amigentd [PORT <n>] [SETUP]

`PORT` picks another port, handy for running a second copy next to the first.
`SETUP` switches on PUT and RUN, see below. The service runs at priority 2, waits
at boot until `bsdsocket.library` and `cybergraphics.library` are there, and
quits on Ctrl-C.

## connections

One client at a time. A new connection always wins: the old one is closed, even
in the middle of a frame. Mouse buttons still held by the old client are let go.

A client that sends nothing for 120 seconds is dropped. A single send or receive
that stalls for 30 seconds drops it too.

## hello

Service to client, 8 bytes: `AMGT`, `u16 version`, `u16 reserved`. The version
is the protocol version, currently `1`. Reserved is `0`.

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
- A new screen size (another screen in front) starts over: the next frame sends
  every tile.
- Screens on the chipset are read through their palette. HAM is not decoded.

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

## setup

- PUT writes the file in one go. If it can't (no SETUP mode, bad path, disk
  full) the service still reads all the data, answers `1` and removes any half
  written file.
- RUN runs the line like a Shell would, with input and output on `NIL:`. The
  reply is the return code, `255` if the line didn't run at all or SETUP is off.
- SETUP_END leaves SETUP mode and answers `0`. The service keeps running as a
  plain screen service.
