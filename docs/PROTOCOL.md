# amigentd protocol

TCP port 6801. The client sends commands, the service answers each one once.
All numbers are big endian.

## hello

Service to client, 8 bytes: `AMGT`, `u16 version`, `u16 reserved`.

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

ok is a single byte, `0` for success. PUT sends the path and then the data right
after the command, RUN sends the command line. Both work only in `SETUP` mode.

## info

`u16 width`, `u16 height`, `u16 tile size`, `u16 reserved`.

## frame

`u32 tile count`, then for each changed tile:
`u16 x`, `u16 y`, `u16 w`, `u16 h`, `u32 bytes`, data.

Data is RGB332, row by row, as pairs of `u8 length-1`, `u8 colour`. If packing
doesn't help, `bytes` equals `w*h` and the data is raw.

The service remembers the last frame across connections, so a dropped link doesn't
cost a full transfer. A client without a picture asks with arg 1.
