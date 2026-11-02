"""The viewer's screen loop: input goes out at once, tiles come in as they trickle.

amigentd answers strictly in order, one command after the other. So the PC
keeps the line full instead of asking and waiting: commands go out back to
back on one Pipeline, input is written the moment it arrives, and frames are
asked for in pieces small enough that input never queues behind a fat one on
the Amiga either. Right after input the pieces around the pointer and the text
cursor go first; the rest of the screen follows when it is due.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import Counter, deque

from .amigentd import (CMD_BUTTON, CMD_CHAR, CMD_MOVE, CMD_MOVE_REL, CMD_RAWKEY, CMD_SCREEN_FRONT,
                       QUALIFIERS, RAWKEYS, Pipeline)

TILE = 64
REPEAT = 0x0200  # IEQUALIFIER_REPEAT
WHEEL_UP, WHEEL_DOWN = 0x7A, 0x7B  # NewMouse convention
ACTIVE = 2.0  # seconds after input that count as "someone is driving"
SWEEP_ACTIVE = 0.5  # full screen at most this often while driving: the focus gets the rest
FOLLOW_UP = 0.3  # a second look at the focus, for programs that take their time
IDLE_STEPS = (0.1, 0.15, 0.25, 0.35, 0.45)  # pause after quiet sweeps; under amigentd's 0.5 s read-ahead
DOZE_AFTER, DOZE = 15.0, 1.0  # nobody touched anything for long: let the Amiga breathe
DUTY = 0.5, 0.25  # share of the Amiga's time a sweep may take while awake, while dozing
FOCUS_KEEP = 10.0  # a tile that answered a key stays interesting this long
FOCUS_HALF_LIFE = 2.0  # seconds: old answers count half, the cursor moves on
FOCUS_BOXES = 2
ANIMATED_KEEP = 30.0  # a tile that moved by itself is suspect this long after its last move
ANIMATED_EVERY = 1.0  # while someone drives, the screen's own demo gets the leftovers
QUIET_HANDS = 3.0  # a change this long after any input is nobody's doing
BUDGET = 0.25  # seconds of wire a piece of screen may cost while someone drives
FRAMES_IN_FLIGHT = 2  # always allowed, however big they may be
MAX_FRAMES_IN_FLIGHT = 16  # cheap ones fill the round trip up to here
INPUTS_IN_FLIGHT = 64  # a long paste must not bury the frames behind it


# ---------------------------------------------------------------- input

class InputQueue:
    """Input from the pages, squeezed for a thin wire.

    The newest pointer position wins, relative moves and wheel clicks add up,
    and a held key never piles up repeats: one queued press is plenty, and
    letting go throws the queued repeats away. No runaway cursor after the key
    is up, the copper list stays short.
    """

    def __init__(self):
        self._ops: deque = deque()
        self._lock = threading.Lock()

    def put(self, op: dict) -> None:
        kind = op.get("t")
        with self._lock:
            last = self._ops[-1] if self._ops else {}
            if kind == "move" and last.get("t") == "move":
                self._ops[-1] = op
            elif kind == "rel" and last.get("t") == "rel":
                last["dx"] = int(last["dx"]) + int(op.get("dx", 0))
                last["dy"] = int(last["dy"]) + int(op.get("dy", 0))
            elif kind == "wheel" and last.get("t") == "wheel" and (int(last["dy"]) < 0) == (int(op.get("dy", 0)) < 0):
                last["dy"] = int(last["dy"]) + int(op.get("dy", 0))
            elif op.get("repeat") and any(press_of(o) == press_of(op) for o in self._ops):
                pass  # still one waiting: the Amiga hasn't even seen that one
            else:
                if kind == "key" and not op.get("down"):
                    self._ops = deque(o for o in self._ops
                                      if not (o.get("repeat") and press_of(o) == ("key", op.get("code"))))
                self._ops.append(op)

    def take(self, hold_moves: bool = False) -> list[dict]:
        """Everything queued, in order; stops at a move while one is still on its way."""
        out = []
        with self._lock:
            while self._ops:
                if hold_moves and self._ops[0].get("t") in ("move", "rel"):
                    break
                op = self._ops.popleft()
                out.append(op)
                hold_moves = hold_moves or op.get("t") in ("move", "rel")
        return out

    def __len__(self) -> int:
        return len(self._ops)


def press_of(op: dict):
    """What a key press presses, so repeats of it can be told apart; None for anything else."""
    kind = op.get("t")
    if kind == "key" and op.get("down"):
        return ("key", op.get("code"))
    if kind == "char":
        return ("char", op.get("c"))
    return None


def commands(op: dict, width: int, height: int) -> list[tuple]:
    """amigentd commands for one op from the page, as (cmd, arg, x, y); 'info' asks the size."""
    kind = op.get("t")
    if kind == "move":
        return [(CMD_MOVE, 0, max(0, min(int(op["x"]), width - 1)), max(0, min(int(op["y"]), height - 1)))]
    if kind == "buttons":
        return [(CMD_BUTTON, int(op["mask"]) & 7, 0, 0)]
    if kind == "key":
        q = int(op.get("q", 0)) | (REPEAT if op.get("repeat") else 0)
        return [(CMD_RAWKEY, int(bool(op["down"])), int(op["code"]) & 0x7F, q & 0xFFFF)]
    if kind == "char":
        c = str(op["c"])[:1]
        return [(CMD_CHAR, 0, ord(c), int(op.get("q", 0)) & 0xFFFF)] if c and ord(c) < 256 else []
    if kind == "text":
        out = []
        for ch in str(op["s"])[:20000].replace("\r\n", "\n"):
            if ch == "\n":
                out += [(CMD_RAWKEY, 1, RAWKEYS["return"], 0), (CMD_RAWKEY, 0, RAWKEYS["return"], 0)]
            elif ord(ch) < 256:
                out.append((CMD_CHAR, 0, ord(ch), 0))
        return out
    if kind == "combo":
        *mods, key = str(op["k"]).lower().split("+")
        q = 0
        for mod in mods:
            q |= QUALIFIERS[mod]
        if key in RAWKEYS:
            return [(CMD_RAWKEY, 1, RAWKEYS[key], q), (CMD_RAWKEY, 0, RAWKEYS[key], q)]
        return [(CMD_CHAR, 0, ord(key), q)] if len(key) == 1 and ord(key) < 256 else []
    if kind == "rel":
        dx = max(-32768, min(32767, int(op["dx"])))
        dy = max(-32768, min(32767, int(op["dy"])))
        return [(CMD_MOVE_REL, 0, dx & 0xFFFF, dy & 0xFFFF)]
    if kind == "screen":
        handle = int(op["handle"])
        return [(CMD_SCREEN_FRONT, 0, handle >> 16 & 0xFFFF, handle & 0xFFFF), "info"]
    if kind == "wheel":
        code = WHEEL_UP if int(op["dy"]) < 0 else WHEEL_DOWN
        return [(CMD_RAWKEY, d, code, 0) for _ in range(min(abs(int(op["dy"])), 5)) for d in (1, 0)]
    return []


# ---------------------------------------------------------------- where to look

def chunks(width: int, height: int, max_tiles: int, rows=None) -> list[tuple]:
    """The screen as (x, y, w, h) pieces of at most max_tiles tiles, top to bottom.

    rows limits it to those tile rows; a piece never spans a row left out.
    """
    cols, nrows = -(-width // TILE), -(-height // TILE)
    wanted = [ty for ty in range(nrows) if rows is None or ty in rows]
    out = []
    if cols >= max_tiles:
        for ty in wanted:
            for tx in range(0, cols, max_tiles):
                out.append((tx * TILE, ty * TILE, min(max_tiles * TILE, width - tx * TILE),
                            min(TILE, height - ty * TILE)))
        return out
    per = max(1, max_tiles // cols)
    run: list = []
    for ty in wanted + [None]:
        if run and (ty is None or ty != run[-1] + 1 or len(run) == per):
            out.append((0, run[0] * TILE, width, min(len(run) * TILE, height - run[0] * TILE)))
            run = []
        if ty is not None:
            run.append(ty)
    return out


def _box(width, height, tx0, ty0, tx1, ty1) -> tuple:
    """Tiles tx0..tx1, ty0..ty1 clipped to the screen, as pixels."""
    cols, rows = -(-width // TILE), -(-height // TILE)
    tx0, ty0 = max(0, tx0), max(0, ty0)
    tx1, ty1 = min(cols - 1, tx1), min(rows - 1, ty1)
    return (tx0 * TILE, ty0 * TILE, (tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE)


def _inside(a, b) -> bool:
    """Is box a within box b?"""
    return b[0] <= a[0] and b[1] <= a[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]


class Planner:
    """Decides which bit of screen to ask for next.

    Remembers where the action is: the pointer, and the tiles that changed
    right after a key (the text cursor, wherever the program keeps it).
    """

    def __init__(self):
        self.pointer = (0, 0)
        self.last_input = 0.0
        self.last_key = 0.0
        self.focus: dict = {}  # (x, y) of a tile -> (score, when it last answered a key)
        self.animated: dict = {}  # tiles that change on their own: clocks, logs, demos
        self.seen: dict = {}  # tile -> when it last came in changed
        self.keys: deque = deque(maxlen=64)  # when keys went out
        self.animated_polled = 0.0
        self.avg_tile = 800.0  # bytes per tile on the wire, smoothed
        self.rate = 20000.0  # bytes per second the wire managed, smoothed

    def note_input(self, op: dict, now: float) -> None:
        self.last_input = now
        kind = op.get("t")
        if kind == "move":
            self.pointer = (int(op.get("x", 0)), int(op.get("y", 0)))
        elif kind in ("key", "char", "text", "combo"):
            self.last_key = now
            self.keys.append(now)

    def _key_between(self, start: float, end: float) -> bool:
        return any(start < k <= end for k in self.keys)

    def note_tiles(self, tiles, now: float, first=frozenset()) -> None:
        """Tiles just in; those in first complete the picture and say nothing about change."""
        for x, y, w, h, data in tiles:
            self.avg_tile = self.avg_tile * 0.9 + (12 + len(data)) * 0.1
            t = (x, y)
            before, self.seen[t] = self.seen.get(t), now
            if t in first:
                continue
            if (now - self.last_input > QUIET_HANDS or now - self.animated.get(t, -1e9) < ANIMATED_KEEP
                    or (before is not None and not self._key_between(before, now)
                        and now - self.last_key < 1.5)):
                self.animated[t] = now  # moved without us, or twice for one key: not our cursor
                self.focus.pop(t, None)
            elif now - self.last_key < 1.5 and not self._near_pointer(x, y):
                score, at = self.focus.get(t, (0.0, now))
                self.focus[t] = (score * 0.5 ** ((now - at) / FOCUS_HALF_LIFE) + 1, now)

    def note_rate(self, nbytes: int, seconds: float) -> None:
        if nbytes >= 2048 and seconds > 0.02:
            self.rate = self.rate * 0.7 + (nbytes / seconds) * 0.3

    def _near_pointer(self, x, y) -> bool:
        px, py = self.pointer
        return abs(x // TILE - px // TILE) <= 1 and abs(y // TILE - py // TILE) <= 1

    def active(self, now: float) -> bool:
        return now - self.last_input < ACTIVE

    def priority(self, width: int, height: int, now: float) -> list[tuple]:
        """The pointer's neighbourhood and the text cursor's, smallest first."""
        if not width or not height:
            return []
        px, py = self.pointer
        out = [_box(width, height, px // TILE - 1, py // TILE - 1, px // TILE + 1, py // TILE + 1)]
        self.focus = {t: v for t, v in self.focus.items() if now - v[1] < FOCUS_KEEP}
        # the tiles that answered most keys lately, two in case a demo fooled us once;
        # the cursor walks right and down from there
        for fx, fy in sorted(self.focus, key=lambda t: -self._focus_score(t, now))[:FOCUS_BOXES]:
            box = _box(width, height, fx // TILE - 1, fy // TILE, fx // TILE + 1, fy // TILE + 1)
            if not any(_inside(box, b) for b in out):
                out.append(box)
        return out

    def _focus_score(self, t, now: float) -> float:
        score, at = self.focus[t]
        return score * 0.5 ** ((now - at) / FOCUS_HALF_LIFE)

    def sweep_rows(self, height: int, now: float) -> set | None:
        """Tile rows for the next sweep: while someone drives, animated rows only now and then."""
        if not self.active(now) or now - self.animated_polled >= ANIMATED_EVERY:
            self.animated_polled = now
            return None
        busy = {y // TILE for (x, y), at in self.animated.items() if now - at < ANIMATED_KEEP}
        return {ty for ty in range(-(-height // TILE)) if ty not in busy}

    def piece_tiles(self, active: bool, quiet: bool) -> int:
        """How many tiles one request may cover: everything while the screen is quiet."""
        if quiet and not active:
            return 1 << 16
        return max(2, int(self.rate * BUDGET / max(self.avg_tile, 64)))


# ---------------------------------------------------------------- the loop

class Feed:
    """Owns the screen connection while it works: one thread, one pipeline."""

    def __init__(self, screen, lock, inputs: InputQueue, planner: Planner | None = None,
                 watching=lambda: True, on_online=lambda online, reason: None,
                 on_screens=lambda found: None, on_refused=lambda text: None):
        self.screen = screen
        self.lock = lock
        self.inputs = inputs
        self.planner = planner or Planner()
        self.watching = watching
        self.on_online = on_online
        self.on_screens = on_screens
        self.on_refused = on_refused
        self.wake = threading.Event()
        self._bell_r, self._bell_w = socket.socketpair()
        self._bell_r.setblocking(False)
        self._bell_w.setblocking(False)
        self.want_screens = False
        self.rtt = 0.0  # ms, smoothed, from input answers that had nothing queued ahead
        self.online = None
        self._reset()

    def _reset(self) -> None:
        self.pipe: Pipeline | None = None
        self.sweep: deque = deque()  # pieces of the full screen still to ask for
        self.urgent: deque = deque()  # pieces around the pointer and cursor
        self.backlog: deque = deque()  # input commands not yet written
        self.next_sweep = 0.0
        self.follow_up = 0.0
        self.quiet = 0
        self.found = 0  # tiles found in the running sweep
        self.sweep_started = 0.0
        self.sweep_took = 0.0  # seconds the last quiet sweep needed, round trip and all
        self.in_flight = 0.0  # bytes the sweep frames on their way are expected to bring
        self.piece_bytes: dict = {}  # region -> what it brought last time
        self.sweeps = 0
        self.keys_out: Counter = Counter()

    def ring(self) -> None:
        """Input arrived: wake the loop, wherever it waits."""
        self.wake.set()
        try:
            self._bell_w.send(b"!")
        except OSError:
            pass  # bell full: it's ringing anyway

    def _silence(self) -> None:
        try:
            while self._bell_r.recv(512):
                pass
        except OSError:
            pass

    # what's due

    def _due(self, now: float) -> bool:
        return bool(len(self.inputs) or self.backlog or self.urgent or self.sweep
                    or self.want_screens or now >= self.next_sweep
                    or (self.follow_up and now >= self.follow_up))

    def run(self) -> None:
        retried = False
        while True:
            if not self.watching():
                self.wake.wait(1)
                self.wake.clear()
                continue
            now = time.monotonic()
            if not self._due(now):
                wait = self.next_sweep - now
                if self.follow_up:
                    wait = min(wait, self.follow_up - now)
                self.wake.wait(max(0.0, wait))
                self.wake.clear()
                continue
            try:
                with self.lock():
                    self._burst()
                retried = False
                self._set_online(True)
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                self._broken()
                if self.online and not retried:
                    retried = True  # someone may just have taken the screen for a moment
                    continue
                self._set_online(False, str(e) or type(e).__name__)
                self.wake.wait(2)
                self.wake.clear()

    def _set_online(self, online: bool, reason: str = "") -> None:
        if online != self.online:
            self.online = online
            self.on_online(online, reason)

    def _broken(self) -> None:
        with self.lock():
            if self.pipe and self.pipe.frames:
                self.screen.drop_picture()  # half a frame: amigentd counts it as ours
            self.screen.close()
        self._reset()

    # one burst: owns the connection until nothing is left in flight

    def _burst(self) -> None:
        if not self.screen._sock:
            self.screen.connect()
            start = time.perf_counter()
            self.screen.ping()  # one round trip to size the pipeline by
            self.rtt = (time.perf_counter() - start) * 1000
        self.pipe = Pipeline(self.screen, on_tiles=self._tiles)
        try:
            while True:
                self._silence()  # before looking: a ring after this wakes the pump
                now = time.monotonic()
                self._feed_inputs(now)
                self._plan(now)
                self._feed_frames()
                if not self.pipe.busy() and not self.backlog and not self.urgent and not self.sweep:
                    break
                self.pipe.pump(1.0, self._bell_r)
        finally:
            self.pipe.close()
            self.pipe = None

    def _feed_inputs(self, now: float) -> None:
        pipe = self.pipe
        ops = self.inputs.take(hold_moves=pipe.moves > 0)
        w, h = self.screen.width or 1, self.screen.height or 1
        for op in ops:
            try:
                cmds = commands(op, w, h)
            except (KeyError, ValueError, TypeError):
                continue  # malformed message from the page
            press = press_of(op)
            if op.get("repeat") and self.keys_out[press]:
                continue  # the last press of this key is still on its way: no pile-up
            if op.get("t") == "rel" and self.screen.protocol < 2:
                self.on_refused("relative mouse needs amigentd protocol 2")
                continue
            self.planner.note_input(op, now)
            self.backlog.extend((cmd, press) for cmd in cmds)
        if ops:
            self.urgent.clear()
            self.urgent.extend(self.planner.priority(self.screen.width, self.screen.height, now))
            self.follow_up = now + FOLLOW_UP
            self.quiet = 0
            # typing somewhere we haven't seen yet: look everywhere now, else soon
            soon = 0.0 if self.planner.last_key == now and not self.planner.focus else SWEEP_ACTIVE
            self.next_sweep = min(self.next_sweep, now + soon)
        while self.backlog and pipe.inputs < INPUTS_IN_FLIGHT:
            cmd, press = self.backlog.popleft()
            if cmd == "info":
                pipe.info()
                continue
            self.keys_out[press] += 1
            alone = not pipe.busy()
            pipe.command(*cmd, on_reply=lambda ok, took, press=press, alone=alone: self._answered(press, took, alone))

    def _answered(self, press, took: float, alone: bool) -> None:
        self.keys_out[press] -= 1
        if alone:
            self.rtt = self.rtt * 0.8 + took * 200  # ms, smoothed

    def _plan(self, now: float) -> None:
        w, h = self.screen.width, self.screen.height
        if self.follow_up and now >= self.follow_up:
            self.follow_up = 0.0
            if not self.urgent:
                self.urgent.extend(self.planner.priority(w, h, now))
        if self.want_screens:
            self.want_screens = False
            if self.screen.protocol >= 2:
                self.pipe.screens(self.on_screens)
        if self.sweep or now < self.next_sweep or self.pipe.frames:
            return
        self.sweeps += 1
        self.next_sweep = float("inf")  # until this sweep is through
        if self.sweeps % 10 == 1:
            self.pipe.info()  # another screen may have come to the front
        missing = self.screen.missing_tiles()
        active = self.planner.active(now)
        size = self.planner.piece_tiles(active, self.quiet > 0 and not missing)
        pieces = [p + (False,) for p in chunks(w, h, size, self.planner.sweep_rows(h, now))]
        if missing:
            pieces = [p[:4] + (True,) for p in self._missing_pieces(missing, size)]
        self.found = 0
        self.sweep_started = now
        if not pieces:  # all of it animated and not due: nothing to ask, the sweep is over
            self._sweep_done()
        self.sweep.extend(pieces)

    def _missing_pieces(self, missing, size) -> list[tuple]:
        """Pieces of a picture still to come, the pointer's corner first."""
        pieces = [p for p in chunks(self.screen.width, self.screen.height, size)
                  if any(p[0] <= x < p[0] + p[2] and p[1] <= y < p[1] + p[3] for x, y in missing)]
        py = self.planner.pointer[1]
        return sorted(pieces, key=lambda p: not (p[1] <= py < p[1] + p[3]))

    def _feed_frames(self) -> None:
        pipe = self.pipe
        while self.urgent and pipe.frames < FRAMES_IN_FLIGHT + 2:
            x, y, w, h = self.urgent.popleft()
            missing = self.screen.missing_tiles()
            force = any(x <= tx < x + w and y <= ty < y + h for tx, ty in missing)
            pipe.frame(x, y, w, h, force=force, on_done=self._frame_done)
        while self.sweep and self._room(self.sweep[0]):
            piece = self.sweep.popleft()
            guess = self._guess(piece)
            self.in_flight += guess
            last = not self.sweep
            pipe.frame(*piece[:4], force=piece[4],
                       on_done=lambda *r, p=piece, g=guess, last=last: self._piece_done(p, g, last, *r))

    def _guess(self, piece) -> float:
        tiles = -(-piece[2] // TILE) * -(-piece[3] // TILE)
        return self.piece_bytes.get(piece[:4], tiles * self.planner.avg_tile * (1 if piece[4] else 0.3))

    def _room(self, piece) -> bool:
        """Fill the round trip with frames, but never more than input should wait behind."""
        frames = self.pipe.frames
        if frames < FRAMES_IN_FLIGHT:
            return True
        window = self.planner.rate * (BUDGET + self.rtt / 1000)
        return frames < MAX_FRAMES_IN_FLIGHT and self.in_flight + self._guess(piece) <= window

    def _frame_done(self, count: int, nbytes: int, seconds: float) -> None:
        self.found += count
        self.planner.note_rate(nbytes, seconds)

    def _piece_done(self, piece, guess, last, count, nbytes, seconds) -> None:
        self.in_flight -= guess
        self.piece_bytes[piece[:4]] = nbytes
        self._frame_done(count, nbytes, seconds)
        if last:
            self._sweep_done()

    def _sweep_done(self) -> None:
        now = time.monotonic()
        if self.found or self.screen.missing_tiles():
            self.quiet = 0
        else:
            self.quiet += 1
            self.sweep_took = now - self.sweep_started
        self.next_sweep = now + self._pause(now)

    def _pause(self, now: float) -> float:
        """Breather after a sweep: short while things happen, longer the quieter it gets.

        A quiet sweep still costs a slow Amiga a full read of its screen (and
        amigentd reads it once more ahead), so the pause also keeps our share of
        its time under DUTY: a 7 MHz box has better things to do than draw for us.
        """
        if self.screen.missing_tiles():
            return 0.0
        if self.planner.active(now):
            return SWEEP_ACTIVE
        if self.found:
            return IDLE_STEPS[0]
        step = IDLE_STEPS[min(self.quiet, len(IDLE_STEPS)) - 1]
        duty = DUTY[0]
        if now - self.planner.last_input > DOZE_AFTER and self.quiet > len(IDLE_STEPS):
            step, duty = DOZE, DUTY[1]
        return max(step, self.sweep_took * (1 / duty - 1))

    def _tiles(self, tiles) -> None:
        self.planner.note_tiles(tiles, time.monotonic(), self.screen.missing_tiles())
