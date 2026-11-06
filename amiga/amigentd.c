/*
 * amigentd: the front screen as 64x64 tiles, mouse and keyboard, and command
 * channels for command lines and files, over TCP; with AmiSSL also over TLS.
 * The wire format is in docs/PROTOCOL.md. MIT licence, see LICENSE.
 *
 *   amigentd [PORT <n>] [SETUP] [KEY <file>] [QUIET] [TLSONLY]
 *
 * SETUP also accepts files (PUT) and command lines (RUN) until SETUP_END.
 * TLSONLY: once the TLS port is up, the plain one talks to localhost only.
 * From Workbench the same come as tool types: PORT=<n>, SETUP, KEY=<file>, QUIET,
 * TLSONLY.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <exec/execbase.h>
#include <dos/dos.h>
#include <dos/dostags.h>
#include <dos/dosextens.h>
#include <intuition/intuitionbase.h>
#include <intuition/screens.h>
#include <intuition/classes.h>
#include <intuition/imageclass.h>
#include <devices/input.h>
#include <devices/inputevent.h>
#include <devices/timer.h>
#include <graphics/gfx.h>
#include <graphics/gfxbase.h>
#include <graphics/view.h>
#include <graphics/modeid.h>
#include <graphics/clip.h>
#include <graphics/layers.h>
#include <workbench/startup.h>
#include <workbench/workbench.h>
#include <proto/exec.h>
#include <proto/timer.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/graphics.h>
#include <proto/keymap.h>
#include <proto/icon.h>
#include <proto/bsdsocket.h>
#include <proto/rexxsyslib.h>
#include <rexx/storage.h>
#include <rexx/errors.h>
#include <netinet/tcp.h>
#include <sys/errno.h>
#include <sys/ioctl.h>
#include <string.h>

/* The SDK here has the CGX inline stubs but no clib or cybergraphx headers;
 * the values below are from cybergraphx/cybergraphics.h. */
extern struct Library *CyberGfxBase;
#include <inline/cybergraphics_protos.h>

#define RECTFMT_RGB 0
#define CYBRMATTR_BPPIX 0x80000002
#define CYBRMATTR_ISCYBERGFX 0x80000008

#define PROTOCOL_VERSION 4
#define DEFAULT_PORT 6801
#define TILE 64
#define RGB_TILES 2  /* tiles read in one go: 24 KB, so a 1 MB A500+ still copes */
#define RGB_MAX (512L * 512)  /* FRAME_RGB: most pixels at once */
/* Menu boxes relative to Intuition's layout numbers, found on OS 3.2. */
#define MENU_X 0
#define MENU_Y 0
#define SUB_X 0
#define SUB_Y 0
#define IDLE_SECONDS 120
#define MAX_AGE_MS 500
#define SETTLE_MS 250
#define IO_SECONDS 30
#define PAUSE_MAX 65535  /* seconds, about 18 hours: what INFO's u16 can tell */
#define OUT_SIZE 16384
#define SEND_BUFFER 16384  /* little queued, so a takeover is not stuck behind it */
#define SEND_STEP 2048  /* send low water mark: writable means this much room */
/* A frame starts going out once this much is packed: early enough that the
 * line works while the CPU packs, late enough for full packets. Plipbox and
 * serial pay per packet, not per byte. */
#define SEND_EARLY 8192
#define CHUNK 32768
#define PENDING 4  /* connections that have not said yet what they are */
#define CHANNELS 4  /* command channels next to the screen client */
#ifndef INADDR_LOOPBACK
#define INADDR_LOOPBACK 0x7F000001
#endif
#define HELLO_SECONDS 5  /* a newcomer's key has to arrive this fast */

#ifndef UNIQUE_ID
#define UNIQUE_ID (-1)
#endif

enum {
    CMD_INFO = 1, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING,
    CMD_CHAR, CMD_PUT, CMD_RUN, CMD_SETUP_END, CMD_SCREENS, CMD_SCREEN_FRONT,
    CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS, CMD_AUTH, CMD_CLIENT, CMD_PAUSE,
    CMD_CHANNEL = 20, CMD_EXEC, CMD_READ_FILE, CMD_WRITE_FILE, CMD_DIR, CMD_STAT, CMD_DELETE,
    CMD_RENAME, CMD_MAKEDIR, CMD_CHECKSUM, CMD_SYSINFO, CMD_VOLUMES
};

const char version[] = "$VER: amigentd 1.3.0 (06.11.2026)";

extern struct ExecBase *SysBase;
struct Device *TimerBase;
struct IntuitionBase *IntuitionBase;
struct GfxBase *GfxBase;
struct Library *KeymapBase;
struct Library *CyberGfxBase;
struct Library *SocketBase;
struct Library *IconBase;

static struct MsgPort *input_port, *timer_port;
static struct IOStdReq *input_io;
static APTR old_window;  /* pr_WindowPtr as the Shell or Workbench gave it */
static struct timerequest *timer_io;

static LONG listener = -1, client = -1;
static BOOL setup_mode, quit, authed, from_wb, quiet, tls_only;
static ULONG client_id, owner;  /* CLIENT ids: this connection's, and whose picture ref is */

/* With a key file, every connection has to send the key first. */
#define KEY_MAX 64
static char secret_file[256] = "S:amigent.key";
static UBYTE secret[KEY_MAX];
static LONG secret_len;
static UWORD buttons;
static UBYTE prev_code[2], prev_qual[2];

/* The reference: what the clients were last sent, kept across connections.
 * valid[] holds one byte per tile: bit 0 once ref has it, SEND while it is queued,
 * DIFF while cur (the screen as last read) differs from ref. */
#define SEND 2
#define DIFF 4
static UBYTE *ref, *cur, *valid, *rgb;
static LONG ref_w, ref_h, cols, rows;

/* Connections that have not yet asked for the screen or a command channel. */
static struct newcomer {
    LONG fd, have;
    UBYTE cmd[10];
    BOOL authed, screen;
    ULONG since, addr;
} pend[PENDING];

/* Command channels run in processes of their own, see channel_main. */
static struct channel {
    struct Process *proc;
    LONG id;
    volatile BOOL busy;
    volatile ULONG last;
    ULONG addr, since;  /* for CLIENTS on the ARexx port */
} chans[CHANNELS];
static ULONG client_addr, client_since, start_ms;
static BYTE chan_gone = -1;  /* a channel or exec runner ended */
static LONG children;  /* channel and runner processes alive, changed under Forbid */
static ULONG runs;  /* numbers temporary names, under Forbid */

static LONG watch(fd_set *r, LONG top);
static volatile BOOL tls_ready;
static volatile BOOL tls_open;  /* the TLS port listens right now */
static BOOL newcomers(fd_set *r);

static UBYTE out[OUT_SIZE];
static LONG out_len;
/* The screen client's next command, as far as it came in. Input that turns up
 * while a reply is still going out is done at once; its answer waits in acks
 * and follows the reply, so the order on the wire stays the order asked. */
static UBYTE in_cmd[10];
static LONG in_have, nacks;
static UBYTE acks[64];
static BOOL sneak_input(void);
static void rexx_messages(void);
static ULONG rexx_signal(void);
static UBYTE tile_buf[12 + TILE * TILE * 2];

/* ---- network ---------------------------------------------------------- */

/* Waits until the client can take data or has some. FALSE when a newcomer asks
 * for the screen, on Ctrl-C, or after IO_SECONDS without progress. Newcomers
 * that only send their key or want a command channel are served meanwhile. */
static BOOL ready(BOOL writing)
{
    for (;;) {
        fd_set r, w;
        struct timeval tv = {IO_SECONDS, 0};
        ULONG sigs = SIGBREAKF_CTRL_C | rexx_signal();
        LONG n, top;

        BOOL sneak = writing && in_have < (LONG)sizeof(in_cmd) && nacks < (LONG)sizeof(acks);

        FD_ZERO(&r);
        FD_ZERO(&w);
        FD_SET(client, writing ? &w : &r);
        if (sneak)  /* the mouse doesn't queue behind a frame */
            FD_SET(client, &r);
        top = watch(&r, client);
        n = WaitSelect(top + 1, &r, &w, NULL, &tv, &sigs);
        if (sigs & SIGBREAKF_CTRL_C)
            quit = TRUE;
        if (sigs & rexx_signal())  /* a question from ARexx doesn't wait for the frame */
            rexx_messages();
        if (n == 0 && (sigs & rexx_signal()))
            continue;
        if (n <= 0 || quit || newcomers(&r))
            return FALSE;
        if (sneak && FD_ISSET(client, &r) && !sneak_input())
            return FALSE;
        if (FD_ISSET(client, writing ? &w : &r))
            return TRUE;
    }
}

static BOOL flush(void)
{
    LONG done = 0, n;

    while (done < out_len) {
        if (!ready(TRUE))
            return FALSE;
        /* No more than the stack promised room for, so send() never blocks. */
        n = out_len - done < SEND_STEP ? out_len - done : SEND_STEP;
        n = send(client, out + done, n, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0)
            return FALSE;
        done += n;
    }
    out_len = 0;
    return TRUE;
}

static BOOL put(const void *data, LONG len)
{
    const UBYTE *p = data;
    LONG n;

    while (len > 0) {
        if (out_len == OUT_SIZE && !flush())
            return FALSE;
        n = OUT_SIZE - out_len;
        if (n > len)
            n = len;
        memcpy(out + out_len, p, n);
        out_len += n;
        p += n;
        len -= n;
    }
    return TRUE;
}

static BOOL reply(UBYTE code)
{
    return put(&code, 1) && flush();
}

static BOOL receive(void *data, LONG len)
{
    UBYTE *p = data;
    LONG n;

    while (len > 0) {
        if (!ready(FALSE))
            return FALSE;
        n = recv(client, p, len, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
    }
    return TRUE;
}

static BOOL skip(ULONG len)
{
    UBYTE scratch[512];

    while (len > 0) {
        ULONG n = len > sizeof(scratch) ? sizeof(scratch) : len;
        if (!receive(scratch, n))
            return FALSE;
        len -= n;
    }
    return TRUE;
}

static void be16(UBYTE *p, UWORD v)
{
    p[0] = v >> 8;
    p[1] = v;
}

static void be32(UBYTE *p, ULONG v)
{
    be16(p, v >> 16);
    be16(p + 2, v);
}

/* ---- input ------------------------------------------------------------ */

static void write_event(struct InputEvent *ev)
{
    GetSysTime(&ev->ie_TimeStamp);
    input_io->io_Command = IND_WRITEEVENT;
    input_io->io_Data = ev;
    input_io->io_Length = sizeof(*ev);
    input_io->io_Flags = 0;
    DoIO((struct IORequest *)input_io);
}

static UWORD button_qualifiers(void)
{
    UWORD q = IEQUALIFIER_RELATIVEMOUSE;

    if (buttons & 1) q |= IEQUALIFIER_LEFTBUTTON;
    if (buttons & 2) q |= IEQUALIFIER_RBUTTON;
    if (buttons & 4) q |= IEQUALIFIER_MIDBUTTON;
    return q;
}

/* For programs that read mouse deltas and ignore absolute positions. */
static void move_by(WORD dx, WORD dy)
{
    struct InputEvent ev;

    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_RAWMOUSE;
    ev.ie_Code = IECODE_NOBUTTON;
    ev.ie_Qualifier = button_qualifiers();
    ev.ie_X = dx;
    ev.ie_Y = dy;
    write_event(&ev);
}

static void move_to(WORD x, WORD y)
{
    struct InputEvent ev;
    struct IEPointerPixel pp;
    ULONG lock;

    lock = LockIBase(0);
    pp.iepp_Screen = IntuitionBase->FirstScreen;
    UnlockIBase(lock);
    pp.iepp_Position.X = x;
    pp.iepp_Position.Y = y;
    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_NEWPOINTERPOS;
    ev.ie_SubClass = IESUBCLASS_PIXEL;
    ev.ie_Qualifier = button_qualifiers() & ~IEQUALIFIER_RELATIVEMOUSE;
    ev.ie_EventAddress = &pp;
    write_event(&ev);
}

static void set_buttons(UWORD mask)
{
    static const UWORD codes[3] = {IECODE_LBUTTON, IECODE_RBUTTON, IECODE_MBUTTON};
    struct InputEvent ev;
    int i;

    for (i = 0; i < 3; i++) {
        UWORD bit = 1 << i;
        if ((mask ^ buttons) & bit) {
            buttons ^= bit;
            memset(&ev, 0, sizeof(ev));
            ev.ie_Class = IECLASS_RAWMOUSE;
            ev.ie_Code = codes[i] | (buttons & bit ? 0 : IECODE_UP_PREFIX);
            ev.ie_Qualifier = button_qualifiers();
            write_event(&ev);
        }
    }
}

static void key(UBYTE code, BOOL down, UWORD qualifier)
{
    struct InputEvent ev;

    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_RAWKEY;
    ev.ie_Code = code | (down ? 0 : IECODE_UP_PREFIX);
    ev.ie_Qualifier = qualifier;
    /* Dead keys look back at the last two keys pressed. */
    ev.ie_position.ie_dead.ie_prev1DownCode = prev_code[0];
    ev.ie_position.ie_dead.ie_prev1DownQual = prev_qual[0];
    ev.ie_position.ie_dead.ie_prev2DownCode = prev_code[1];
    ev.ie_position.ie_dead.ie_prev2DownQual = prev_qual[1];
    write_event(&ev);
    if (down) {
        prev_code[1] = prev_code[0];
        prev_qual[1] = prev_qual[0];
        prev_code[0] = code;
        prev_qual[0] = qualifier;
    }
}

static BOOL type_char(UWORD ch, UWORD extra)
{
    /* Both buffers as words: on a 68000 keymap.library may access them by the
     * word, and a UBYTE array on the stack can sit at an odd address. */
    UWORD in = ch << 8, buf[8];
    UBYTE *keys = (UBYTE *)buf;
    LONG n, i;

    if (ch > 255)
        return FALSE;
    n = MapANSI((STRPTR)&in, 1, (STRPTR)keys, sizeof(buf) / 2, NULL);
    if (n <= 0)
        return FALSE;
    for (i = 0; i < n; i++) {
        key(keys[2 * i], TRUE, keys[2 * i + 1] | extra);
        key(keys[2 * i], FALSE, keys[2 * i + 1] | extra);
    }
    return TRUE;
}

/* ---- screen ----------------------------------------------------------- */

/* cur is kept fresh between commands: after each frame a reader process reads its tiles
 * again, so the next look only checks flags. stamp[] says when a tile was last read.
 * The semaphore guards everything about the picture; both processes hold it only
 * while at priority 2, so neither waits on the other behind a busy program. */
static struct SignalSemaphore picture;
static struct Task *server, *reader;
static BYTE reader_gone = -1;
static volatile BOOL reader_quit;
static ULONG *stamp;
static UBYTE *want;
static LONG wanted;
static struct Screen *seen;
static ULONG input_ms;

/* Milliseconds from the E clock: it only ever goes forward. The system time
 * doesn't; a clock sync can put it back, and then an uptime comes out shorter
 * than a connection that started after it. Wraps after 49 days, harmless for
 * differences. */
static ULONG now_ms(void)
{
    static ULONG khz;
    struct EClockVal ev;
    ULONG ms;

    if (!khz)
        khz = ReadEClock(&ev) / 1000;
    ReadEClock(&ev);
    ms = (ULONG)((((unsigned long long)ev.ev_hi << 32) | ev.ev_lo) / khz);
    return ms ? ms : 1;  /* 0 means never read */
}

/* PAUSE: no screen is read until pause_end, so a test or a benchmark has the
 * CPU and the chip bus to itself. Mouse, keys, command channels and files go
 * on as usual. Set from the screen client, a command channel or ARexx; one
 * aligned long, so the channel processes can write it without a lock. */
static volatile ULONG pause_end;  /* now_ms() at the end, 0 while not paused */

/* Seconds of pause left, rounded up; 0 when there is none. */
static ULONG pause_left(void)
{
    ULONG end = pause_end;
    LONG left;

    if (!end)
        return 0;
    left = (LONG)(end - now_ms());
    if (left <= 0) {
        pause_end = 0;
        return 0;
    }
    return ((ULONG)left + 999) / 1000;
}

/* seconds 0 ends a pause; a new one replaces the old. */
static void set_pause(ULONG seconds)
{
    ULONG end;

    if (seconds > PAUSE_MAX)
        seconds = PAUSE_MAX;
    end = now_ms() + seconds * 1000;
    pause_end = seconds ? (end ? end : 1) : 0;  /* 0 means no pause */
}

/* Waits for sigs, or ms milliseconds when ms >= 0; returns the signals that came.
 * Instead of WaitSelect without sockets, whose timeout WinUAE's bsdsocket ignores. */
static ULONG wait_ms(LONG ms, ULONG sigs)
{
    struct MsgPort *port = ms >= 0 ? CreateMsgPort() : NULL;
    struct timerequest *t = port ? (struct timerequest *)CreateIORequest(port, sizeof(*t)) : NULL;
    ULONG got;

    if (t && OpenDevice(TIMERNAME, UNIT_VBLANK, (struct IORequest *)t, 0) == 0) {
        t->tr_node.io_Command = TR_ADDREQUEST;
        t->tr_time.tv_secs = ms / 1000;
        t->tr_time.tv_micro = ms % 1000 * 1000;
        SendIO((struct IORequest *)t);
        got = Wait(sigs | 1UL << port->mp_SigBit) & sigs;
        if (!CheckIO((struct IORequest *)t))
            AbortIO((struct IORequest *)t);
        WaitIO((struct IORequest *)t);
        CloseDevice((struct IORequest *)t);
    } else if (ms >= 0) {
        Delay(ms / 20 + 1);
        got = SetSignal(0, sigs) & sigs;
    } else
        got = Wait(sigs);
    if (t)
        DeleteIORequest((struct IORequest *)t);
    if (port)
        DeleteMsgPort(port);
    return got;
}

static void free_frame(void)
{
    FreeVec(ref);
    FreeVec(cur);
    FreeVec(valid);
    FreeVec(rgb);
    FreeVec(stamp);
    FreeVec(want);
    ref = cur = valid = rgb = want = NULL;
    stamp = NULL;
    ref_w = ref_h = wanted = 0;
}

/* A new screen size starts a fresh reference: every tile counts as changed. */
static BOOL fit_frame(LONG w, LONG h)
{
    if (w == ref_w && h == ref_h && ref)
        return TRUE;
    free_frame();
    cols = (w + TILE - 1) / TILE;
    rows = (h + TILE - 1) / TILE;
    ref = AllocVec(w * h, MEMF_ANY);
    cur = AllocVec(w * h, MEMF_ANY);
    valid = AllocVec(cols * rows, MEMF_ANY | MEMF_CLEAR);
    want = AllocVec(cols * rows, MEMF_ANY | MEMF_CLEAR);
    stamp = AllocVec(cols * rows * sizeof(ULONG), MEMF_ANY | MEMF_CLEAR);
    rgb = AllocVec(RGB_TILES * TILE * TILE * 3, MEMF_ANY);
    if (!ref || !cur || !valid || !want || !stamp || !rgb) {
        free_frame();
        return FALSE;
    }
    ref_w = w;
    ref_h = h;
    return TRUE;
}

/* Locks Intuition and fits the buffers to the front screen; NULL without memory. */
static struct Screen *front(ULONG *lock)
{
    struct Screen *scr;

    *lock = LockIBase(0);
    scr = IntuitionBase->FirstScreen;
    if (!fit_frame(scr->Width, scr->Height)) {
        UnlockIBase(*lock);
        return NULL;
    }
    if (scr != seen) {  /* same size, other screen: nothing read so far counts */
        memset(stamp, 0, cols * rows * sizeof(ULONG));
        seen = scr;
    }
    return scr;
}

#define rgb332(r, g, b) ((UBYTE)(((r) >> 5) | ((g) >> 5 << 3) | ((b) & 0xC0)))

/* One row of HAM6 or HAM8 pens from the left edge; RGB888 out from pixel skip on.
 * Top two bits: 0 = pen, 1 = blue, 2 = red, 3 = green. The data bits replace the
 * top bits of that component, the low bits stay from the pixel before. */
static void ham_row(const UBYTE *pens, LONG width, ULONG depth, const UBYTE *pal,
                    UBYTE *dst, LONG skip)
{
    UBYTE c[3], shift = depth - 2, mask = (1 << shift) - 1, up = 8 - shift;
    UBYTE low = (1 << up) - 1;
    LONG i;

    c[0] = pal[0], c[1] = pal[1], c[2] = pal[2];  /* each row starts from colour 0 */
    for (i = 0; i < width; i++) {
        UBYTE p = pens[i], data = p & mask;
        switch (p >> shift) {
        case 0: c[0] = pal[3 * data]; c[1] = pal[3 * data + 1]; c[2] = pal[3 * data + 2]; break;
        case 1: c[2] = (c[2] & low) | data << up; break;
        case 2: c[0] = (c[0] & low) | data << up; break;
        case 3: c[1] = (c[1] & low) | data << up; break;
        }
        if (i >= skip) {
            UBYTE *d = dst + 3 * (i - skip);
            d[0] = c[0], d[1] = c[1], d[2] = c[2];
        }
    }
}

/* ---- OS 2.04 ------------------------------------------------------------ */

/* Kickstart 2.04 (V37) has no GetBitMapAttr, GetRGB32 or AllocBitMap. It has
 * no graphics cards either, so every bitmap is the classic planar kind and the
 * old ways do: Depth from the struct, colours from GetRGB4, pens by hand. */
static BOOL os39;  /* graphics.library 39 or newer */

static ULONG bm_depth(struct BitMap *bm)
{
    return os39 ? GetBitMapAttr(bm, BMA_DEPTH) : bm->Depth;
}

/* n colours from first as 32 bit triples, GetRGB32 style. 4 bits a gun on
 * V37: 0xA becomes 0xAAAAAAAA, the same as V39 reports for an OCS palette. */
static void get_rgb32(struct ColorMap *cm, ULONG first, ULONG n, ULONG *out)
{
    ULONG i;

    if (os39) {
        GetRGB32(cm, first, n, out);
        return;
    }
    for (i = 0; i < n; i++) {
        ULONG v = GetRGB4(cm, first + i);
        *out++ = ((v >> 8) & 15) * 0x11111111UL;
        *out++ = ((v >> 4) & 15) * 0x11111111UL;
        *out++ = (v & 15) * 0x11111111UL;
    }
}

/* Pens of one row straight from the planes, for V37 where ReadPixelLine8
 * would want an AllocBitMap. A table fans a plane byte out to eight pens, two
 * longs at a time: c2p the way it was done before there was a blitter trick. */
static ULONG fan_hi[256], fan_lo[256];

static void pens_by_hand(struct BitMap *bm, LONG x0, LONG y, LONG width, ULONG depth, UBYTE *pens)
{
    LONG x, p;

    if (!fan_hi[255])
        for (x = 0; x < 256; x++)
            for (p = 0; p < 8; p++)
                if (x & (0x80 >> p)) {
                    if (p < 4) fan_hi[x] |= 1UL << (24 - 8 * p);
                    else fan_lo[x] |= 1UL << (24 - 8 * (p - 4));
                }
    if (x0 & 7) {  /* off the byte grid: the slow way */
        memset(pens, 0, width);
        for (p = 0; p < (LONG)depth; p++) {
            const UBYTE *row = bm->Planes[p] + y * bm->BytesPerRow;
            UBYTE bit = 1 << p;
            for (x = 0; x < width; x++)
                if (row[(x0 + x) >> 3] & (0x80 >> ((x0 + x) & 7)))
                    pens[x] |= bit;
        }
        return;
    }
    y *= bm->BytesPerRow;  /* now the row's offset in each plane */
    for (x = 0; x < width; x += 8) {  /* pens comes from AllocVec: longs line up */
        ULONG hi = 0, lo = 0;
        for (p = 0; p < (LONG)depth; p++) {
            UBYTE b = bm->Planes[p][y + ((x0 + x) >> 3)];
            hi |= fan_hi[b] << p;
            lo |= fan_lo[b] << p;
        }
        ((ULONG *)(pens + x))[0] = hi;
        ((ULONG *)(pens + x))[1] = lo;
    }
}

/* Chipset screens: pens through the palette, HAM6 and HAM8 decoded along each row. */
static BOOL read_planar(struct Screen *scr, LONG x0, LONG y0, LONG w, LONG h, UBYTE *out)
{
    struct BitMap *bm = scr->RastPort.BitMap, *line = NULL;
    struct RastPort tmp;
    ULONG depth = bm_depth(bm), colours, i;
    ULONG *table;
    UBYTE pal[3 * 256], *pens;
    BOOL ham = (GetVPModeID(&scr->ViewPort) & HAM_KEY) && (depth == 6 || depth == 8);
    LONG y, from = ham ? 0 : x0, width = x0 + w - from;

    if (depth > 8)
        return FALSE;
    colours = 1UL << depth;
    if (colours > (ULONG)scr->ViewPort.ColorMap->Count)
        colours = scr->ViewPort.ColorMap->Count;
    table = AllocVec(colours * 3 * sizeof(ULONG), MEMF_ANY | MEMF_CLEAR);
    pens = AllocVec((width + 15) & ~15, MEMF_ANY);
    if (os39)
        line = AllocBitMap((width + 15) & ~15, 1, depth, 0, NULL);
    if (!table || !pens || (os39 && !line)) {
        FreeVec(table);
        FreeVec(pens);
        if (line)
            FreeBitMap(line);
        return FALSE;
    }
    get_rgb32(scr->ViewPort.ColorMap, 0, colours, table);
    memset(pal, 0, sizeof(pal));
    for (i = 0; i < 3 * colours; i++)
        pal[i] = table[i] >> 24;
    if ((GetVPModeID(&scr->ViewPort) & EXTRAHALFBRITE_KEY) && depth == 6)
        for (i = 3 * 32; i < 3 * 64; i++)  /* EHB: the top 32 are the bottom 32 at half */
            pal[i] = pal[i - 3 * 32] >> 1;
    tmp = scr->RastPort;
    tmp.Layer = NULL;
    tmp.BitMap = line;
    for (y = 0; y < h; y++) {
        UBYTE *dst = out + y * w * 3;
        if (os39)
            ReadPixelLine8(&scr->RastPort, from, y0 + y, width, pens, &tmp);
        else
            pens_by_hand(bm, from, y0 + y, width, depth, pens);
        if (!ham) {
            for (i = 0; i < (ULONG)w; i++, dst += 3) {
                const UBYTE *c = pal + 3 * pens[i];
                dst[0] = c[0], dst[1] = c[1], dst[2] = c[2];
            }
        } else {
            ham_row(pens, width, depth, pal, dst, x0 - from);
        }
    }
    if (line)
        FreeBitMap(line);
    FreeVec(pens);
    FreeVec(table);
    return TRUE;
}

/* Without cybergraphics.library there are only chipset screens. */
static BOOL is_rtg(struct Screen *scr)
{
    return CyberGfxBase && GetCyberMapAttr(scr->RastPort.BitMap, CYBRMATTR_ISCYBERGFX);
}

/* A graphics card screen of 256 colours, as RGB888 rows. Its pens are read
 * with graphics.library and looked up in the screen's own palette: asked for
 * RGB, or for the pens, some drivers answer from something other than what is
 * on display, and a game comes out in a handful of wrong colours or black.
 * ReadPixelArray8 pads each row to 16 pixels. FALSE when memory is short. */
static BOOL read_lut8(struct Screen *scr, LONG x0, LONG y0, LONG w, LONG h, UBYTE *out)
{
    ULONG colours = scr->ViewPort.ColorMap ? scr->ViewPort.ColorMap->Count : 0;
    LONG pw = (w + 15) & ~15, x, y;
    ULONG *table = AllocVec(256 * 3 * sizeof(ULONG), MEMF_ANY | MEMF_CLEAR);
    UBYTE *pens = AllocVec(pw * h, MEMF_ANY);
    struct BitMap *line = AllocBitMap(pw, 1, 8, 0, NULL);
    struct RastPort temp;
    BOOL ok = table && pens && line;

    if (ok) {
        if (colours > 256)
            colours = 256;
        if (colours)
            get_rgb32(scr->ViewPort.ColorMap, 0, colours, table);
        temp = scr->RastPort;
        temp.Layer = NULL;
        temp.BitMap = line;
        ReadPixelArray8(&scr->RastPort, x0, y0, x0 + w - 1, y0 + h - 1, pens, &temp);
        for (y = 0; y < h; y++) {
            const UBYTE *p = pens + y * pw;
            UBYTE *o = out + 3 * y * w;
            for (x = 0; x < w; x++, o += 3) {
                const ULONG *c = table + 3 * p[x];
                o[0] = c[0] >> 24;
                o[1] = c[1] >> 24;
                o[2] = c[2] >> 24;
            }
        }
    }
    if (line)
        FreeBitMap(line);
    FreeVec(pens);
    FreeVec(table);
    return ok;
}

/* Any screen, as RGB888 rows of w pixels. Callers hold LockIBase. */
static void read_rgb(struct Screen *scr, LONG x0, LONG y0, LONG w, LONG h, UBYTE *out)
{
    if (is_rtg(scr) || !read_planar(scr, x0, y0, w, h, out)) {
        if (!CyberGfxBase)
            memset(out, 0, w * h * 3);  /* deeper than 8 planes, nothing to read it with */
        else if (!(os39 && is_rtg(scr) && GetCyberMapAttr(scr->RastPort.BitMap, CYBRMATTR_BPPIX) == 1 &&
                   read_lut8(scr, x0, y0, w, h, out)))
            ReadPixelArray(out, 0, 0, w * 3, &scr->RastPort, x0, y0, w, h, RECTFMT_RGB);
    }
}

/* Does the tile at x, y differ between cur and ref? Long words where they line up. */
static BOOL differs(LONG x, LONG y, LONG tw, LONG th)
{
    LONG at = y * ref_w + x, n, i;

    for (n = 0; n < th; n++, at += ref_w) {
        if (!(ref_w & 3) && !(tw & 3)) {
            const ULONG *a = (const ULONG *)(ref + at), *b = (const ULONG *)(cur + at);
            for (i = 0; i < tw / 4; i++)
                if (a[i] != b[i])
                    return TRUE;
        } else {
            for (i = 0; i < tw; i++)
                if (ref[at + i] != cur[at + i])
                    return TRUE;
        }
    }
    return FALSE;
}

/* Reads tiles tx0..tx1 of tile row ty from the screen into cur, as RGB332. */
static void read_tiles(struct Screen *scr, LONG ty, LONG tx0, LONG tx1)
{
    LONG y0 = ty * TILE, x, n, i, t0;
    LONG th = ref_h - y0 < TILE ? ref_h - y0 : TILE;
    ULONG ms = now_ms();

    for (t0 = tx0; t0 <= tx1; t0 += RGB_TILES) {  /* a couple of tiles at a time */
        LONG x0 = t0 * TILE, t1 = t0 + RGB_TILES - 1 < tx1 ? t0 + RGB_TILES - 1 : tx1;
        LONG cw = ((t1 + 1) * TILE < ref_w ? (t1 + 1) * TILE : ref_w) - x0;
        UBYTE *src = rgb;

        read_rgb(scr, x0, y0, cw, th, rgb);
        for (n = 0; n < th; n++) {
            UBYTE *dst = cur + (y0 + n) * ref_w + x0;
            for (i = 0; i < cw; i++, src += 3)
                dst[i] = rgb332(src[0], src[1], src[2]);
        }
    }
    for (x = tx0; x <= tx1; x++) {
        UBYTE *v = valid + ty * cols + x;
        if (differs(x * TILE, y0, (x + 1) * TILE < ref_w ? TILE : ref_w - x * TILE, th))
            *v |= DIFF;
        else
            *v &= ~DIFF;
        stamp[ty * cols + x] = ms;
        if (want[ty * cols + x]) {
            want[ty * cols + x] = 0;
            wanted--;
        }
    }
}

/* Too old to answer from: never read, read before the last input had time to
 * show, or long ago. */
static BOOL stale(LONG t, ULONG ms)
{
    return !stamp[t] || (input_ms && (LONG)(stamp[t] - input_ms) < SETTLE_MS) ||
           ms - stamp[t] > MAX_AGE_MS;
}

/* Brings the tiles of a region up to date; FALSE without memory. */
static BOOL grab(BOOL force, LONG x0, LONG y0, LONG x1, LONG y1, LONG *w, LONG *h)
{
    struct Screen *scr;
    ULONG lock, ms = now_ms();
    LONG tx, ty, run;

    if (!(scr = front(&lock)))
        return FALSE;
    *w = ref_w;
    *h = ref_h;
    if (x1 > ref_w) x1 = ref_w;
    if (y1 > ref_h) y1 = ref_h;
    for (ty = y0 / TILE; ty * TILE < y1; ty++) {
        run = -1;
        for (tx = x0 / TILE; tx * TILE < x1; tx++) {
            BOOL old = force || stale(ty * cols + tx, ms);
            if (old && run < 0)
                run = tx;
            if (!old && run >= 0) {
                read_tiles(scr, ty, run, tx - 1);
                run = -1;
            }
        }
        if (run >= 0)
            read_tiles(scr, ty, run, tx - 1);
    }
    UnlockIBase(lock);
    return TRUE;
}

/* One step of background reading: up to eight wanted tiles of one tile row. */
static void refresh(void)
{
    struct Screen *scr;
    ULONG lock;
    LONG t, tx0, tx1;

    if (!wanted || !(scr = front(&lock)))
        return;
    for (t = 0; t < cols * rows && !want[t]; t++)
        ;
    if (t < cols * rows) {
        tx0 = t % cols;
        for (tx1 = cols - 1; !want[t - tx0 + tx1]; tx1--)
            ;
        if (tx1 > tx0 + 7)  /* small steps keep everyone else waiting only briefly */
            tx1 = tx0 + 7;
        read_tiles(scr, t / cols, tx0, tx1);
    }
    UnlockIBase(lock);
}

/* Reads ahead at priority -1, a few tiles at a time, whenever a frame asks for it. */
static void __saveds reader_main(void)
{
    struct Task *me = FindTask(NULL);
    BOOL more;

    while (!reader_quit) {
        Wait(SIGBREAKF_CTRL_C | SIGBREAKF_CTRL_F);
        do {
            SetTaskPri(me, 2);
            ObtainSemaphore(&picture);
            refresh();
            more = wanted > 0;
            ReleaseSemaphore(&picture);
            SetTaskPri(me, -1);  /* lets the stack and every program go first */
        } while (more && !reader_quit);
    }
    Forbid();
    Signal(server, 1UL << reader_gone);
}

static void want_region(LONG x0, LONG y0, LONG x1, LONG y1)
{
    LONG tx, ty;

    for (ty = y0 / TILE; ty * TILE < y1; ty++)
        for (tx = x0 / TILE; tx * TILE < x1; tx++)
            if (!want[ty * cols + tx]) {
                want[ty * cols + tx] = 1;
                wanted++;
            }
}

static void clear_marks(void)
{
    LONG i;

    for (i = 0; i < cols * rows; i++)
        valid[i] &= ~SEND;
}

/* Runs of (length-1, colour) across the whole tile; raw if that is not smaller. */
static LONG pack(LONG tx, LONG ty, LONG tw, LONG th, UBYTE *dst)
{
    LONG size = tw * th, n = 0, y, x, run = 0;
    UBYTE colour = 0;

    for (y = 0; y < th && n < size; y++) {
        const UBYTE *row = cur + (ty + y) * ref_w + tx;
        for (x = 0; x < tw; x++) {
            if (run && (row[x] != colour || run == 256)) {
                dst[n++] = run - 1;
                dst[n++] = colour;
                run = 0;
            }
            colour = row[x];
            run++;
        }
    }
    if (run && n < size) {
        dst[n++] = run - 1;
        dst[n++] = colour;
    }
    if (n < size)
        return n;
    for (y = 0; y < th; y++)
        memcpy(dst + y * tw, cur + (ty + y) * ref_w + tx, tw);
    return size;
}

static BOOL frame_locked(BOOL force, LONG x, LONG y, LONG w, LONG h);

static BOOL frame(BOOL force, LONG x, LONG y, LONG w, LONG h)
{
    BOOL ok;

    ObtainSemaphore(&picture);
    ok = frame_locked(force, x, y, w, h);
    ReleaseSemaphore(&picture);
    if (wanted)
        Signal(reader, SIGBREAKF_CTRL_F);
    return ok;
}

static BOOL frame_locked(BOOL force, LONG x, LONG y, LONG w, LONG h)
{
    LONG sw, sh, x1, y1, tx, ty, count = 0;
    UBYTE head[4];

    x1 = w ? x + w : 0x7FFF;
    y1 = h ? y + h : 0x7FFF;
    if (!grab(force, x, y, x1, y1, &sw, &sh)) {
        be32(head, 0);
        return put(head, 4) && flush();
    }
    if (x1 > sw) x1 = sw;
    if (y1 > sh) y1 = sh;
    want_region(x, y, x1, y1);  /* read again after the reply, for the next look */
    for (ty = y / TILE; ty * TILE < y1; ty++) {
        for (tx = x / TILE; tx * TILE < x1; tx++) {
            UBYTE *v = valid + ty * cols + tx;
            if (force || !(*v & 1) || (*v & DIFF)) {
                *v |= SEND;
                count++;
            }
        }
    }
    /* The reference stays some client's exact picture only if that client keeps
     * getting the changes, or starts over with everything. */
    if (count)
        owner = force || (client_id && owner == client_id) ? client_id : 0;
    be32(head, count);
    if (!put(head, 4))
        return FALSE;
    for (ty = y / TILE; count && ty * TILE < y1; ty++) {
        for (tx = x / TILE; tx * TILE < x1; tx++) {
            LONG px = tx * TILE, py = ty * TILE;
            LONG tw = sw - px < TILE ? sw - px : TILE;
            LONG th = sh - py < TILE ? sh - py : TILE;
            UBYTE *v = valid + ty * cols + tx;
            LONG size, at;

            if (!(*v & SEND))
                continue;
            /* Counted as sent before it is, so an aborted frame leaves no marks. */
            *v = 1;  /* ref is about to equal cur */
            count--;
            size = pack(px, py, tw, th, tile_buf + 12);
            be16(tile_buf, px);
            be16(tile_buf + 2, py);
            be16(tile_buf + 4, tw);
            be16(tile_buf + 6, th);
            be32(tile_buf + 8, size);
            for (at = py * ref_w + px; at < (py + th) * ref_w; at += ref_w)
                memcpy(ref + at, cur + at, tw);
            if (!put(tile_buf, 12 + size)) {
                clear_marks();
                return FALSE;
            }
        }
    }
    return flush();
}

/* ---- planar frames ---------------------------------------------------- */

/* Chipset screens, the bitplanes as they are: no c2p, no palette lookups, no
 * RGB332. Change detection is a compare of the plane bytes against what the
 * client already has, so a quiet screen costs one pass over chip RAM and the
 * reader can sleep. The client gets 1..8 bits a pixel and does the c2p itself;
 * a PC has cycles to burn, a 7 MHz 68000 has not.
 *
 * pref: the planes as the client has them, pcur: the planes as just read, both
 * [plane][row][pbpr]. pvalid: one byte a tile, set once the client has it. */
static UBYTE *pref, *pcur, *pvalid, *porder;
static LONG pw, ph, pbpr, pdepth, pcols, prows;
static struct BitMap *pbm;
static ULONG powner, pconn, conn_serial;  /* pref belongs to: CLIENT id, connection */
static UBYTE ppal[3 * 256];
static LONG pcolours = -1;
static ULONG prgb[3 * 256];
/* ULONG backed: the XOR and the row copies go a long at a time, and a 68000
 * takes an odd address as a personal insult. */
#define PENC_LONGS ((8 * 8 * TILE + 8 * TILE / 128 + 16) / 4)
static ULONG penc_mem[2][PENC_LONGS], ptile_mem[8 * 8 * TILE / 4];
static ULONG pwork_mem[8 * 8 * TILE / 4], pold_mem[8 * 8 * TILE / 4];
#define penc(i) ((UBYTE *)penc_mem[i])
#define ptile ((UBYTE *)ptile_mem)
#define pwork ((UBYTE *)pwork_mem)
#define pold ((UBYTE *)pold_mem)

static void free_planar(void)
{
    FreeVec(pref);
    FreeVec(pcur);
    FreeVec(pvalid);
    FreeVec(porder);
    pref = pcur = pvalid = porder = NULL;
    pw = ph = 0;
    pbm = NULL;
    pcolours = -1;
}

/* A planar screen we can read straight: chipset, 1..8 planes. */
static BOOL planar_ok(struct Screen *scr)
{
    struct BitMap *bm = scr->RastPort.BitMap;
    ULONG depth = bm_depth(bm);

    return !is_rtg(scr) && depth >= 1 && depth <= 8 &&
           (!os39 || (GetBitMapAttr(bm, BMA_FLAGS) & BMF_STANDARD));
}

/* New size, depth or screen: start from scratch, the client gets it all. */
static BOOL fit_planar(struct Screen *scr)
{
    struct BitMap *bm = scr->RastPort.BitMap;
    LONG w = scr->Width, h = scr->Height, depth = bm_depth(bm);

    if (pref && w == pw && h == ph && depth == pdepth && bm == pbm)
        return TRUE;
    free_planar();
    pcols = (w + TILE - 1) / TILE;
    prows = (h + TILE - 1) / TILE;
    pbpr = pcols * (TILE / 8);
    pref = AllocVec(depth * h * pbpr, MEMF_ANY | MEMF_CLEAR);
    pcur = AllocVec(depth * h * pbpr, MEMF_ANY | MEMF_CLEAR);
    pvalid = AllocVec(pcols * prows, MEMF_ANY | MEMF_CLEAR);
    porder = AllocVec(pcols * prows * sizeof(UWORD), MEMF_ANY);
    if (!pref || !pcur || !pvalid || !porder) {
        free_planar();
        return FALSE;
    }
    pw = w, ph = h, pdepth = depth, pbm = bm;
    return TRUE;
}

/* n longs side by side; -1 if all equal, else n-1 minus the index of the first
 * that differs. cmpm.l and dbne: the tightest compare loop a 68000 knows,
 * 30 cycles a long. dbne counts a word, so n stays below 32768. */
LONG cmp_longs(__reg("a0") const ULONG *a, __reg("a1") const ULONG *b, __reg("d0") LONG n) =
    "\tsubq.w\t#1,d0\n\tcmpm.l\t(a0)+,(a1)+\n\tdbne\td0,*-2\n\text.l\td0";

/* Marks the tiles of tile row ty, columns tx0..tx1, that moved on from pref.
 * Row by row across the whole width, as the planes lie in memory: one pass
 * over chip RAM, and a quiet screen costs nothing more. */
static void scan_changes(struct BitMap *bm, LONG ty, LONG tx0, LONG tx1)
{
    LONG y0 = ty * TILE, th = y0 + TILE < ph ? TILE : ph - y0, p, y, i;
    LONG full = tx1 - tx0 + 1, edge = -1, ebw = 0;
    UBYTE *mark = pvalid + ty * pcols;

    if (tx1 * TILE + TILE > pw) {  /* the last column is narrower: bytes for that one */
        edge = tx1;
        ebw = (pw - tx1 * TILE + 7) >> 3;
        full--;
    }
    for (p = 0; p < pdepth; p++) {
        PLANEPTR plane = bm->Planes[p];
        const UBYTE *old = pref + (p * ph + y0) * pbpr + tx0 * (TILE / 8);
        const UBYTE *src = plane + y0 * bm->BytesPerRow + tx0 * (TILE / 8);

        for (y = 0; y < th; y++, old += pbpr, src += bm->BytesPerRow) {
            if (plane == NULL || plane == (PLANEPTR)-1) {  /* V39 constant planes */
                UBYTE fill = plane ? 0xFF : 0;
                for (i = 0; i < full * 8 + ebw; i++)
                    if (old[i] != fill)
                        mark[tx0 + (i >> 3)] |= DIFF;
                continue;
            }
            if (full > 0) {
                LONG at = 0, n = full * 2, r;
                while ((r = cmp_longs((const ULONG *)src + at, (const ULONG *)old + at, n - at)) >= 0) {
                    LONG k = at + (n - at - 1 - r);  /* the long that differs */
                    mark[tx0 + (k >> 1)] |= DIFF;
                    at = (k | 1) + 1;  /* on to the next tile */
                    if (at >= n)
                        break;
                }
            }
            for (i = 0; i < ebw; i++)
                if (src[full * 8 + i] != old[full * 8 + i]) {
                    mark[edge] |= DIFF;
                    break;
                }
        }
    }
}

/* Copies th rows of bw bytes, the strides apart. The common case is a full tile:
 * two longs a row, pointers only. Multiplies are a library call on a 68000,
 * so none in here. */
static void copy_rows(UBYTE *dst, LONG dstep, const UBYTE *src, LONG sstep, LONG bw, LONG th)
{
    if (bw == 8) {
        while (th-- > 0) {
            ((ULONG *)dst)[0] = ((const ULONG *)src)[0];
            ((ULONG *)dst)[1] = ((const ULONG *)src)[1];
            dst += dstep, src += sstep;
        }
    } else {
        while (th-- > 0) {
            LONG i;
            for (i = 0; i < bw; i++)
                dst[i] = src[i];
            dst += dstep, src += sstep;
        }
    }
}

/* The tile from the screen into pcur, to be packed after Intuition is let go. */
static void grab_tile(struct BitMap *bm, LONG tx, LONG ty)
{
    LONG bx = tx * (TILE / 8), y0 = ty * TILE, p, y;
    LONG bw = ((tx * TILE + TILE < pw ? TILE : pw - tx * TILE) + 7) >> 3;
    LONG th = y0 + TILE < ph ? TILE : ph - y0;
    UBYTE *dst = pcur + y0 * pbpr + bx;

    for (p = 0; p < pdepth; p++, dst += ph * pbpr) {
        PLANEPTR plane = bm->Planes[p];
        if (plane == NULL || plane == (PLANEPTR)-1) {  /* V39 constant planes */
            UBYTE *d = dst;
            for (y = 0; y < th; y++, d += pbpr)
                memset(d, plane ? 0xFF : 0, bw);
        } else
            copy_rows(dst, pbpr, plane + y0 * bm->BytesPerRow + bx, bm->BytesPerRow, bw, th);
    }
}

/* PackBits: n < 128 then n+1 literals, n > 128 then the next byte 257-n times.
 * A literal byte costs two compares, the bookkeeping only happens at a run:
 * the 68000 has no time for second looks. */
static LONG packbits(const UBYTE *s, LONG len, UBYTE *d)
{
    const UBYTE *end = s + len, *stop = end - 2, *lit = s, *r, *lim;
    UBYTE *start = d, c;
    LONG k;

    while (s < stop) {
        c = *s;
        if (s[1] != c || s[2] != c) {  /* no run of three here: literal */
            s++;
            continue;
        }
        while (lit < s) {  /* literals waiting in front of the run */
            k = s - lit > 128 ? 128 : s - lit;
            *d++ = (UBYTE)(k - 1);
            memcpy(d, lit, k);
            d += k, lit += k;
        }
        lim = end - s > 128 ? s + 128 : end;
        for (r = s + 3; r < lim && *r == c; r++)
            ;
        *d++ = (UBYTE)(257 - (r - s));
        *d++ = c;
        s = lit = r;
    }
    while (lit < end) {
        k = end - lit > 128 ? 128 : end - lit;
        *d++ = (UBYTE)(k - 1);
        memcpy(d, lit, k);
        d += k, lit += k;
    }
    return d - start;
}

/* A tile's bytes, plane after plane, row after row, bw bytes a row. */
static LONG tile_bytes(const UBYTE *planes, LONG tx, LONG ty, LONG bw, LONG th, UBYTE *dst)
{
    const UBYTE *src = planes + ty * TILE * pbpr + tx * (TILE / 8);
    LONG p, plane = ph * pbpr;

    for (p = 0; p < pdepth; p++, src += plane, dst += bw * th)
        copy_rows(dst, bw, src, pbpr, bw, th);
    return pdepth * bw * th;
}

/* Pointer's neighbourhood first: that is where the eye is. */
static LONG order_tiles(const UBYTE *marks, LONG ncols, UWORD *out,
                        LONG tx0, LONG ty0, LONG tx1, LONG ty1, LONG mx, LONG my)
{
    LONG pass, tx, ty, n = 0, ptx = mx / TILE, pty = my / TILE;

    for (pass = 0; pass < 2; pass++)
        for (ty = ty0; ty <= ty1; ty++)
            for (tx = tx0; tx <= tx1; tx++) {
                BOOL near = tx >= ptx - 1 && tx <= ptx + 1 && ty >= pty - 1 && ty <= pty + 1;
                if (near == (pass == 0) && (marks[ty * ncols + tx] & SEND))
                    out[n++] = ty * ncols + tx;
            }
    return n;
}

/* Each row XORed with the one above: text and window borders turn into zeros,
 * which PackBits eats for breakfast. Rows of rb bytes; longs when they fit. */
static void xor_rows(const UBYTE *src, UBYTE *dst, LONG rb, LONG rows)
{
    const UBYTE *up = src;
    LONG y, i;

    memcpy(dst, src, rb);
    src += rb, dst += rb;
    for (y = 1; y < rows; y++, up += rb, src += rb, dst += rb) {
        if (!(rb & 3)) {
            const ULONG *s = (const ULONG *)src, *u = (const ULONG *)up;
            ULONG *d = (ULONG *)dst;
            for (i = rb >> 2; i > 0; i--)
                *d++ = *s++ ^ *u++;
        } else {
            for (i = 0; i < rb; i++)
                dst[i] = src[i] ^ up[i];
        }
    }
}

static void row_xor(const UBYTE *src, UBYTE *dst, LONG bw, LONG th)
{
    LONG p;

    for (p = 0; p < pdepth; p++, src += bw * th, dst += bw * th)
        xor_rows(src, dst, bw, th);
}

/* The old picture, dy rows further down, where this tile sits: what a scrolled
 * window was before. Rows off the screen count as zeros. */
static void shifted_bytes(LONG ttx, LONG tty, LONG bw, LONG th, LONG dy, UBYTE *dst)
{
    LONG p, y;

    for (p = 0; p < pdepth; p++)
        for (y = 0; y < th; y++, dst += bw) {
            LONG sy = tty * TILE + y + dy;
            if (sy < 0 || sy >= ph)
                memset(dst, 0, bw);
            else
                memcpy(dst, pref + (p * ph + sy) * pbpr + ttx * (TILE / 8), bw);
        }
}

/* Did the client have every tile the shifted rows come from, before this frame? */
static BOOL shift_ok(LONG ttx, LONG tty, LONG th, LONG dy)
{
    LONG top = tty * TILE + dy, bottom = top + th - 1, r;

    if (top < 0) top = 0;
    if (bottom >= ph) bottom = ph - 1;
    for (r = top / TILE; r <= bottom / TILE && top <= bottom; r++)
        if (!(pvalid[r * pcols + ttx] & 1))
            return FALSE;
    return TRUE;
}

/* Scrolling, the bread and butter of a Shell: a changed row of plane 0 from one
 * of the first tiles the client has, looked up in the old picture of the same
 * column, up to SHIFT_MAX rows either way, nearest first. A match counts once
 * the next rows agree as well; one lonely row is a coincidence, not a scroll. */
#define SHIFT_MAX 256
#define SHIFT_ROWS 8

static BOOL rows_match(const UBYTE *cur, LONG cy, LONG ttx, LONG bw, LONG s, LONG rows)
{
    const UBYTE *old = pref + ttx * (TILE / 8);
    LONG k;

    for (k = 0; k < rows; k++, cur += pbpr)
        if (cy + k + s < 0 || cy + k + s >= ph || memcmp(cur, old + (cy + k + s) * pbpr, bw))
            return FALSE;
    return TRUE;
}

static LONG find_dy(LONG n)
{
    LONG i, r, s, tries = 0;

    for (i = 0; i < n && tries < 4; i++) {
        LONG t = ((UWORD *)porder)[i], ttx = t % pcols, tty = t / pcols;
        LONG py = tty * TILE, th = py + TILE < ph ? TILE : ph - py;
        LONG bw = ((ttx * TILE + TILE < pw ? TILE : pw - ttx * TILE) + 7) >> 3;
        const UBYTE *cur = pcur + py * pbpr + ttx * (TILE / 8);
        const UBYTE *old = pref + ttx * (TILE / 8);

        if (!(pvalid[t] & 1))
            continue;
        tries++;
        for (r = 0; r + SHIFT_ROWS <= th; r++, cur += pbpr) {
            LONG k;
            for (k = 0; k < bw && !cur[k]; k++)
                ;
            if (k == bw || !memcmp(cur, old + (py + r) * pbpr, bw))
                continue;  /* empty or unchanged rows tell nothing */
            for (s = 1; s <= SHIFT_MAX; s++) {
                if (rows_match(cur, py + r, ttx, bw, s, SHIFT_ROWS))
                    return s;
                if (rows_match(cur, py + r, ttx, bw, -s, SHIFT_ROWS))
                    return -s;
            }
            break;  /* this tile didn't scroll, try the next */
        }
    }
    return 0;
}

static BOOL frame_chunky(BOOL force, LONG x, LONG y, LONG w, LONG h);

/* FRAME with arg 2: planar tiles for chipset screens, else the classic ones.
 * See "planar" in docs/PROTOCOL.md. */
static BOOL frame_planar(BOOL force, BOOL ext, LONG x, LONG y, LONG w, LONG h)
{
    struct Screen *scr;
    ULONG lock, mode;
    LONG x1, y1, tx, ty, tx1, ty1, n, i, count = 0, colours, mx, my, dy;
    UBYTE head[8], flags = 0, *v;
    BOOL ok, known;

    lock = LockIBase(0);
    scr = IntuitionBase->FirstScreen;
    if (!planar_ok(scr) || !fit_planar(scr)) {
        UnlockIBase(lock);
        ObtainSemaphore(&picture);
        if (ext) {  /* format 2: chunky with all the tricks */
            ok = frame_chunky(force, x, y, w, h);
        } else {
            head[0] = 0;  /* classic tiles follow */
            ok = put(head, 1) && frame_locked(force, x, y, w, h);
        }
        ReleaseSemaphore(&picture);
        if (wanted)
            Signal(reader, SIGBREAKF_CTRL_F);
        return ok;
    }
    /* Skipping and XOR need the client to hold pref: same connection, or the
     * CLIENT id it was made for. Anyone else gets everything. */
    known = pconn == conn_serial || (client_id && powner == client_id);
    if (!known)
        force = TRUE;
    mode = GetVPModeID(&scr->ViewPort);
    if ((mode & HAM_KEY) && (pdepth == 6 || pdepth == 8))
        flags |= 1;
    if ((mode & EXTRAHALFBRITE_KEY) && pdepth == 6)
        flags |= 4;
    colours = 1L << pdepth;
    if (flags & 4)
        colours = 32;
    if (colours > scr->ViewPort.ColorMap->Count)
        colours = scr->ViewPort.ColorMap->Count;
    get_rgb32(scr->ViewPort.ColorMap, 0, colours, prgb);
    for (i = 0; i < 3 * colours; i++)
        if ((UBYTE)(prgb[i] >> 24) != ppal[i])
            break;
    if (force || colours != pcolours || i < 3 * colours) {
        flags |= 2;
        for (i = 0; i < 3 * colours; i++)
            ppal[i] = prgb[i] >> 24;
        pcolours = colours;
    }
    x1 = w ? x + w : 0x7FFF;
    y1 = h ? y + h : 0x7FFF;
    if (x1 > pw) x1 = pw;
    if (y1 > ph) y1 = ph;
    tx1 = (x1 - 1) / TILE, ty1 = (y1 - 1) / TILE;
    for (ty = y / TILE; ty <= ty1 && x < x1 && y < y1; ty++) {
        if (!force)
            scan_changes(scr->RastPort.BitMap, ty, x / TILE, tx1);
        for (tx = x / TILE; tx <= tx1; tx++) {
            v = pvalid + ty * pcols + tx;
            if (force || !(*v & 1) || (*v & DIFF)) {
                grab_tile(scr->RastPort.BitMap, tx, ty);
                *v = (*v & ~DIFF) | SEND;
                count++;
            }
        }
    }
    mx = scr->MouseX, my = scr->MouseY;
    UnlockIBase(lock);  /* the rest is pcur and the wire, Intuition may go on */

    head[0] = 1;
    head[1] = pdepth;
    head[2] = flags;
    head[3] = 0;
    be16(head + 4, pw);
    be16(head + 6, ph);
    if (!put(head, 8))
        return FALSE;
    if (flags & 2) {
        be16(head, pcolours);
        if (!put(head, 2) || !put(ppal, 3 * pcolours))
            return FALSE;
    }
    n = count ? order_tiles(pvalid, pcols, (UWORD *)porder, x / TILE, y / TILE, tx1, ty1, mx, my) : 0;
    be32(head, n);
    if (!put(head, 4))
        return FALSE;
    dy = ext && !force ? find_dy(n) : 0;
    for (i = 0; i < n; i++) {
        LONG t = ((UWORD *)porder)[i], size, raw, best, method, p, k, b = 0;
        LONG ttx = t % pcols, tty = t / pcols, px = ttx * TILE, py = tty * TILE;
        LONG tw = px + TILE < pw ? TILE : pw - px, th = py + TILE < ph ? TILE : ph - py;
        LONG bw = (tw + 7) >> 3;
        BOOL had = !force && (pvalid[t] & 1);

        /* Every way we know, the shortest wins; penc(b) holds it. */
        raw = tile_bytes(pcur, ttx, tty, bw, th, ptile);
        best = packbits(ptile, raw, penc(0));
        method = 1;
        if (ext && best > raw / 4) {  /* rows against rows: text, borders, patterns.
             * Not for tiles PackBits already squeezed: a 68000 pays a second
             * pass for a percent or two. */
            row_xor(ptile, pwork, bw, th);
            if ((size = packbits(pwork, raw, penc(1 - b))) < best)
                best = size, method = 1 | 4, b = 1 - b;
        }
        if (had) {  /* the client has the old tile: send the difference */
            ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *o = (ULONG *)pold;
            tile_bytes(pref, ttx, tty, bw, th, pold);
            for (k = (raw + 3) >> 2; k > 0; k--)
                *a++ = *c++ ^ *o++;
            if ((size = packbits(pwork, raw, penc(1 - b))) < best)
                best = size, method = 2, b = 1 - b;
        }
        if (had && dy && shift_ok(ttx, tty, th, dy)) {  /* scrolled: the old rows, moved */
            ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *o = (ULONG *)pold;
            shifted_bytes(ttx, tty, bw, th, dy, pold);
            for (k = (raw + 3) >> 2; k > 0; k--)
                *a++ = *c++ ^ *o++;
            if ((size = packbits(pwork, raw, penc(1 - b) + 2) + 2) < best) {
                be16(penc(1 - b), (UWORD)dy);
                best = size, method = 2 | 8, b = 1 - b;
            }
        }
        if (best >= raw) {  /* noise beats every trick: raw then */
            memcpy(penc(b), ptile, raw);
            best = raw, method = 0;
        }
        be16(tile_buf, px);
        be16(tile_buf + 2, py);
        be16(tile_buf + 4, tw);
        be16(tile_buf + 6, th);
        tile_buf[8] = method;
        be32(tile_buf + 9, best);
        /* Out the door as soon as there is a packet's worth, and a look for
         * input between tiles: the pointer must not wait for the picture. */
        if (!put(tile_buf, 13) || !put(penc(b), best) ||
            (out_len >= SEND_EARLY && !flush()) ||
            !sneak_input()) {
            /* Half a frame out: no telling what the client holds. Start over. */
            for (p = 0; p < pcols * prows; p++)
                pvalid[p] &= ~SEND;
            pconn = powner = 0;
            return FALSE;
        }
    }
    /* All out: pref becomes the client's picture. Only now, so the shifted rows
     * above came from the old picture, as the client has it. */
    for (i = 0; i < n; i++) {
        LONG t = ((UWORD *)porder)[i], ttx = t % pcols, tty = t / pcols, p, at;
        LONG th = tty * TILE + TILE < ph ? TILE : ph - tty * TILE;
        LONG bw = ((ttx * TILE + TILE < pw ? TILE : pw - ttx * TILE) + 7) >> 3;

        for (p = 0, at = tty * TILE * pbpr + ttx * (TILE / 8); p < pdepth; p++, at += ph * pbpr)
            copy_rows(pref + at, pbpr, pcur + at, pbpr, bw, th);
        pvalid[t] = 1;
    }
    pconn = conn_serial;
    powner = client_id;
    return flush();
}

/* ---- chunky frames ---------------------------------------------------- */

/* Format 2, for graphics cards and everything else that isn't planar: the
 * RGB332 picture the classic frame keeps (cur, ref), with the tricks of the
 * planar tiles, plus a palette per tile. Most of a desktop is a handful of
 * colours a tile; 1, 2 or 4 bits a pixel say that in a quarter of the bytes. */
static ULONG cconn;  /* connection whose picture ref is, as pconn for planar */

/* The tile's RGB332 bytes from pic (cur or ref), rows of tw. */
static LONG chunky_bytes(const UBYTE *pic, LONG px, LONG py, LONG tw, LONG th, UBYTE *dst)
{
    const UBYTE *src = pic + py * ref_w + px;
    LONG y;

    for (y = 0; y < th; y++, src += ref_w, dst += tw)
        memcpy(dst, src, tw);
    return tw * th;
}

/* Up to 16 colours: u8 k, k colours, then rows of indices, 1/2/4 bits, top
 * bits first, each row on a byte. Returns the index rows' size, 0 if the tile
 * has too many colours; *head gets 1 + k. */
static LONG index_tile(const UBYTE *px, LONG tw, LONG th, UBYTE *dst, LONG *head)
{
    UBYTE slot[256], *rows;
    LONG k = 0, i, y, bits, rb;

    memset(slot, 0xFF, sizeof(slot));
    for (i = 0; i < tw * th; i++)
        if (slot[px[i]] == 0xFF) {
            if (k == 16)
                return 0;
            slot[px[i]] = k;
            dst[1 + k++] = px[i];
        }
    dst[0] = k;
    *head = 1 + k;
    bits = k <= 2 ? 1 : k <= 4 ? 2 : 4;
    rb = (tw * bits + 7) >> 3;
    rows = dst + *head;
    memset(rows, 0, rb * th);
    for (y = 0; y < th; y++, px += tw, rows += rb) {
        LONG x, shift = 8 - bits;
        UBYTE *o = rows;
        for (x = 0; x < tw; x++) {
            *o |= slot[px[x]] << shift;
            if ((shift -= bits) < 0)
                shift = 8 - bits, o++;
        }
    }
    return rb * th;
}

/* Did the client have every tile the shifted rows come from? */
static BOOL chunky_shift_ok(LONG tx, LONG py, LONG th, LONG dy)
{
    LONG top = py + dy, bottom = top + th - 1, r;

    if (top < 0) top = 0;
    if (bottom >= ref_h) bottom = ref_h - 1;
    for (r = top / TILE; r <= bottom / TILE && top <= bottom; r++)
        if (!(valid[r * cols + tx] & 1))
            return FALSE;
    return TRUE;
}

/* The scroll, as find_dy, on RGB332 rows; a row of one colour tells nothing. */
static LONG chunky_find_dy(const UWORD *order, LONG n)
{
    LONG i, r, s, k, tries = 0;

    for (i = 0; i < n && tries < 4; i++) {
        LONG t = order[i], tx = t % cols, py = (t / cols) * TILE;
        LONG tw = tx * TILE + TILE < ref_w ? TILE : ref_w - tx * TILE;
        LONG th = py + TILE < ref_h ? TILE : ref_h - py;

        if (!(valid[t] & 1))
            continue;
        tries++;
        for (r = 0; r + SHIFT_ROWS <= th; r++) {
            const UBYTE *row = cur + (py + r) * ref_w + tx * TILE;
            for (k = 1; k < tw && row[k] == row[0]; k++)
                ;
            if (k == tw || !memcmp(row, ref + (py + r) * ref_w + tx * TILE, tw))
                continue;
            for (s = 1; s <= SHIFT_MAX; s++) {
                LONG d, q;
                for (d = s; d >= -s; d -= 2 * s) {
                    for (q = 0; q < SHIFT_ROWS; q++) {
                        LONG sy = py + r + q + d;
                        if (sy < 0 || sy >= ref_h ||
                            memcmp(row + q * ref_w, ref + sy * ref_w + tx * TILE, tw))
                            break;
                    }
                    if (q == SHIFT_ROWS)
                        return d;
                }
            }
            break;
        }
    }
    return 0;
}

static BOOL frame_chunky(BOOL force, LONG x, LONG y, LONG w, LONG h)
{
    struct Screen *scr;
    ULONG lock;
    LONG sw, sh, x1, y1, tx, ty, tx1, ty1, n = 0, i, dy, mx, my;
    UBYTE head[12], *v;
    UWORD *order = NULL;
    BOOL known = cconn == conn_serial || (client_id && owner == client_id);

    if (!known)
        force = TRUE;
    x1 = w ? x + w : 0x7FFF;
    y1 = h ? y + h : 0x7FFF;
    head[0] = 2, head[1] = 8, head[2] = 0, head[3] = 0;
    if (!grab(force, x, y, x1, y1, &sw, &sh) ||
        !(order = AllocVec(cols * rows * sizeof(UWORD), MEMF_ANY))) {
        be16(head + 4, 0), be16(head + 6, 0), be32(head + 8, 0);
        return put(head, 12) && flush();
    }
    if (x1 > sw) x1 = sw;
    if (y1 > sh) y1 = sh;
    want_region(x, y, x1, y1);  /* the reader gets them fresh for next time */
    tx1 = (x1 - 1) / TILE, ty1 = (y1 - 1) / TILE;
    for (ty = y / TILE; ty <= ty1 && x < x1 && y < y1; ty++)
        for (tx = x / TILE; tx <= tx1; tx++) {
            v = valid + ty * cols + tx;
            if (force || !(*v & 1) || (*v & DIFF))
                *v |= SEND;
        }
    lock = LockIBase(0);
    scr = IntuitionBase->FirstScreen;
    mx = scr->MouseX, my = scr->MouseY;
    UnlockIBase(lock);
    if (x < x1 && y < y1)
        n = order_tiles(valid, cols, order, x / TILE, y / TILE, tx1, ty1, mx, my);
    be16(head + 4, sw), be16(head + 6, sh), be32(head + 8, n);
    if (!put(head, 12))
        goto fail;
    dy = force ? 0 : chunky_find_dy(order, n);
    for (i = 0; i < n; i++) {
        LONG t = order[i], ttx = t % cols, px = ttx * TILE, py = (t / cols) * TILE;
        LONG tw = px + TILE < sw ? TILE : sw - px, th = py + TILE < sh ? TILE : sh - py;
        LONG raw, best, size, method, k, b = 0, ihead, irows;
        BOOL had = !force && (valid[t] & 1);

        raw = chunky_bytes(cur, px, py, tw, th, ptile);
        if ((irows = index_tile(ptile, tw, th, pold, &ihead))) {
            /* A few colours: indices win nine times out of ten, so the full
             * bytes don't even get a PackBits pass. Saves the slow CPUs half. */
            UBYTE *o = penc(0);
            memcpy(o, pold, ihead);
            best = ihead + packbits(pold + ihead, irows, o + ihead);
            method = 1 | 16;
            o = penc(1 - b);
            memcpy(o, pold, ihead);
            xor_rows(pold + ihead, pwork, irows / th, th);
            if ((size = ihead + packbits(pwork, irows, o + ihead)) < best)
                best = size, method = 1 | 4 | 16, b = 1 - b;
        } else {
            best = packbits(ptile, raw, penc(0));
            method = 1;
            if (best > raw / 4) {  /* as for planar: only where there is room to win */
                xor_rows(ptile, pwork, tw, th);
                if ((size = packbits(pwork, raw, penc(1 - b))) < best)
                    best = size, method = 1 | 4, b = 1 - b;
            }
        }
        if (had) {  /* the client has the old tile */
            ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *o = (ULONG *)pold;
            chunky_bytes(ref, px, py, tw, th, pold);
            for (k = (raw + 3) >> 2; k > 0; k--)
                *a++ = *c++ ^ *o++;
            if ((size = packbits(pwork, raw, penc(1 - b))) < best)
                best = size, method = 2, b = 1 - b;
        }
        if (had && dy && chunky_shift_ok(ttx, py, th, dy)) {  /* scrolled */
            ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile;
            UBYTE *o = pold;
            LONG yy;
            for (yy = 0; yy < th; yy++, o += tw) {
                LONG sy = py + yy + dy;
                if (sy < 0 || sy >= sh)
                    memset(o, 0, tw);
                else
                    memcpy(o, ref + sy * ref_w + px, tw);
            }
            {
                ULONG *ol = (ULONG *)pold;
                for (k = (raw + 3) >> 2; k > 0; k--)
                    *a++ = *c++ ^ *ol++;
            }
            if ((size = packbits(pwork, raw, penc(1 - b) + 2) + 2) < best) {
                be16(penc(1 - b), (UWORD)dy);
                best = size, method = 2 | 8, b = 1 - b;
            }
        }
        if (best >= raw) {
            memcpy(penc(b), ptile, raw);
            best = raw, method = 0;
        }
        be16(tile_buf, px);
        be16(tile_buf + 2, py);
        be16(tile_buf + 4, tw);
        be16(tile_buf + 6, th);
        tile_buf[8] = method;
        be32(tile_buf + 9, best);
        if (!put(tile_buf, 13) || !put(penc(b), best) ||
            (out_len >= SEND_EARLY && !flush()) || !sneak_input())
            goto fail;
    }
    /* All out: ref becomes the client's picture, now and not earlier, so the
     * shifted rows came from the old one. */
    for (i = 0; i < n; i++) {
        LONG t = order[i], px = (t % cols) * TILE, py = (t / cols) * TILE, at;
        LONG tw = px + TILE < sw ? TILE : sw - px, th = py + TILE < sh ? TILE : sh - py;
        for (at = py * ref_w + px; at < (py + th) * ref_w; at += ref_w)
            memcpy(ref + at, cur + at, tw);
        valid[t] = (valid[t] & ~(SEND | DIFF)) | 1;
    }
    cconn = conn_serial;
    owner = client_id;
    FreeVec(order);
    return flush();
fail:
    clear_marks();
    cconn = owner = 0;  /* half a frame out: start over next time */
    FreeVec(order);
    return FALSE;
}

static BOOL info(void)
{
    UBYTE msg[8];
    ULONG lock;

    lock = LockIBase(0);
    be16(msg, IntuitionBase->FirstScreen->Width);
    be16(msg + 2, IntuitionBase->FirstScreen->Height);
    UnlockIBase(lock);
    be16(msg + 4, TILE);
    be16(msg + 6, pause_left());  /* protocol 4; was always 0 before */
    return put(msg, 8) && flush();
}

/* ---- protocol 2: screens, true colour, user interface ------------------ */

/* A growing reply buffer for the variable length answers. */
struct text {
    UBYTE *buf;
    ULONG len, size;
    BOOL broke;
};

static void t_add(struct text *t, const void *data, ULONG n)
{
    if (t->broke)
        return;
    if (t->len + n > t->size) {
        ULONG size = t->size ? t->size : 4096;
        UBYTE *bigger;
        while (size < t->len + n)
            size *= 2;
        if (!(bigger = AllocVec(size, MEMF_ANY))) {
            t->broke = TRUE;
            return;
        }
        if (t->buf) {
            CopyMem(t->buf, bigger, t->len);
            FreeVec(t->buf);
        }
        t->buf = bigger;
        t->size = size;
    }
    CopyMem((APTR)data, t->buf + t->len, n);
    t->len += n;
}

static void t_chr(struct text *t, char c)
{
    t_add(t, &c, 1);
}

/* A name or label: tabs and line ends become spaces; underscores go if marks is set. */
static void t_str(struct text *t, const UBYTE *s, BOOL marks)
{
    LONG n;

    for (n = 0; s && *s && n < 256; s++, n++) {
        if (marks && *s == '_' && s[1] && s[1] != '_')
            continue;
        t_chr(t, *s == '\t' || *s == '\n' || *s == '\r' ? ' ' : *s);
    }
}

static void t_num(struct text *t, LONG v)
{
    char d[12];
    LONG n = 0;
    ULONG u = v < 0 ? -v : v;

    if (v < 0)
        t_chr(t, '-');
    do
        d[n++] = '0' + u % 10;
    while (u /= 10);
    while (n)
        t_chr(t, d[--n]);
}

static void t_hex(struct text *t, ULONG v)
{
    LONG i;

    for (i = 28; i >= 0; i -= 4)
        t_chr(t, "0123456789abcdef"[v >> i & 15]);
}

static void t_tab(struct text *t)
{
    t_chr(t, '\t');
}

/* u32 length, then the text. */
static BOOL send_text(struct text *t)
{
    UBYTE head[4];
    BOOL ok;

    if (t->broke)
        t->len = 0;
    be32(head, t->len);
    ok = put(head, 4) && put(t->buf, t->len) && flush();
    FreeVec(t->buf);
    return ok;
}

static const UBYTE *screen_title(struct Screen *s)
{
    return s->DefaultTitle ? s->DefaultTitle : s->Title;
}

static BOOL screens(void)
{
    struct text t = {0};
    struct Screen *s;
    ULONG lock;
    UBYTE b[16] = {0};
    UWORD count = 0;
    BOOL ok;

    t_add(&t, b, 2);  /* count, filled in below */
    lock = LockIBase(0);
    for (s = IntuitionBase->FirstScreen; s; s = s->NextScreen) {
        const UBYTE *title = screen_title(s);
        UWORD flags = 0, n = title ? strlen((const char *)title) : 0;
        if (s == IntuitionBase->FirstScreen) flags |= 1;
        if ((s->Flags & SCREENTYPE) == WBENCHSCREEN) flags |= 2;
        if (is_rtg(s)) flags |= 4;
        if (GetVPModeID(&s->ViewPort) & HAM_KEY) flags |= 8;
        be32(b, (ULONG)s);
        be16(b + 4, s->Width);
        be16(b + 6, s->Height);
        be16(b + 8, bm_depth(s->RastPort.BitMap));
        be16(b + 10, flags);
        be16(b + 12, n);
        t_add(&t, b, 14);
        t_add(&t, title, n);
        count++;
    }
    UnlockIBase(lock);
    if (t.broke) {
        FreeVec(t.buf);
        be16(b, 0);
        return put(b, 2) && flush();
    }
    be16(t.buf, count);
    ok = put(t.buf, t.len) && flush();
    FreeVec(t.buf);
    return ok;
}

static struct Screen *open_screen(ULONG handle)
{
    struct Screen *s;
    ULONG lock;

    lock = LockIBase(0);
    for (s = IntuitionBase->FirstScreen; s && (ULONG)s != handle; s = s->NextScreen)
        ;
    UnlockIBase(lock);
    return s;
}

/* ScreenToFront waits for Intuition, which holds it back while a menu is open,
 * for as long as that may be. So a helper process waits instead of us. */
static void __saveds front_main(void)
{
    struct Screen *s;

    Wait(SIGBREAKF_CTRL_F);
    if ((s = open_screen((ULONG)FindTask(NULL)->tc_UserData)))
        ScreenToFront(s);
    Forbid();
    children--;
    Signal(server, 1UL << chan_gone);
}

/* Only a screen that is still in the list gets brought to the front. */
static BOOL screen_front(ULONG handle)
{
    struct Process *proc;

    if (!open_screen(handle))
        return reply(1);
    Forbid();
    children++;
    Permit();
    proc = CreateNewProcTags(NP_Entry, (ULONG)front_main, NP_Name, (ULONG)"amigentd front",
                             NP_Priority, 0, NP_StackSize, 4096, TAG_END);
    if (!proc) {
        Forbid();
        children--;
        Permit();
        return reply(1);
    }
    proc->pr_Task.tc_UserData = (APTR)handle;
    Signal(&proc->pr_Task, SIGBREAKF_CTRL_F);
    input_ms = now_ms();  /* what was read before shows another screen */
    return reply(0);
}

/* Runs of equal pixels as u8 count-1, r, g, b, crossing row ends. Returns the
   packed length, or 0 once it would reach len, the raw size. */
static ULONG pack_rgb(const UBYTE *p, ULONG len, UBYTE *out)
{
    const UBYTE *end = p + len;
    ULONG n = 0;

    while (p < end) {
        const UBYTE *q = p + 3;
        while (q < end && q - p < 256 * 3 && q[0] == p[0] && q[1] == p[1] && q[2] == p[2])
            q += 3;
        if (n + 4 >= len)
            return 0;
        out[n] = (q - p) / 3 - 1;
        out[n + 1] = p[0];
        out[n + 2] = p[1];
        out[n + 3] = p[2];
        n += 4;
        p = q;
    }
    return n;
}

static BOOL frame_rgb(BOOL packed, LONG x, LONG y, LONG w, LONG h)
{
    struct Screen *scr;
    ULONG lock, len, n = 0;
    UBYTE head[8], *buf = NULL, *runs = NULL;
    BOOL ok;

    lock = LockIBase(0);
    scr = IntuitionBase->FirstScreen;
    if (!w || x + w > scr->Width) w = scr->Width - x;
    if (!h || y + h > scr->Height) h = scr->Height - y;
    if (w <= 0 || h <= 0 || w * h > RGB_MAX || !(buf = AllocVec(w * h * 3, MEMF_ANY)))
        w = h = 0;
    else
        read_rgb(scr, x, y, w, h, buf);
    UnlockIBase(lock);
    len = w * h * 3;
    if (packed && len && (runs = AllocVec(len, MEMF_ANY)))
        n = pack_rgb(buf, len, runs);
    be16(head, w);
    be16(head + 2, h);
    if (!packed)
        ok = put(head, 4) && put(buf, len);
    else if (n) {
        be32(head + 4, n);
        ok = put(head, 8) && put(runs, n);
    } else {
        be32(head + 4, len);
        ok = put(head, 8) && put(buf, len);
    }
    ok = ok && flush();
    FreeVec(runs);
    FreeVec(buf);
    return ok;
}

/* Gadget box on the screen, from the window's place and the relative flags. */
static void gadget_box(struct Window *w, struct Gadget *g, LONG *box)
{
    LONG x = g->LeftEdge, y = g->TopEdge, gw = g->Width, gh = g->Height;

    if (g->Flags & GFLG_RELRIGHT) x += w->Width - 1;
    if (g->Flags & GFLG_RELBOTTOM) y += w->Height - 1;
    if (g->Flags & GFLG_RELWIDTH) gw += w->Width;
    if (g->Flags & GFLG_RELHEIGHT) gh += w->Height;
    if ((w->Flags & WFLG_GIMMEZEROZERO) && !(g->GadgetType & GTYP_GZZGADGET)) {
        x += w->BorderLeft;
        y += w->BorderTop;
    }
    box[0] = w->LeftEdge + x;
    box[1] = w->TopEdge + y;
    box[2] = gw;
    box[3] = gh;
}

static void t_box(struct text *t, LONG *box)
{
    LONG i;

    for (i = 0; i < 4; i++) {
        t_tab(t);
        t_num(t, box[i]);
    }
}

static void t_itext(struct text *t, struct IntuiText *it)
{
    BOOL first = TRUE;

    for (; it; it = it->NextText) {
        if (!it->IText)
            continue;
        if (!first)
            t_chr(t, ' ');
        t_str(t, it->IText, TRUE);
        first = FALSE;
    }
}

/* GadTools buttons carry their label as an itexticlass image; its data is the text. */
static struct IntuiText *image_text(struct Image *im)
{
    struct IClass *cl;

    if (!im || im->Depth != CUSTOMIMAGEDEPTH)
        return NULL;
    cl = OCLASS(im);
    if (!cl || !cl->cl_ID || strcmp((const char *)cl->cl_ID, "itexticlass"))
        return NULL;
    return (struct IntuiText *)im->ImageData;
}

static BOOL ui_tree(void)
{
    static const char *const sys[] = {"", "size", "drag", "sdrag", "depth", "sdepth",
                                      "zoom", "", "close"};
    struct text t = {0};
    struct Screen *s;
    struct Layer *l;
    ULONG lock;

    lock = LockIBase(0);
    s = IntuitionBase->FirstScreen;
    t_chr(&t, 'S');
    t_tab(&t);
    t_hex(&t, (ULONG)s);
    t_tab(&t);
    t_num(&t, s->Width);
    t_tab(&t);
    t_num(&t, s->Height);
    t_tab(&t);
    t_str(&t, screen_title(s), FALSE);
    t_chr(&t, '\n');
    /* Layers run front to back; the window list does not. */
    for (l = s->LayerInfo.top_layer; l; l = l->back) {
        struct Window *w;
        struct Gadget *g;
        LONG box[4], n = 0;

        for (w = s->FirstWindow; w && w != (struct Window *)l->Window; w = w->NextWindow)
            ;
        if (!w)
            continue;
        box[0] = w->LeftEdge, box[1] = w->TopEdge, box[2] = w->Width, box[3] = w->Height;
        t_chr(&t, 'W');
        t_tab(&t);
        t_hex(&t, (ULONG)w);
        t_box(&t, box);
        t_tab(&t);
        if (w == IntuitionBase->ActiveWindow) t_chr(&t, 'a');
        if (w->Flags & WFLG_BACKDROP) t_chr(&t, 'b');
        t_tab(&t);
        t_str(&t, w->Title, FALSE);
        t_chr(&t, '\n');
        for (g = w->FirstGadget; g; g = g->NextGadget, n++) {
            UWORD type = g->GadgetType;
            gadget_box(w, g, box);
            t_chr(&t, 'G');
            t_tab(&t);
            t_hex(&t, (ULONG)w);
            t_tab(&t);
            t_num(&t, n);
            t_box(&t, box);
            t_tab(&t);
            if (type & GTYP_SYSGADGET)
                t_str(&t, (const UBYTE *)"sys", FALSE);
            else switch (type & GTYP_GTYPEMASK) {
            case GTYP_BOOLGADGET:
                t_str(&t, (const UBYTE *)(g->Activation & GACT_TOGGLESELECT ? "check" : "button"), FALSE);
                break;
            case GTYP_PROPGADGET: t_str(&t, (const UBYTE *)"prop", FALSE); break;
            case GTYP_STRGADGET: t_str(&t, (const UBYTE *)"string", FALSE); break;
            default: t_str(&t, (const UBYTE *)"other", FALSE); break;
            }
            t_tab(&t);
            if (g->Flags & GFLG_DISABLED) t_chr(&t, 'd');
            if (g->Flags & GFLG_SELECTED) t_chr(&t, 's');
            t_tab(&t);
            if (type & GTYP_SYSGADGET)
                t_str(&t, (const UBYTE *)sys[(type & GTYP_SYSTYPEMASK) >> 4 <= 8 ?
                                             (type & GTYP_SYSTYPEMASK) >> 4 : 0], FALSE);
            else if ((g->Flags & GFLG_LABELMASK) == GFLG_LABELSTRING)
                t_str(&t, (const UBYTE *)g->GadgetText, TRUE);
            else if ((g->Flags & GFLG_LABELMASK) == GFLG_LABELITEXT)
                t_itext(&t, g->GadgetText);
            else
                t_itext(&t, image_text((struct Image *)g->GadgetText));
            t_chr(&t, '\n');
        }
    }
    UnlockIBase(lock);
    return send_text(&t);
}

static void t_item(struct text *t, struct MenuItem *it, LONG *box)
{
    t_box(t, box);
    t_tab(t);
    if (it->Flags & ITEMENABLED) t_chr(t, 'e');
    if ((it->Flags & CHECKIT) && (it->Flags & CHECKED)) t_chr(t, 'c');
    t_tab(t);
    if ((it->Flags & COMMSEQ) && it->Command)
        t_chr(t, it->Command);
    t_tab(t);
    if ((it->Flags & ITEMTEXT) && it->ItemFill)
        t_itext(t, (struct IntuiText *)it->ItemFill);
    else
        t_str(t, (const UBYTE *)"(image)", FALSE);
    t_chr(t, '\n');
}

/* Boxes as Intuition places them: titles in the screen bar, items below their title,
 * sub items next to their item. Menu patches that draw elsewhere are not seen. */
static BOOL menus(void)
{
    struct text t = {0};
    struct Window *w;
    struct Menu *m;
    ULONG lock;
    LONG mi = 0;

    lock = LockIBase(0);
    w = IntuitionBase->ActiveWindow;
    if (w) {
        /* RMBTRAP: the right button goes to the program, the strip never shows. */
        t_chr(&t, 'A');
        t_tab(&t);
        t_hex(&t, (ULONG)w);
        t_tab(&t);
        if (w->Flags & WFLG_RMBTRAP) t_chr(&t, 'r');
        t_tab(&t);
        t_str(&t, w->Title, FALSE);
        t_chr(&t, '\n');
    }
    for (m = w ? w->MenuStrip : NULL; m; m = m->NextMenu, mi++) {
        struct Screen *s = w->WScreen;
        struct MenuItem *it;
        LONG box[4], ii = 0, bar = s->BarHeight + 1;

        box[0] = m->LeftEdge, box[1] = 0, box[2] = m->Width, box[3] = bar;
        t_chr(&t, 'M');
        t_tab(&t);
        t_num(&t, mi);
        t_box(&t, box);
        t_tab(&t);
        if (m->Flags & MENUENABLED) t_chr(&t, 'e');
        t_tab(&t);
        t_str(&t, (const UBYTE *)m->MenuName, FALSE);
        t_chr(&t, '\n');
        for (it = m->FirstItem; it; it = it->NextItem, ii++) {
            struct MenuItem *sub;
            LONG ix = m->LeftEdge + it->LeftEdge + MENU_X, iy = bar + it->TopEdge + MENU_Y, si = 0;

            box[0] = ix, box[1] = iy, box[2] = it->Width, box[3] = it->Height;
            t_chr(&t, 'I');
            t_tab(&t);
            t_num(&t, mi);
            t_tab(&t);
            t_num(&t, ii);
            t_item(&t, it, box);
            for (sub = it->SubItem; sub; sub = sub->NextItem, si++) {
                box[0] = ix + sub->LeftEdge + SUB_X, box[1] = iy + sub->TopEdge + SUB_Y;
                box[2] = sub->Width, box[3] = sub->Height;
                t_chr(&t, 'U');
                t_tab(&t);
                t_num(&t, mi);
                t_tab(&t);
                t_num(&t, ii);
                t_tab(&t);
                t_num(&t, si);
                t_item(&t, sub, box);
            }
        }
    }
    UnlockIBase(lock);
    return send_text(&t);
}

/* ---- setup ------------------------------------------------------------ */

static BOOL put_file(UWORD path_len, ULONG size)
{
    UBYTE *buf = AllocVec(CHUNK, MEMF_ANY);
    char *path = AllocVec(path_len + 1, MEMF_ANY);
    BPTR fh = 0;
    BOOL alive = FALSE, ok = FALSE;

    if (!buf || !path) {
        FreeVec(buf);
        FreeVec(path);
        return skip(path_len) && skip(size) && reply(1);
    }
    if (!receive(path, path_len))
        goto done;
    path[path_len] = 0;
    if (setup_mode)
        fh = Open(path, MODE_NEWFILE);
    ok = fh != 0;
    while (size > 0) {
        ULONG n = size > CHUNK ? CHUNK : size;
        if (!receive(buf, n))
            goto done;
        if (ok && Write(fh, buf, n) != n)
            ok = FALSE;
        size -= n;
    }
    alive = TRUE;
done:
    if (fh) {
        Close(fh);
        if (!ok || !alive)
            DeleteFile(path);
    }
    FreeVec(buf);
    FreeVec(path);
    return alive && reply(ok ? 0 : 1);
}

static BOOL run_command(UWORD len)
{
    char *line = AllocVec(len + 1, MEMF_ANY);
    BPTR in, nil;
    LONG rc = 255;

    if (!line)
        return skip(len) && reply(255);
    if (!receive(line, len)) {
        FreeVec(line);
        return FALSE;
    }
    line[len] = 0;
    if (setup_mode) {
        in = Open("NIL:", MODE_OLDFILE);
        nil = Open("NIL:", MODE_NEWFILE);
        if (in && nil) {
            rc = SystemTags(line, SYS_Input, in, SYS_Output, nil,
                            NP_StackSize, 65536, NP_Priority, 0, NP_WindowPtr, -1, TAG_END);
            if (rc < 0 || rc > 255)
                rc = 255;
        }
        if (in) Close(in);
        if (nil) Close(nil);
    }
    FreeVec(line);
    return reply(rc);
}

/* ---- connections ------------------------------------------------------ */

static void drop_client(void)
{
    if (client >= 0) {
        /* Reset rather than close: whatever still sits in the send buffer would
         * otherwise hold up the next client on a slow link. */
        struct linger now = {1, 0};
        setsockopt(client, SOL_SOCKET, SO_LINGER, &now, sizeof(now));
        CloseSocket(client);
        client = -1;
    }
    out_len = 0;
    in_have = nacks = 0;
    set_buttons(0);
    ObtainSemaphore(&picture);
    if (want)
        memset(want, 0, cols * rows);
    wanted = 0;
    ReleaseSemaphore(&picture);
}

/* First line of the key file, at most KEY_MAX bytes, trailing blanks cut. Read for
 * every connection, so a key put there during SETUP counts from the next one on. */
static void load_key(void)
{
    ULONG words[KEY_MAX / 4];  /* even address for the 68000 */
    UBYTE *buf = (UBYTE *)words;
    BPTR fh = Open(secret_file, MODE_OLDFILE);
    LONG n = 0, i;

    secret_len = 0;
    if (!fh)
        return;
    n = Read(fh, buf, KEY_MAX);
    Close(fh);
    if (n <= 0)
        return;
    for (i = 0; i < n && buf[i] != '\n' && buf[i] != '\r'; i++)
        ;
    while (i > 0 && (buf[i - 1] == ' ' || buf[i - 1] == '\t'))
        i--;
    memset(secret, 0, sizeof(secret));
    memcpy(secret, buf, i);
    secret_len = i;
}

/* A failed attempt costs a second for everyone: one guess per second at most. */
static BOOL refuse(void)
{
    reply(1);
    Delay(50);
    return FALSE;
}

static BOOL key_matches(const UBYTE *got, UWORD len)
{
    UBYTE diff = len != secret_len;
    LONG i;

    for (i = 0; i < KEY_MAX; i++)  /* the same work whatever matches */
        diff |= got[i] ^ secret[i];
    return !secret_len || !diff;
}

static BOOL auth(UWORD len)
{
    UBYTE got[KEY_MAX];

    memset(got, 0, sizeof(got));
    if (!receive(got, len < KEY_MAX ? len : KEY_MAX) || (len > KEY_MAX && !skip(len - KEY_MAX)))
        return FALSE;
    if (!key_matches(got, len))
        return refuse();
    authed = TRUE;
    return reply(0);
}

/* ---- links: small blocking transfers on any socket, from any process ---- */

struct link {
    struct Library *base;  /* each process has its own bsdsocket.library */
    LONG fd;
};

/* secs 0 waits for ever. A Ctrl-C ends the wait and stays set for the caller. */
static BOOL l_wait(struct link *l, BOOL writing, LONG secs)
{
    struct Library *SocketBase = l->base;
    struct timeval tv;
    ULONG sigs = SIGBREAKF_CTRL_C;
    fd_set s;

    tv.tv_sec = secs;
    tv.tv_usec = 0;
    FD_ZERO(&s);
    FD_SET(l->fd, &s);
    if (WaitSelect(l->fd + 1, writing ? NULL : &s, writing ? &s : NULL, NULL,
                   secs ? &tv : NULL, &sigs) > 0)
        return TRUE;
    if (sigs & SIGBREAKF_CTRL_C)
        SetSignal(SIGBREAKF_CTRL_C, SIGBREAKF_CTRL_C);
    return FALSE;
}

static BOOL l_send(struct link *l, const void *data, ULONG len, LONG secs)
{
    struct Library *SocketBase = l->base;
    const UBYTE *p = data;
    LONG n;

    while (len > 0) {
        if (!l_wait(l, TRUE, secs))
            return FALSE;
        n = send(l->fd, (UBYTE *)p, len, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
    }
    return TRUE;
}

static BOOL l_recv(struct link *l, void *data, ULONG len, LONG secs)
{
    struct Library *SocketBase = l->base;
    UBYTE *p = data;
    LONG n;

    while (len > 0) {
        if (!l_wait(l, FALSE, secs))
            return FALSE;
        n = recv(l->fd, p, len, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
    }
    return TRUE;
}

static BOOL l_skip(struct link *l, ULONG len, LONG secs)
{
    UBYTE scratch[512];

    while (len > 0) {
        ULONG n = len > sizeof(scratch) ? sizeof(scratch) : len;
        if (!l_recv(l, scratch, n, secs))
            return FALSE;
        len -= n;
    }
    return TRUE;
}

/* ---- newcomers --------------------------------------------------------- */

/* A connection only becomes the screen client with its first screen command, so
 * one that sends its key or asks for a command channel leaves the screen alone. */

static void close_fd(LONG fd)
{
    struct linger now = {1, 0};

    setsockopt(fd, SOL_SOCKET, SO_LINGER, &now, sizeof(now));
    CloseSocket(fd);
}

static void close_pending(LONG i)
{
    close_fd(pend[i].fd);
    pend[i].fd = -1;
}

static void accept_pending(void)
{
    struct sockaddr_in addr;
    LONG len = sizeof(addr), fd, one = 1, low = SEND_STEP, buffer = SEND_BUFFER, i, old = 0;
    BOOL locked;
    UBYTE hello[8] = {'A', 'M', 'G', 'T', 0, PROTOCOL_VERSION, 0, 0};
    struct link l;

    fd = accept(listener, (struct sockaddr *)&addr, &len);
    if (fd < 0)
        return;
    for (i = 0; i < PENDING && pend[i].fd >= 0; i++)
        if (pend[i].since < pend[old].since)
            old = i;
    if (i == PENDING) {  /* no room: the one waiting longest goes */
        close_pending(old);
        i = old;
    }
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    IoctlSocket(fd, FIONBIO, (char *)&one);  /* waits go through WaitSelect */
    setsockopt(fd, SOL_SOCKET, SO_SNDLOWAT, &low, sizeof(low));
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
    load_key();
    locked = tls_only && tls_open && !setup_mode;
    hello[7] = (secret_len ? 1 : 0) | (tls_ready ? 2 : 0) | (locked ? 4 : 0);
    l.base = SocketBase;
    l.fd = fd;
    /* TLSONLY: strangers get the hello, so they know where the door is, then out.
     * 127.0.0.1 is our own TLS pumps, and whoever sits at the Amiga. */
    if (!l_send(&l, hello, 8, HELLO_SECONDS) ||
        (locked && addr.sin_addr.s_addr != htonl(INADDR_LOOPBACK))) {
        close_fd(fd);
        return;
    }
    pend[i].fd = fd;
    pend[i].addr = addr.sin_addr.s_addr;
    pend[i].have = 0;
    pend[i].authed = secret_len == 0;
    pend[i].screen = FALSE;
    pend[i].since = now_ms();
}

static void start_channel(LONG i);

/* The newcomer has sent something: a key, a channel request, or a screen command. */
static void step(LONG i)
{
    struct newcomer *p = &pend[i];
    struct link l;
    LONG n;

    l.base = SocketBase;
    l.fd = p->fd;
    n = recv(p->fd, p->cmd + p->have, sizeof(p->cmd) - p->have, 0);
    if (n < 0 && Errno() == EWOULDBLOCK)
        return;
    if (n <= 0) {
        close_pending(i);
        return;
    }
    if ((p->have += n) < sizeof(p->cmd))
        return;
    if (p->cmd[0] == CMD_AUTH) {
        UWORD len = p->cmd[2] << 8 | p->cmd[3];
        UBYTE got[KEY_MAX];

        memset(got, 0, sizeof(got));
        if (!l_recv(&l, got, len < KEY_MAX ? len : KEY_MAX, HELLO_SECONDS) ||
            (len > KEY_MAX && !l_skip(&l, len - KEY_MAX, HELLO_SECONDS))) {
            close_pending(i);
            return;
        }
        p->have = 0;
        if (key_matches(got, len)) {
            p->authed = TRUE;
            if (!l_send(&l, "\0", 1, HELLO_SECONDS))
                close_pending(i);
            return;
        }
    } else if (p->authed) {
        if (p->cmd[0] == CMD_CHANNEL)
            start_channel(i);
        else
            p->screen = TRUE;  /* serve() hands it the screen */
        return;
    }
    l_send(&l, "\1", 1, HELLO_SECONDS);
    Delay(50);  /* as refuse() */
    close_pending(i);
}

/* Adds the listener and the newcomers to r; drops those silent for too long. */
static LONG watch(fd_set *r, LONG top)
{
    LONG i;

    FD_SET(listener, r);
    if (listener > top)
        top = listener;
    for (i = 0; i < PENDING; i++) {
        if (pend[i].fd < 0 || pend[i].screen)
            continue;
        if (now_ms() - pend[i].since > IDLE_SECONDS * 1000) {
            close_pending(i);
            continue;
        }
        FD_SET(pend[i].fd, r);
        if (pend[i].fd > top)
            top = pend[i].fd;
    }
    return top;
}

/* Serves what watch() found ready. TRUE if a newcomer wants the screen. */
static BOOL newcomers(fd_set *r)
{
    LONG i;
    BOOL wants = FALSE;

    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0 && !pend[i].screen && FD_ISSET(pend[i].fd, r))
            step(i);
    if (FD_ISSET(listener, r))
        accept_pending();
    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0 && pend[i].screen)
            wants = TRUE;
    return wants;
}

/* ---- command channels -------------------------------------------------- */

/* Every reply starts with a status: 0, or an error with u16 length and text. */
static BOOL c_ok(struct link *l)
{
    return l_send(l, "\0", 1, IO_SECONDS);
}

static BOOL c_fail(struct link *l, UBYTE status, const char *text)
{
    UBYTE head[3];
    UWORD n = strlen(text);

    head[0] = status;
    be16(head + 1, n);
    return l_send(l, head, 3, IO_SECONDS) && l_send(l, text, n, IO_SECONDS);
}

/* The AmigaDOS text for an error code. */
static BOOL c_fault(struct link *l, LONG code)
{
    ULONG words[25];  /* even address for the 68000 */
    char *buf = (char *)words;

    if (!code)
        return c_fail(l, 1, "failed");
    Fault(code, NULL, buf, sizeof(words));
    return c_fail(l, 1, buf);
}

static BOOL c_text(struct link *l, struct text *t)
{
    UBYTE head[5];
    BOOL ok;

    if (t->broke) {
        FreeVec(t->buf);
        return c_fault(l, ERROR_NO_FREE_STORE);
    }
    head[0] = 0;
    be32(head + 1, t->len);
    ok = l_send(l, head, 5, IO_SECONDS) && l_send(l, t->buf, t->len, IO_SECONDS);
    FreeVec(t->buf);
    return ok;
}

/* A name of len bytes from the request. NULL with *alive still set: no memory. */
static char *c_name(struct link *l, UWORD len, BOOL *alive)
{
    char *name = AllocVec(len + 1, MEMF_ANY);

    *alive = name ? l_recv(l, name, len, IO_SECONDS) : l_skip(l, len, IO_SECONDS);
    if (name && !*alive) {
        FreeVec(name);
        return NULL;
    }
    if (name)
        name[len] = 0;
    return name;
}

static ULONG next_run(void)
{
    ULONG n;

    Forbid();
    n = ++runs;
    Permit();
    return n;
}

/* Decimal into p, with a 0 at the end. */
static void dec(char *p, ULONG v)
{
    char d[12];
    LONG n = 0;

    do
        d[n++] = '0' + v % 10;
    while (v /= 10);
    while (n)
        *p++ = d[--n];
    *p = 0;
}

static void t_pad(struct text *t, LONG v, LONG digits)
{
    char d[12];
    LONG n;

    dec(d, v);
    for (n = strlen(d); n < digits; n++)
        t_chr(t, '0');
    t_str(t, (const UBYTE *)d, FALSE);
}

/* YYYY-MM-DD HH:MM:SS */
static void t_date(struct text *t, const struct DateStamp *ds)
{
    static const UBYTE month_days[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    LONG days = ds->ds_Days, y = 1978, m = 0, len;
    BOOL leap;

    for (;;) {
        leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
        if (days < (len = leap ? 366 : 365))
            break;
        days -= len;
        y++;
    }
    while (days >= (len = month_days[m] + (m == 1 && leap))) {
        days -= len;
        m++;
    }
    t_pad(t, y, 4);
    t_chr(t, '-');
    t_pad(t, m + 1, 2);
    t_chr(t, '-');
    t_pad(t, days + 1, 2);
    t_chr(t, ' ');
    t_pad(t, ds->ds_Minute / 60, 2);
    t_chr(t, ':');
    t_pad(t, ds->ds_Minute % 60, 2);
    t_chr(t, ':');
    t_pad(t, ds->ds_Tick / TICKS_PER_SECOND, 2);
}

/* FILE|DIR, name, size, protection, date; one line. */
static void t_entry(struct text *t, struct FileInfoBlock *fib)
{
    BOOL dir = fib->fib_DirEntryType > 0 && fib->fib_DirEntryType != ST_SOFTLINK;

    t_str(t, (const UBYTE *)(dir ? "DIR" : "FILE"), FALSE);
    t_tab(t);
    t_str(t, (const UBYTE *)fib->fib_FileName, FALSE);
    t_tab(t);
    t_num(t, dir ? 0 : fib->fib_Size);
    t_tab(t);
    t_hex(t, fib->fib_Protection);
    t_tab(t);
    t_date(t, &fib->fib_Date);
    t_chr(t, '\n');
}

static BOOL c_dir(struct link *l, UWORD len, BOOL itself)
{
    struct text t = {0};
    struct FileInfoBlock *fib = AllocDosObject(DOS_FIB, NULL);
    BPTR lock = 0;
    LONG err = 0;
    BOOL alive;
    char *path = c_name(l, len, &alive);

    if (!alive) {
        FreeDosObject(DOS_FIB, fib);
        return FALSE;
    }
    if (!path || !fib)
        err = ERROR_NO_FREE_STORE;
    else if (!(lock = Lock(path, SHARED_LOCK)) || !Examine(lock, fib))
        err = IoErr();
    else if (itself)
        t_entry(&t, fib);
    else if (fib->fib_DirEntryType <= 0)
        err = ERROR_OBJECT_WRONG_TYPE;
    else {
        while (ExNext(lock, fib))
            t_entry(&t, fib);
        if (IoErr() != ERROR_NO_MORE_ENTRIES)
            err = IoErr();
    }
    if (lock)
        UnLock(lock);
    FreeDosObject(DOS_FIB, fib);
    FreeVec(path);
    if (err) {
        FreeVec(t.buf);
        return c_fault(l, err);
    }
    return c_text(l, &t);
}

static BOOL c_read(struct link *l, UWORD len)
{
    struct FileInfoBlock *fib = AllocDosObject(DOS_FIB, NULL);
    UBYTE *buf = AllocVec(CHUNK, MEMF_ANY), head[5];
    BPTR fh = 0;
    LONG err = 0, n;
    ULONG size = 0;
    BOOL alive;
    char *path = c_name(l, len, &alive);

    if (alive) {
        if (!path || !fib || !buf)
            err = ERROR_NO_FREE_STORE;
        else if (!(fh = Open(path, MODE_OLDFILE)) || !ExamineFH(fh, fib))
            err = IoErr();
        else
            size = fib->fib_Size;
        if (err)
            alive = c_fault(l, err);
        else {
            head[0] = 0;
            be32(head + 1, size);
            alive = l_send(l, head, 5, IO_SECONDS);
            /* The size is out, so a read error can only end the link. */
            while (alive && size > 0) {
                n = Read(fh, buf, size > CHUNK ? CHUNK : size);
                alive = n > 0 && l_send(l, buf, n, IO_SECONDS);
                size -= n;
            }
        }
    }
    if (fh)
        Close(fh);
    FreeDosObject(DOS_FIB, fib);
    FreeVec(buf);
    FreeVec(path);
    return alive;
}

static BOOL is_dir(const char *path)
{
    struct FileInfoBlock *fib = AllocDosObject(DOS_FIB, NULL);
    BPTR lock = Lock(path, SHARED_LOCK);
    BOOL dir = FALSE;

    if (lock && fib && Examine(lock, fib))
        dir = fib->fib_DirEntryType > 0;
    if (lock)
        UnLock(lock);
    FreeDosObject(DOS_FIB, fib);
    return dir;
}

/* A new file is written under a temporary name next to it and renamed at the
 * end, so a broken transfer never leaves half a file. */
static BOOL c_write(struct link *l, BOOL append, UWORD len, ULONG size)
{
    UBYTE *buf = AllocVec(CHUNK, MEMF_ANY);
    char *tmp = NULL, name[24];
    BPTR fh = 0;
    LONG err = 0, n;
    BOOL alive;
    char *path = c_name(l, len, &alive);

    if (!alive) {
        FreeVec(buf);
        return FALSE;
    }
    if (!path || !buf || (!append && !(tmp = AllocVec(len + sizeof(name) + 2, MEMF_ANY))))
        err = ERROR_NO_FREE_STORE;
    else if (is_dir(path))
        err = ERROR_OBJECT_WRONG_TYPE;
    else if (append) {
        if ((fh = Open(path, MODE_READWRITE)))
            Seek(fh, 0, OFFSET_END);
    } else {
        n = (char *)PathPart(path) - path;
        CopyMem(path, tmp, n);
        tmp[n] = 0;
        strcpy(name, "amigentd-");
        dec(name + strlen(name), next_run());
        strcat(name, ".tmp");
        AddPart(tmp, name, len + sizeof(name) + 2);
        fh = Open(tmp, MODE_NEWFILE);
    }
    if (!err && !fh)
        err = IoErr();
    while (size > 0) {  /* takes all the data even after an error */
        n = size > CHUNK ? CHUNK : size;
        if (!(alive = l_recv(l, buf, n, IO_SECONDS)))
            break;
        if (fh && !err && Write(fh, buf, n) != n)
            err = IoErr() ? IoErr() : ERROR_DISK_FULL;
        size -= n;
    }
    if (fh && !Close(fh) && !err)
        err = IoErr();
    if (fh && tmp) {
        if (alive && !err) {
            DeleteFile(path);  /* Rename doesn't replace */
            if (!Rename(tmp, path))
                err = IoErr();
        }
        if (!alive || err)
            DeleteFile(tmp);
    }
    FreeVec(tmp);
    FreeVec(path);
    FreeVec(buf);
    if (!alive)
        return FALSE;
    return err ? c_fault(l, err) : c_ok(l);
}

/* The zlib CRC-32. */
static BOOL c_checksum(struct link *l, const char *path)
{
    ULONG table[256], crc = 0xFFFFFFFF, c;
    UBYTE *buf = AllocVec(CHUNK, MEMF_ANY), head[5];
    BPTR fh = 0;
    LONG n, i, k, err = 0;

    for (i = 0; i < 256; i++) {
        for (c = i, k = 0; k < 8; k++)
            c = c & 1 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
        table[i] = c;
    }
    if (!buf)
        err = ERROR_NO_FREE_STORE;
    else if (!(fh = Open(path, MODE_OLDFILE)))
        err = IoErr();
    else {
        while ((n = Read(fh, buf, CHUNK)) > 0)
            for (i = 0; i < n; i++)
                crc = table[(crc ^ buf[i]) & 0xFF] ^ (crc >> 8);
        if (n < 0)
            err = IoErr();
        Close(fh);
    }
    FreeVec(buf);
    if (err)
        return c_fault(l, err);
    head[0] = 0;
    be32(head + 1, ~crc);
    return l_send(l, head, 5, IO_SECONDS);
}

/* DELETE, MAKEDIR and CHECKSUM: one name, then status. */
static BOOL c_path_cmd(struct link *l, UBYTE cmd, UWORD len)
{
    LONG err = 0;
    BPTR lock;
    BOOL alive, ok;
    char *path = c_name(l, len, &alive);

    if (!alive)
        return FALSE;
    if (!path)
        return c_fault(l, ERROR_NO_FREE_STORE);
    if (cmd == CMD_CHECKSUM) {
        ok = c_checksum(l, path);
        FreeVec(path);
        return ok;
    }
    if (cmd == CMD_DELETE) {
        if (!DeleteFile(path))
            err = IoErr();
    } else if ((lock = CreateDir(path)))
        UnLock(lock);
    else
        err = IoErr();
    FreeVec(path);
    return err ? c_fault(l, err) : c_ok(l);
}

static BOOL c_rename(struct link *l, UWORD old_len, UWORD new_len)
{
    LONG err = 0;
    BOOL alive, alive2 = FALSE;
    char *from = c_name(l, old_len, &alive), *to = alive ? c_name(l, new_len, &alive2) : NULL;

    if (alive && alive2) {
        if (!from || !to)
            err = ERROR_NO_FREE_STORE;
        else if (!Rename(from, to))
            err = IoErr();
    }
    FreeVec(from);
    FreeVec(to);
    if (!alive || !alive2)
        return FALSE;
    return err ? c_fault(l, err) : c_ok(l);
}

static void t_pair(struct text *t, const char *key, ULONG v)
{
    char d[12];

    dec(d, v);
    t_str(t, (const UBYTE *)key, FALSE);
    t_chr(t, '=');
    t_str(t, (const UBYTE *)d, FALSE);
    t_chr(t, '\n');
}

static void t_version(struct text *t, const char *key, UWORD v, UWORD r)
{
    t_str(t, (const UBYTE *)key, FALSE);
    t_chr(t, '=');
    t_num(t, v);
    t_chr(t, '.');
    t_num(t, r);
    t_chr(t, '\n');
}

/* MEMF_TOTAL came with V39; before, the memory list knows. */
static ULONG mem_total(ULONG kind)
{
    struct MemHeader *mh;
    ULONG n = 0;

    if (SysBase->LibNode.lib_Version >= 39)
        return AvailMem(kind | MEMF_TOTAL);
    Forbid();
    for (mh = (struct MemHeader *)SysBase->MemList.lh_Head; mh->mh_Node.ln_Succ;
         mh = (struct MemHeader *)mh->mh_Node.ln_Succ)
        if (mh->mh_Attributes & kind)
            n += (ULONG)mh->mh_Upper - (ULONG)mh->mh_Lower;
    Permit();
    return n;
}

static BOOL c_sysinfo(struct link *l)
{
    struct Library *SocketBase = l->base;
    struct text t = {0};

    t_pair(&t, "chip_free", AvailMem(MEMF_CHIP));
    t_pair(&t, "chip_total", mem_total(MEMF_CHIP));
    t_pair(&t, "chip_largest", AvailMem(MEMF_CHIP | MEMF_LARGEST));
    t_pair(&t, "fast_free", AvailMem(MEMF_FAST));
    t_pair(&t, "fast_total", mem_total(MEMF_FAST));
    t_pair(&t, "fast_largest", AvailMem(MEMF_FAST | MEMF_LARGEST));
    t_pair(&t, "total_free", AvailMem(MEMF_ANY));
    t_pair(&t, "screen_paused", pause_left());  /* seconds left, see PAUSE */
    t_version(&t, "exec_version", SysBase->LibNode.lib_Version, SysBase->LibNode.lib_Revision);
    t_version(&t, "kickstart", SysBase->LibNode.lib_Version, SysBase->SoftVer);
    t_version(&t, "bsdsocket", SocketBase->lib_Version, SocketBase->lib_Revision);
    t_version(&t, "graphics", GfxBase->LibNode.lib_Version, GfxBase->LibNode.lib_Revision);
    {
        UWORD f = SysBase->AttnFlags;
        t_pair(&t, "cpu", f & AFF_68060 ? 68060 : f & AFF_68040 ? 68040 : f & AFF_68030 ? 68030 :
                          f & AFF_68020 ? 68020 : f & AFF_68010 ? 68010 : 68000);
        t_pair(&t, "attn_flags", f);  /* raw, for bits newer than the NDK, such as a 68080's */
    }
    return c_text(l, &t);
}

/* Blocks times block size, which may pass 4 GB. */
static void t_bytes(struct text *t, ULONG blocks, ULONG size)
{
    unsigned long long v = (unsigned long long)blocks * size;
    char d[24];
    LONG n = 0;

    do
        d[n++] = '0' + (int)(v % 10);
    while (v /= 10);
    while (n)
        t_chr(t, d[--n]);
}

static BOOL c_volumes(struct link *l)
{
    struct text t = {0}, names = {0};
    struct DosList *dl;
    struct InfoData *info = AllocVec(sizeof(*info), MEMF_ANY);
    const UBYTE *name;
    BPTR lock;
    ULONG at;

    if (!info)
        return c_fault(l, ERROR_NO_FREE_STORE);
    /* Names first: asking a handler while holding the list could hang. */
    dl = LockDosList(LDF_VOLUMES | LDF_READ);
    while ((dl = NextDosEntry(dl, LDF_VOLUMES)))
        if (dl->dol_Task) {  /* mounted */
            name = BADDR(dl->dol_Name);
            t_add(&names, name + 1, name[0]);
            t_add(&names, ":", 2);
        }
    UnLockDosList(LDF_VOLUMES | LDF_READ);
    for (at = 0; !names.broke && at < names.len; at += strlen((char *)names.buf + at) + 1) {
        name = names.buf + at;
        if (!(lock = Lock((STRPTR)name, SHARED_LOCK)))
            continue;
        if (Info(lock, info)) {
            t_str(&t, name, FALSE);
            t_tab(&t);
            t_bytes(&t, info->id_NumBlocksUsed, info->id_BytesPerBlock);
            t_tab(&t);
            t_bytes(&t, info->id_NumBlocks - info->id_NumBlocksUsed, info->id_BytesPerBlock);
            t_tab(&t);
            t_bytes(&t, info->id_NumBlocks, info->id_BytesPerBlock);
            t_tab(&t);
            t_num(&t, info->id_BytesPerBlock);
            t_chr(&t, '\n');
        }
        UnLock(lock);
    }
    FreeVec(names.buf);
    FreeVec(info);
    return c_text(l, &t);
}

/* EXEC: a runner process calls SystemTags, so the channel can watch the clock
 * and break the command. Whoever finishes last frees the run. */
struct run {
    struct Task *chan;
    BYTE sig;
    volatile BOOL done, orphan;
    LONG rc, err;
    BPTR dir;
    char *line;
    char name[32], out[48];
};

/* Where commands are looked for when no Shell gave us a path, as the
 * Startup-Sequence sets it; C: comes last anyway. */
static const char *const std_path[] = {
    "SYS:Utilities", "SYS:Rexxc", "SYS:System", "S:", "SYS:Prefs", "SYS:WBStartup",
    "SYS:Tools", "SYS:Tools/Commodities", NULL
};

/* A copy of a command path for the command's Shell, which frees it. Without it
 * the command would only find C: programs. It is the path of the Shell that
 * started us; from Workbench, that of the Workbench process, or else std_path.
 * Nodes are a BPTR to the next one and a lock. */
static BPTR copy_path(void)
{
    struct CommandLineInterface *cli = BADDR(((struct Process *)server)->pr_CLI);
    struct Task *wb;
    BPTR first = 0, *link = &first, *from, *node, lock;
    BOOL copied;
    LONG i;

    Forbid();  /* the Workbench process could end meanwhile */
    if (!cli && (wb = FindTask("Workbench")) && wb->tc_Node.ln_Type == NT_PROCESS)
        cli = BADDR(((struct Process *)wb)->pr_CLI);
    for (from = cli ? BADDR(cli->cli_CommandDir) : NULL; from; from = BADDR(from[0])) {
        if (!(node = AllocVec(2 * sizeof(BPTR), MEMF_PUBLIC | MEMF_CLEAR)))
            break;
        node[1] = DupLock(from[1]);
        *link = MKBADDR(node);
        link = &node[0];
    }
    copied = first != 0;
    Permit();
    for (i = 0; !copied && std_path[i]; i++) {  /* also when that path was empty */
        if (!(lock = Lock(std_path[i], SHARED_LOCK)))
            continue;
        if (!(node = AllocVec(2 * sizeof(BPTR), MEMF_PUBLIC | MEMF_CLEAR))) {
            UnLock(lock);
            break;
        }
        node[1] = lock;
        *link = MKBADDR(node);
        link = &node[0];
    }
    return first;
}

static void __saveds runner_main(void)
{
    struct run *r;
    BPTR in, out, old = 0;
    BOOL orphan;

    Wait(SIGBREAKF_CTRL_F);
    r = FindTask(NULL)->tc_UserData;
    if (r->dir)
        old = CurrentDir(r->dir);  /* the shell starts in a copy of it */
    in = Open("NIL:", MODE_OLDFILE);
    out = Open(r->out, MODE_NEWFILE);
    r->rc = -1;
    if (in && out) {
        r->rc = SystemTags(r->line, SYS_Input, in, SYS_Output, out,
                           NP_Error, out, NP_CloseError, FALSE, NP_Name, (ULONG)r->name,
                           NP_Path, copy_path(),
                           NP_StackSize, 65536, NP_Priority, 0, NP_WindowPtr, -1, TAG_END);
        if (r->rc == -1)
            r->err = IoErr();
    } else
        r->err = IoErr();
    if (in) Close(in);
    if (out) Close(out);
    if (r->dir)
        CurrentDir(old);
    Forbid();
    orphan = r->orphan;
    r->done = TRUE;
    if (!orphan)
        Signal(r->chan, 1UL << r->sig);
    Permit();
    if (orphan) {  /* the channel has given up on it */
        DeleteFile(r->out);
        if (r->dir)
            UnLock(r->dir);
        FreeVec(r);
    }
    Forbid();
    children--;
    Signal(server, 1UL << chan_gone);
}

static void free_run(struct run *r)
{
    DeleteFile(r->out);
    if (r->dir)
        UnLock(r->dir);
    FreeVec(r);
}

/* status, i32 return code, u32 length, output */
static BOOL send_output(struct link *l, struct run *r)
{
    UBYTE head[9], *buf = AllocVec(CHUNK, MEMF_ANY);
    BPTR fh = Open(r->out, MODE_OLDFILE);
    LONG n, err = 0;
    ULONG size;
    BOOL alive;

    if (r->rc == -1 && r->err)
        err = r->err;
    else if (!fh)
        err = IoErr();
    else if (!buf)
        err = ERROR_NO_FREE_STORE;
    if (err)
        alive = c_fault(l, err);
    else {
        Seek(fh, 0, OFFSET_END);
        size = Seek(fh, 0, OFFSET_BEGINNING);
        head[0] = 0;
        be32(head + 1, r->rc);
        be32(head + 5, size);
        alive = l_send(l, head, 9, IO_SECONDS);
        while (alive && size > 0) {
            n = Read(fh, buf, size > CHUNK ? CHUNK : size);
            alive = n > 0 && l_send(l, buf, n, IO_SECONDS);
            size -= n;
        }
    }
    if (fh)
        Close(fh);
    FreeVec(buf);
    return alive;
}

static BOOL c_exec(struct link *l, UWORD len, UWORD dir_len, UWORD secs)
{
    struct Library *SocketBase = l->base;
    struct run *r = AllocVec(sizeof(*r) + len + dir_len + 2, MEMF_CLEAR);
    struct Process *proc;
    struct Task *shell;
    char *dir;
    ULONG start = now_ms(), n;
    LONG left;
    BOOL quitting = FALSE, alive;

    if (!r)
        return l_skip(l, len + dir_len, IO_SECONDS) && c_fault(l, ERROR_NO_FREE_STORE);
    r->line = (char *)(r + 1);
    dir = r->line + len + 1;
    if (!l_recv(l, r->line, len, IO_SECONDS) || !l_recv(l, dir, dir_len, IO_SECONDS)) {
        FreeVec(r);
        return FALSE;
    }
    r->line[len] = 0;
    dir[dir_len] = 0;
    if (dir_len && !(r->dir = Lock(dir, SHARED_LOCK))) {
        left = IoErr();
        FreeVec(r);
        return c_fault(l, left);
    }
    r->chan = FindTask(NULL);
    n = next_run();
    strcpy(r->name, "amigentd exec ");
    dec(r->name + strlen(r->name), n);
    strcpy(r->out, "T:amigentd-exec-");
    dec(r->out + strlen(r->out), n);
    if ((r->sig = AllocSignal(-1)) < 0) {
        free_run(r);
        return c_fault(l, ERROR_NO_FREE_STORE);
    }
    Forbid();
    children++;
    Permit();
    proc = CreateNewProcTags(NP_Entry, (ULONG)runner_main, NP_Name, (ULONG)"amigentd runner",
                             NP_StackSize, 8192, NP_Priority, 0, NP_WindowPtr, -1, TAG_END);
    if (!proc) {
        Forbid();
        children--;
        Permit();
        FreeSignal(r->sig);
        free_run(r);
        return c_fault(l, ERROR_NO_FREE_STORE);
    }
    proc->pr_Task.tc_UserData = r;
    Signal(&proc->pr_Task, SIGBREAKF_CTRL_F);

    while (!r->done) {
        ULONG sigs;

        if (secs && (left = secs * 1000L - (LONG)(now_ms() - start)) <= 0)
            break;
        sigs = wait_ms(secs ? left : -1, (1UL << r->sig) | SIGBREAKF_CTRL_C);
        if (sigs & SIGBREAKF_CTRL_C) {
            quitting = TRUE;
            break;
        }
    }
    Forbid();
    if (!r->done) {
        r->orphan = TRUE;  /* the runner frees it */
        if ((shell = FindTask(r->name)))
            Signal(shell, SIGBREAKF_CTRL_C);
    }
    Permit();
    FreeSignal(r->sig);
    if (r->orphan)
        return quitting ? FALSE : c_fail(l, 2, "timeout");
    alive = send_output(l, r);
    free_run(r);
    return alive;
}

static BOOL chan_command(struct link *l, const UBYTE *c)
{
    UWORD x = c[2] << 8 | c[3], y = c[4] << 8 | c[5];
    UWORD w = c[6] << 8 | c[7], h = c[8] << 8 | c[9];

    switch (c[0]) {
    case CMD_PING:
    case CMD_CHANNEL:
        return c_ok(l);
    case CMD_EXEC:
        return c_exec(l, x, y, w);
    case CMD_READ_FILE:
        return c_read(l, x);
    case CMD_WRITE_FILE:
        return c_write(l, c[1] & 1, x, (ULONG)w << 16 | h);
    case CMD_DIR:
    case CMD_STAT:
        return c_dir(l, x, c[0] == CMD_STAT);
    case CMD_DELETE:
    case CMD_MAKEDIR:
    case CMD_CHECKSUM:
        return c_path_cmd(l, c[0], x);
    case CMD_RENAME:
        return c_rename(l, x, y);
    case CMD_SYSINFO:
        return c_sysinfo(l);
    case CMD_PAUSE:
        set_pause(x);
        return c_ok(l);
    case CMD_VOLUMES:
        return c_volumes(l);
    case CMD_PUT:  /* these carry data; take it so the next request lines up */
        if (!l_skip(l, (ULONG)w << 16 | h, IO_SECONDS))
            return FALSE;
        /* fall through */
    case CMD_RUN:
    case CMD_AUTH:
        if (!l_skip(l, x, IO_SECONDS))
            return FALSE;
    }
    return c_fail(l, 1, "not on a command channel");
}

static void __saveds channel_main(void)
{
    struct channel *c;
    struct link l;
    UBYTE cmd[10];
    BOOL ok;

    Wait(SIGBREAKF_CTRL_F);
    c = FindTask(NULL)->tc_UserData;
    if ((l.base = OpenLibrary("bsdsocket.library", 4))) {
        struct Library *SocketBase = l.base;
        LONG one = 1, buffer = 32768;

        l.fd = ObtainSocket(c->id, AF_INET, SOCK_STREAM, 0);
        if (l.fd < 0)
            l.fd = ObtainSocket(c->id, AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (l.fd >= 0) {
            IoctlSocket(l.fd, FIONBIO, (char *)&one);
            setsockopt(l.fd, SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
            ok = c_ok(&l);
            while (ok && l_recv(&l, cmd, sizeof(cmd), 0)) {
                c->busy = TRUE;
                ok = chan_command(&l, cmd);
                c->last = now_ms();
                c->busy = FALSE;
            }
            CloseSocket(l.fd);
        }
        CloseLibrary(l.base);
    }
    Forbid();
    c->proc = NULL;
    children--;
    Signal(server, 1UL << chan_gone);
}

/* A free slot, or the one idle longest when all are taken. */
static struct channel *free_channel(void)
{
    struct channel *c, *old = NULL;
    LONG k;

    for (c = chans; c < chans + CHANNELS; c++) {
        if (!c->proc)
            return c;
        if (!c->busy && (!old || c->last < old->last))
            old = c;
    }
    if (!old)
        return NULL;
    Signal(&old->proc->pr_Task, SIGBREAKF_CTRL_C);
    for (k = 0; k < 100 && old->proc; k++)
        Delay(1);
    return old->proc ? NULL : old;
}

/* The channel process answers the CHANNEL request once it has the socket. */
static void start_channel(LONG i)
{
    struct newcomer *p = &pend[i];
    struct channel *c = free_channel();
    struct Process *proc;
    struct link l;

    if (c && (c->id = ReleaseSocket(p->fd, UNIQUE_ID)) != -1) {
        c->busy = FALSE;
        c->last = c->since = now_ms();
        c->addr = p->addr;
        Forbid();
        children++;
        Permit();
        proc = CreateNewProcTags(NP_Entry, (ULONG)channel_main, NP_Name, (ULONG)"amigentd channel",
                                 NP_Priority, 0, NP_StackSize, 16384, NP_WindowPtr, -1, TAG_END);
        if (proc) {
            c->proc = proc;
            proc->pr_Task.tc_UserData = c;
            Signal(&proc->pr_Task, SIGBREAKF_CTRL_F);
            p->fd = -1;
            return;
        }
        Forbid();
        children--;
        Permit();
        if ((p->fd = ObtainSocket(c->id, AF_INET, SOCK_STREAM, 0)) < 0) {  /* take it back */
            p->fd = -1;
            return;
        }
    }
    l.base = SocketBase;
    l.fd = p->fd;
    l_send(&l, "\1", 1, HELLO_SECONDS);
    close_pending(i);
}

static BOOL is_input(UBYTE cmd)
{
    return cmd == CMD_MOVE || cmd == CMD_BUTTON || cmd == CMD_RAWKEY || cmd == CMD_CHAR ||
           cmd == CMD_MOVE_REL;
}

/* Mouse and keys: the answer code, nothing sent. */
static UBYTE do_input(const UBYTE *c)
{
    UWORD x = c[2] << 8 | c[3], y = c[4] << 8 | c[5];

    input_ms = now_ms();
    switch (c[0]) {
    case CMD_MOVE:
        move_to(x, y);
        break;
    case CMD_BUTTON:
        set_buttons(c[1] & 7);
        break;
    case CMD_RAWKEY:
        key(x & 0x7F, c[1] & 1, y);
        break;
    case CMD_CHAR:
        return type_char(x, y) ? 0 : 1;
    case CMD_MOVE_REL:
        move_by((WORD)x, (WORD)y);
        break;
    }
    return 0;
}

/* Bytes from the screen client while a reply is going out. A whole input
 * command is done right away; anything else waits in in_cmd for its turn. */
static BOOL sneak_input(void)
{
    while (in_have < (LONG)sizeof(in_cmd) && nacks < (LONG)sizeof(acks)) {
        LONG n = recv(client, in_cmd + in_have, sizeof(in_cmd) - in_have, 0);

        if (n < 0 && Errno() == EWOULDBLOCK)
            return TRUE;
        if (n <= 0)
            return FALSE;
        in_have += n;
        if (in_have == sizeof(in_cmd) && authed && is_input(in_cmd[0])) {
            acks[nacks++] = do_input(in_cmd);
            in_have = 0;
        }
    }
    return TRUE;
}

/* The answers of input done while the last reply was on its way. */
static BOOL send_acks(void)
{
    LONG n = nacks;

    nacks = 0;
    return !n || (put(acks, n) && flush());
}

static BOOL command(const UBYTE *c)
{
    UWORD x = c[2] << 8 | c[3], y = c[4] << 8 | c[5];
    UWORD w = c[6] << 8 | c[7], h = c[8] << 8 | c[9];

    if (c[0] == CMD_AUTH)
        return auth(x);
    if (!authed)
        return refuse();
    if (is_input(c[0]))
        return reply(do_input(c));
    if ((c[0] == CMD_FRAME || c[0] == CMD_FRAME_RGB) && pause_left()) {
        x = y = 0x7FFF;  /* paused: off screen, so the answer is empty and nothing is read */
        w = h = 1;
    }
    switch (c[0]) {
    case CMD_INFO:
        return info();
    case CMD_PAUSE:
        set_pause(x);
        return reply(0);
    case CMD_FRAME:
        if (c[1] & 2)
            return frame_planar(c[1] & 1, (c[1] & 4) != 0, x, y, w, h);
        return frame(c[1] & 1, x, y, w, h);
    case CMD_PING:
        return reply(0);
    case CMD_PUT:
        return put_file(x, (ULONG)w << 16 | h);
    case CMD_RUN:
        return run_command(x);
    case CMD_SETUP_END:
        setup_mode = FALSE;
        return reply(0);
    case CMD_SCREENS:
        return screens();
    case CMD_SCREEN_FRONT:
        return screen_front((ULONG)x << 16 | y);
    case CMD_FRAME_RGB:
        return frame_rgb(c[1] & 1, x, y, w, h);
    case CMD_UITREE:
        return ui_tree();
    case CMD_MENUS:
        return menus();
    case CMD_CLIENT:
        client_id = (ULONG)x << 16 | y;
        return reply(client_id && owner == client_id ? 0 : 1);
    }
    return reply(1);
}

static void serve(void)
{
    LONG n, top, i;
    ULONG heard = 0;

    while (!quit) {
        fd_set r;
        struct timeval tv = {1, 0};
        ULONG sigs = SIGBREAKF_CTRL_C | rexx_signal();

        for (i = 0; i < PENDING && !(pend[i].fd >= 0 && pend[i].screen); i++)
            ;
        if (i < PENDING) {  /* the newcomer wins */
            drop_client();
            client = pend[i].fd;
            client_addr = pend[i].addr;
            client_since = now_ms();
            pend[i].fd = -1;
            authed = TRUE;
            client_id = 0;
            conn_serial++;  /* a new face: pref is not its picture yet */
            heard = now_ms();
            if (!command(pend[i].cmd) || !send_acks())
                drop_client();
            continue;
        }
        if (client >= 0 && now_ms() - heard > IDLE_SECONDS * 1000)
            drop_client();  /* idle */
        if (client >= 0 && in_have == sizeof(in_cmd)) {  /* came in during the last reply */
            in_have = 0;
            if (!command(in_cmd) || !send_acks())
                drop_client();
            continue;
        }
        FD_ZERO(&r);
        top = watch(&r, client);
        if (client >= 0)
            FD_SET(client, &r);
        n = WaitSelect(top + 1, &r, NULL, NULL, &tv, &sigs);
        if (sigs & SIGBREAKF_CTRL_C)
            return;
        if (sigs & rexx_signal())
            rexx_messages();
        if (n <= 0)
            continue;
        newcomers(&r);
        if (client < 0 || !FD_ISSET(client, &r))
            continue;
        n = recv(client, in_cmd + in_have, sizeof(in_cmd) - in_have, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0) {
            drop_client();
            continue;
        }
        heard = now_ms();
        in_have += n;
        if (in_have == sizeof(in_cmd)) {
            in_have = 0;
            if (!command(in_cmd) || !send_acks())
                drop_client();
        }
    }
}

/* ---- TLS --------------------------------------------------------------- */

/* With AmiSSL and a certificate in TLS_FILE, a process of its own listens on
 * PORT+1. Each TLS connection gets a pump process that talks TLS to the client
 * and plain protocol to our own port on 127.0.0.1, so key, channels and
 * takeover work exactly as without TLS. The few AmiSSL calls are declared here
 * from the AmiSSL 5.27 SDK (inline/amissl_protos.h, inline/amisslmaster_protos.h,
 * amissl/tags.h, libraries/amisslmaster.h), so building needs no SDK. */

typedef struct ssl_st SSL;
typedef struct ssl_ctx_st SSL_CTX;
typedef struct ssl_method_st SSL_METHOD;

struct Library *AmiSSLMasterBase, *AmiSSLBase;

LONG __OpenAmiSSLTags(__reg("a6") struct Library *, __reg("d0") LONG APIVersion, Tag tag, ...) =
    "\tmove.l\ta0,-(a7)\n\tlea\t4(a7),a0\n\tjsr\t-60(a6)\n\tmovea.l\t(a7)+,a0";
#define OpenAmiSSLTags(v, ...) __OpenAmiSSLTags(AmiSSLMasterBase, (v), __VA_ARGS__)
void __CloseAmiSSL(__reg("a6") struct Library *) = "\tjsr\t-42(a6)";
#define CloseAmiSSL() __CloseAmiSSL(AmiSSLMasterBase)
long __InitAmiSSL(__reg("a6") struct Library *, Tag tag, ...) =
    "\tmove.l\ta0,-(a7)\n\tlea\t4(a7),a0\n\tjsr\t-36(a6)\n\tmovea.l\t(a7)+,a0";
#define InitAmiSSL(...) __InitAmiSSL(AmiSSLBase, __VA_ARGS__)
long __CleanupAmiSSL(__reg("a6") struct Library *, Tag tag, ...) =
    "\tmove.l\ta0,-(a7)\n\tlea\t4(a7),a0\n\tjsr\t-42(a6)\n\tmovea.l\t(a7)+,a0";
#define CleanupAmiSSL(...) __CleanupAmiSSL(AmiSSLBase, __VA_ARGS__)
const SSL_METHOD *__TLS_server_method(__reg("a6") struct Library *) = "\tjsr\t-26946(a6)";
#define TLS_server_method() __TLS_server_method(AmiSSLBase)
SSL_CTX *__SSL_CTX_new(__reg("a6") struct Library *, __reg("a0") const SSL_METHOD *meth) = "\tjsr\t-8208(a6)";
#define SSL_CTX_new(m) __SSL_CTX_new(AmiSSLBase, (m))
void __SSL_CTX_free(__reg("a6") struct Library *, __reg("a0") SSL_CTX *a) = "\tjsr\t-8214(a6)";
#define SSL_CTX_free(a) __SSL_CTX_free(AmiSSLBase, (a))
int __SSL_CTX_use_certificate_chain_file(__reg("a6") struct Library *, __reg("a0") SSL_CTX *ctx,
                                         __reg("a1") const char *file) = "\tjsr\t-8508(a6)";
#define SSL_CTX_use_certificate_chain_file(c, f) __SSL_CTX_use_certificate_chain_file(AmiSSLBase, (c), (f))
int __SSL_CTX_use_PrivateKey_file(__reg("a6") struct Library *, __reg("a0") SSL_CTX *ctx,
                                  __reg("a1") const char *file, __reg("d0") int type) = "\tjsr\t-8496(a6)";
#define SSL_CTX_use_PrivateKey_file(c, f, t) __SSL_CTX_use_PrivateKey_file(AmiSSLBase, (c), (f), (t))
int __SSL_CTX_check_private_key(__reg("a6") struct Library *, __reg("a0") const SSL_CTX *ctx) = "\tjsr\t-8766(a6)";
#define SSL_CTX_check_private_key(c) __SSL_CTX_check_private_key(AmiSSLBase, (c))
SSL *__SSL_new(__reg("a6") struct Library *, __reg("a0") SSL_CTX *ctx) = "\tjsr\t-8784(a6)";
#define SSL_new(c) __SSL_new(AmiSSLBase, (c))
void __SSL_free(__reg("a6") struct Library *, __reg("a0") SSL *ssl) = "\tjsr\t-8820(a6)";
#define SSL_free(s) __SSL_free(AmiSSLBase, (s))
int __SSL_set_fd(__reg("a6") struct Library *, __reg("a0") SSL *s, __reg("d0") int fd) = "\tjsr\t-8358(a6)";
#define SSL_set_fd(s, fd) __SSL_set_fd(AmiSSLBase, (s), (fd))
int __SSL_accept(__reg("a6") struct Library *, __reg("a0") SSL *ssl) = "\tjsr\t-8826(a6)";
#define SSL_accept(s) __SSL_accept(AmiSSLBase, (s))
int __SSL_read(__reg("a6") struct Library *, __reg("a0") SSL *ssl, __reg("a1") void *buf,
               __reg("d0") int num) = "\tjsr\t-8838(a6)";
#define SSL_read(s, b, n) __SSL_read(AmiSSLBase, (s), (b), (n))
int __SSL_write(__reg("a6") struct Library *, __reg("a0") SSL *ssl, __reg("a1") const void *buf,
                __reg("d0") int num) = "\tjsr\t-8850(a6)";
#define SSL_write(s, b, n) __SSL_write(AmiSSLBase, (s), (b), (n))
int __SSL_shutdown(__reg("a6") struct Library *, __reg("a0") SSL *s) = "\tjsr\t-8994(a6)";
#define SSL_shutdown(s) __SSL_shutdown(AmiSSLBase, (s))
int __SSL_pending(__reg("a6") struct Library *, __reg("a0") const SSL *s) = "\tjsr\t-8352(a6)";
#define SSL_pending(s) __SSL_pending(AmiSSLBase, (s))

#define AMISSL_V303 21  /* AmiSSL 5.1, the first on OpenSSL 3 */
#define AMISSLMASTER_MIN_VERSION 5
#define AmiSSL_SocketBase (TAG_USER + 0x01)
#define AmiSSL_ErrNoPtr (TAG_USER + 0x0b)
#define AmiSSL_UsesOpenSSLStructs (TAG_USER + 0x0c)
#define AmiSSL_GetAmiSSLBase (TAG_USER + 0x0d)
#define SSL_FILETYPE_PEM 1

#define TLS_FILE "S:amigent-tls.pem"
#define TLS_CHECK_SECONDS 10
#define TLS_PUMPS 8
#define TLS_BUFFER 4096

static struct Task *volatile tls_task;
/* tls_ready, declared above: AmiSSL is open, hello flag 2 */
static UWORD plain_port;
static LONG tls_pumps;  /* under Forbid */
static struct Task *pump_task[TLS_PUMPS];  /* under Forbid, for stop_tls() */
static ULONG pump_addr[TLS_PUMPS], pump_since[TLS_PUMPS];  /* for CLIENTS */
static BYTE tls_gone = -1;  /* the listener's: a pump ended */

struct pump {
    LONG id;
    SSL *ssl;
    ULONG addr;
};

/* Everything in buf to the socket, blocking. */
static BOOL send_all(struct Library *SocketBase, LONG fd, const UBYTE *buf, LONG n)
{
    LONG k;

    while (n > 0) {
        if ((k = send(fd, (UBYTE *)buf, n, 0)) <= 0)
            return FALSE;
        buf += k;
        n -= k;
    }
    return TRUE;
}

static BOOL ssl_write_all(SSL *ssl, const UBYTE *buf, LONG n)
{
    LONG k;

    while (n > 0) {
        if ((k = SSL_write(ssl, buf, n)) <= 0)
            return FALSE;
        buf += k;
        n -= k;
    }
    return TRUE;
}

/* Client <-TLS-> pump <-plain-> our own port. Ends when either side does. */
static void pump(struct Library *SocketBase, LONG fd, SSL *ssl, UBYTE *buf)
{
    struct sockaddr_in addr;
    LONG inner, n, one = 1;

    if (SSL_accept(ssl) != 1)
        return;
    if ((inner = socket(AF_INET, SOCK_STREAM, 0)) < 0)
        return;
    memset(&addr, 0, sizeof(addr));
    addr.sin_len = sizeof(addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons(plain_port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(inner, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
        setsockopt(inner, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        for (;;) {
            fd_set r;
            ULONG sigs = SIGBREAKF_CTRL_C;

            FD_ZERO(&r);
            if (SSL_pending(ssl) <= 0) {  /* else data waits inside AmiSSL */
                FD_SET(fd, &r);
                FD_SET(inner, &r);
                if (WaitSelect((fd > inner ? fd : inner) + 1, &r, NULL, NULL, NULL, &sigs) < 0 ||
                    (sigs & SIGBREAKF_CTRL_C))
                    break;
            } else
                FD_SET(fd, &r);
            if (FD_ISSET(fd, &r)) {
                if ((n = SSL_read(ssl, buf, TLS_BUFFER)) <= 0 || !send_all(SocketBase, inner, buf, n))
                    break;
            }
            if (FD_ISSET(inner, &r)) {
                if ((n = recv(inner, buf, TLS_BUFFER, 0)) <= 0 || !ssl_write_all(ssl, buf, n))
                    break;
            }
        }
    }
    CloseSocket(inner);
    SSL_shutdown(ssl);
}

static void __saveds pump_main(void)
{
    struct pump *p;
    struct Library *SocketBase;
    LONG fd, err = 0, zero = 0, n;
    struct timeval limit = {IO_SECONDS, 0};
    UBYTE *buf = AllocVec(TLS_BUFFER, MEMF_ANY);

    Wait(SIGBREAKF_CTRL_F);
    p = FindTask(NULL)->tc_UserData;
    Forbid();
    for (n = 0; n < TLS_PUMPS && pump_task[n]; n++)
        ;
    if (n < TLS_PUMPS) {
        pump_task[n] = FindTask(NULL);
        pump_addr[n] = p->addr;
        pump_since[n] = now_ms();
    }
    Permit();
    if ((SocketBase = OpenLibrary("bsdsocket.library", 4))) {
        if (buf && InitAmiSSL(AmiSSL_SocketBase, SocketBase, AmiSSL_ErrNoPtr, &err, TAG_DONE) == 0) {
            if ((fd = ObtainSocket(p->id, AF_INET, SOCK_STREAM, 0)) < 0)
                fd = ObtainSocket(p->id, AF_INET, SOCK_STREAM, IPPROTO_TCP);
            if (fd >= 0) {
                /* Blocking, but a record that stops halfway ends the link. */
                IoctlSocket(fd, FIONBIO, (char *)&zero);
                setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit));
                setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit));
                SSL_set_fd(p->ssl, fd);
                pump(SocketBase, fd, p->ssl, buf);
                CloseSocket(fd);
            }
            SSL_free(p->ssl);
            CleanupAmiSSL(TAG_DONE);
        }
        CloseLibrary(SocketBase);
    }
    FreeVec(buf);
    FreeVec(p);
    Forbid();
    for (n = 0; n < TLS_PUMPS; n++)
        if (pump_task[n] == FindTask(NULL))
            pump_task[n] = NULL;
    tls_pumps--;
    if (tls_task)
        Signal(tls_task, 1UL << tls_gone);
    children--;
    Signal(server, 1UL << chan_gone);
}

/* Certificate and key from TLS_FILE, or NULL. */
static SSL_CTX *load_tls(void)
{
    SSL_CTX *ctx = SSL_CTX_new(TLS_server_method());

    if (ctx && SSL_CTX_use_certificate_chain_file(ctx, TLS_FILE) == 1 &&
        SSL_CTX_use_PrivateKey_file(ctx, TLS_FILE, SSL_FILETYPE_PEM) == 1 &&
        SSL_CTX_check_private_key(ctx) == 1)
        return ctx;
    if (ctx)
        SSL_CTX_free(ctx);
    return NULL;
}

/* Size and date of TLS_FILE, all 0 when it isn't there. */
static void tls_stamp(LONG *stamp)
{
    struct FileInfoBlock *fib = AllocDosObject(DOS_FIB, NULL);
    BPTR lock = Lock(TLS_FILE, SHARED_LOCK);

    stamp[0] = stamp[1] = stamp[2] = stamp[3] = 0;
    if (lock && fib && Examine(lock, fib)) {
        stamp[0] = fib->fib_Size + 1;
        stamp[1] = fib->fib_Date.ds_Days;
        stamp[2] = fib->fib_Date.ds_Minute;
        stamp[3] = fib->fib_Date.ds_Tick;
    }
    if (lock)
        UnLock(lock);
    FreeDosObject(DOS_FIB, fib);
}

static LONG tls_listen(struct Library *SocketBase)
{
    struct sockaddr_in addr;
    LONG fd = socket(AF_INET, SOCK_STREAM, 0), one = 1;

    if (fd < 0)
        return -1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    memset(&addr, 0, sizeof(addr));
    addr.sin_len = sizeof(addr);
    addr.sin_family = AF_INET;
    addr.sin_port = htons(plain_port + 1);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) == 0 && listen(fd, 4) == 0)
        return fd;
    CloseSocket(fd);
    return -1;
}

static void tls_accept(struct Library *SocketBase, LONG listen_fd, SSL_CTX *ctx)
{
    struct sockaddr_in addr;
    LONG len = sizeof(addr), fd, one = 1, full;
    struct pump *p = NULL;
    struct Process *proc;

    if ((fd = accept(listen_fd, (struct sockaddr *)&addr, &len)) < 0)
        return;
    Forbid();
    full = tls_pumps >= TLS_PUMPS;
    Permit();
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    if (!full && (p = AllocVec(sizeof(*p), MEMF_CLEAR)) && (p->ssl = SSL_new(ctx)) &&
        (p->id = ReleaseSocket(fd, UNIQUE_ID)) != -1) {
        p->addr = addr.sin_addr.s_addr;
        Forbid();
        tls_pumps++;
        children++;
        Permit();
        proc = CreateNewProcTags(NP_Entry, (ULONG)pump_main, NP_Name, (ULONG)"amigentd tls",
                                 NP_Priority, 0, NP_StackSize, 65536, TAG_END);
        if (proc) {
            proc->pr_Task.tc_UserData = p;
            Signal(&proc->pr_Task, SIGBREAKF_CTRL_F);
            return;
        }
        Forbid();
        tls_pumps--;
        children--;
        Permit();
        fd = ObtainSocket(p->id, AF_INET, SOCK_STREAM, 0);
    }
    if (p && p->ssl)
        SSL_free(p->ssl);
    FreeVec(p);
    if (fd >= 0)
        CloseSocket(fd);
}

/* Opens AmiSSL; listens while TLS_FILE is there, and reloads it when it changes. */
static void __saveds tls_main(void)
{
    struct Library *SocketBase;
    SSL_CTX *ctx = NULL;
    LONG listen_fd = -1, err = 0, seen[4] = {0, 0, 0, 0}, now[4], n;
    ULONG sigs;

    Wait(SIGBREAKF_CTRL_F);
    if ((tls_gone = AllocSignal(-1)) >= 0 && (SocketBase = OpenLibrary("bsdsocket.library", 4))) {
        if ((AmiSSLMasterBase = OpenLibrary("amisslmaster.library", AMISSLMASTER_MIN_VERSION))) {
            if (OpenAmiSSLTags(AMISSL_V303, AmiSSL_UsesOpenSSLStructs, FALSE,
                               AmiSSL_GetAmiSSLBase, (ULONG)&AmiSSLBase,
                               AmiSSL_SocketBase, (ULONG)SocketBase,
                               AmiSSL_ErrNoPtr, (ULONG)&err, TAG_DONE) == 0) {
                tls_ready = TRUE;
                for (;;) {
                    struct timeval tv = {TLS_CHECK_SECONDS, 0};
                    fd_set r;

                    tls_stamp(now);
                    /* A file that failed to load is tried again: it may have been
                     * caught half written, with the stamp it ends up with. */
                    if (memcmp(now, seen, sizeof(now)) || (now[0] && !ctx)) {
                        memcpy(seen, now, sizeof(now));
                        if (ctx)
                            SSL_CTX_free(ctx);  /* pumps hold their own reference */
                        ctx = now[0] ? load_tls() : NULL;
                    }
                    if (ctx && listen_fd < 0)
                        listen_fd = tls_listen(SocketBase);
                    if (!ctx && listen_fd >= 0) {
                        CloseSocket(listen_fd);
                        listen_fd = -1;
                    }
                    tls_open = listen_fd >= 0;
                    sigs = SIGBREAKF_CTRL_C;
                    if (listen_fd >= 0) {
                        FD_ZERO(&r);
                        FD_SET(listen_fd, &r);
                        n = WaitSelect(listen_fd + 1, &r, NULL, NULL, &tv, &sigs);
                    } else {
                        sigs = wait_ms(TLS_CHECK_SECONDS * 1000L, sigs);
                        n = 0;
                    }
                    if (sigs & SIGBREAKF_CTRL_C)
                        break;
                    if (n > 0 && listen_fd >= 0 && FD_ISSET(listen_fd, &r))
                        tls_accept(SocketBase, listen_fd, ctx);
                }
                tls_open = FALSE;
                if (listen_fd >= 0)
                    CloseSocket(listen_fd);
                for (;;) {  /* the pumps run in AmiSSL: it stays open until they end */
                    Forbid();
                    n = tls_pumps;
                    Permit();
                    if (!n)
                        break;
                    Wait(1UL << tls_gone);
                }
                if (ctx)
                    SSL_CTX_free(ctx);
                tls_ready = FALSE;
                CloseAmiSSL();
            }
            CloseLibrary(AmiSSLMasterBase);
        }
        CloseLibrary(SocketBase);
    }
    Forbid();
    tls_task = NULL;
    children--;
    Signal(server, 1UL << chan_gone);
}

/* Ctrl-C to the listener and every pump. */
static void stop_tls(void)
{
    LONG i;

    Forbid();
    if (tls_task)
        Signal(tls_task, SIGBREAKF_CTRL_C);
    for (i = 0; i < TLS_PUMPS; i++)
        if (pump_task[i])
            Signal(pump_task[i], SIGBREAKF_CTRL_C);
    Permit();
}

/* ---- start ------------------------------------------------------------ */

/* ---- ARexx --------------------------------------------------------------- */

/* Port AMIGENT, or AMIGENT.<port> when a first copy has it. The main process
 * answers between two looks at the network, so a reply takes a few ms; while a
 * frame goes out, between two tiles. See "arexx" in docs/PROTOCOL.md. */
struct RxsLib *RexxSysBase;
static struct MsgPort *rexx_port;
static char rexx_name[20];

static void rexx_open(UWORD port)
{
    if (!(RexxSysBase = (struct RxsLib *)OpenLibrary("rexxsyslib.library", 36)) ||
        !(rexx_port = CreateMsgPort()))
        return;  /* no ARexx, no port: the rest runs regardless */
    strcpy(rexx_name, "AMIGENT");
    Forbid();
    if (FindPort(rexx_name)) {  /* a sibling on another port got here first */
        char *d = rexx_name + 7;
        UWORD v = port, div = 10000;
        *d++ = '.';
        while (div > 1 && v / div == 0)
            div /= 10;
        for (; div; div /= 10)
            *d++ = '0' + v / div % 10;
        *d = 0;
    }
    rexx_port->mp_Node.ln_Name = rexx_name;
    rexx_port->mp_Node.ln_Pri = 0;
    AddPort(rexx_port);
    Permit();
}

static void rexx_close(void)
{
    struct RexxMsg *m;

    if (rexx_port) {
        Forbid();
        RemPort(rexx_port);
        Permit();
        while ((m = (struct RexxMsg *)GetMsg(rexx_port))) {  /* nobody waits forever */
            m->rm_Result1 = RC_FATAL;
            m->rm_Result2 = 0;
            ReplyMsg((struct Message *)m);
        }
        DeleteMsgPort(rexx_port);
        rexx_port = NULL;
    }
    if (RexxSysBase)
        CloseLibrary((struct Library *)RexxSysBase);
    RexxSysBase = NULL;
}

static ULONG rexx_signal(void)
{
    return rexx_port ? 1UL << rexx_port->mp_SigBit : 0;
}

static void t_ip(struct text *t, ULONG a)
{
    t_num(t, a >> 24 & 255), t_chr(t, '.');
    t_num(t, a >> 16 & 255), t_chr(t, '.');
    t_num(t, a >> 8 & 255), t_chr(t, '.');
    t_num(t, a & 255);
}

static void t_line(struct text *t, const char *key, LONG v)
{
    t_str(t, (const UBYTE *)key, FALSE);
    t_chr(t, '=');
    t_num(t, v);
    t_chr(t, '\n');
}

static void t_flag(struct text *t, const char *key, BOOL on)
{
    t_str(t, (const UBYTE *)key, FALSE);
    t_str(t, (const UBYTE *)(on ? "=on" : "=off"), FALSE);
    t_chr(t, '\n');
}

/* kind, address, seconds connected; a tab between, a line each. */
static void t_client(struct text *t, const char *kind, ULONG addr, ULONG since)
{
    t_str(t, (const UBYTE *)kind, FALSE);
    t_chr(t, '\t');
    t_ip(t, addr);
    t_chr(t, '\t');
    t_num(t, (now_ms() - since) / 1000);
    t_chr(t, '\n');
}

static LONG count_channels(void)
{
    LONG i, n = 0;

    for (i = 0; i < CHANNELS; i++)
        if (chans[i].proc)
            n++;
    return n;
}

static void rexx_status(struct text *t)
{
    const UBYTE *s;
    LONG i, n = 0;

    t_str(t, (const UBYTE *)"version=", FALSE);
    for (s = (const UBYTE *)version + 15; *s && *s != ' '; s++)  /* past "$VER: amigentd " */
        t_chr(t, *s);
    t_chr(t, '\n');
    t_line(t, "protocol", PROTOCOL_VERSION);
    t_line(t, "port", plain_port);
    t_line(t, "uptime", (now_ms() - start_ms) / 1000);
    t_flag(t, "tls", tls_open);
    t_flag(t, "tls_only", tls_only);
    t_flag(t, "setup", setup_mode);
    t_flag(t, "key", secret_len > 0);
    t_line(t, "screen", client >= 0);
    t_line(t, "channels", count_channels());
    Forbid();
    t_line(t, "tls_clients", tls_pumps);
    Permit();
    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0)
            n++;
    t_line(t, "pending", n);
    t_line(t, "paused", pause_left());
}

static void rexx_clients(struct text *t)
{
    LONG i;

    if (client >= 0)
        t_client(t, "screen", client_addr, client_since);
    for (i = 0; i < CHANNELS; i++)
        if (chans[i].proc)
            t_client(t, "channel", chans[i].addr, chans[i].since);
    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0)
            t_client(t, "pending", pend[i].addr, pend[i].since);
    Forbid();
    for (i = 0; i < TLS_PUMPS; i++)
        if (pump_task[i])
            t_client(t, "tls", pump_addr[i], pump_since[i]);
    Permit();
}

/* Same word, ignoring case; the rest of the line may only be blanks or args. */
static BOOL is_word(const UBYTE *s, const char *w)
{
    for (; *w; s++, w++)
        if ((*s & 0xDF) != *w)
            return FALSE;
    return *s == 0 || *s == ' ' || *s == '\t';
}

/* PAUSE [seconds]: without a number it tells the seconds left; the result is
 * always the seconds left afterwards. */
static LONG rexx_pause(const UBYTE *s, struct text *t)
{
    ULONG v = 0;
    BOOL digits = FALSE;

    while (*s == ' ' || *s == '\t')
        s++;
    while (*s >= '0' && *s <= '9') {
        if (v <= PAUSE_MAX)
            v = v * 10 + (*s - '0');
        digits = TRUE;
        s++;
    }
    while (*s == ' ' || *s == '\t')
        s++;
    if (*s)
        return RC_ERROR;
    if (digits)
        set_pause(v);
    t_num(t, pause_left());
    return 0;
}

/* An argument as AllocVec'd text: blanks around it and one pair of quotes off,
 * so 'TYPE "hello"' and 'TYPE hello' type the same. */
static UBYTE *rexx_arg(const UBYTE *s)
{
    LONG n;
    UBYTE *a;

    while (*s == ' ' || *s == '\t')
        s++;
    n = strlen((const char *)s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t'))
        n--;
    if (n >= 2 && (s[0] == '"' || s[0] == '\'') && s[n - 1] == s[0]) {
        s++;
        n -= 2;
    }
    if ((a = AllocVec(n + 1, MEMF_ANY)) != NULL) {
        memcpy(a, s, n);
        a[n] = 0;
    }
    return a;
}

static const struct { const char *name; UBYTE code; } key_names[] = {
    {"space", 0x40}, {"backspace", 0x41}, {"tab", 0x42}, {"enter", 0x43}, {"return", 0x44},
    {"esc", 0x45}, {"escape", 0x45}, {"del", 0x46}, {"delete", 0x46}, {"up", 0x4C},
    {"down", 0x4D}, {"right", 0x4E}, {"left", 0x4F}, {"help", 0x5F},
    {"f1", 0x50}, {"f2", 0x51}, {"f3", 0x52}, {"f4", 0x53}, {"f5", 0x54},
    {"f6", 0x55}, {"f7", 0x56}, {"f8", 0x57}, {"f9", 0x58}, {"f10", 0x59}
};
static const struct { const char *name; UWORD bits; } qual_names[] = {
    {"shift", IEQUALIFIER_LSHIFT}, {"ctrl", IEQUALIFIER_CONTROL}, {"alt", IEQUALIFIER_LALT},
    {"lamiga", IEQUALIFIER_LCOMMAND}, {"ramiga", IEQUALIFIER_RCOMMAND}
};
#define COUNT(a) ((LONG)(sizeof(a) / sizeof((a)[0])))

/* Does s[0..n) spell name, whatever the case? */
static BOOL same_name(const UBYTE *s, LONG n, const char *name)
{
    LONG i;

    for (i = 0; i < n; i++)
        if (!name[i] || (s[i] | 0x20) != name[i])
            return FALSE;
    return !name[n];
}

/* One key as words, "ctrl c", "lamiga+q", "shift tab", "return": qualifiers
 * first, the key last, a key name or one character. Pressed and let go. */
static BOOL press_named(const UBYTE *s, LONG len)
{
    UWORD qual = 0;
    LONG i, n;

    for (;;) {
        while (len && (*s == ' ' || *s == '\t' || *s == '+' && len > 1))
            s++, len--;
        for (n = 0; n < len && s[n] != ' ' && s[n] != '\t' && (s[n] != '+' || n == 0); n++)
            ;
        if (!n)
            return FALSE;
        if (n == len)
            break;  /* the last word is the key */
        for (i = 0; i < COUNT(qual_names) && !same_name(s, n, qual_names[i].name); i++)
            ;
        if (i == COUNT(qual_names))
            return FALSE;
        qual |= qual_names[i].bits;
        s += n;
        len -= n;
    }
    for (i = 0; i < COUNT(key_names); i++)
        if (same_name(s, n, key_names[i].name)) {
            key(key_names[i].code, TRUE, qual);
            key(key_names[i].code, FALSE, qual);
            return TRUE;
        }
    return n == 1 && type_char(*s, qual);
}

/* TYPE <text>: typed through the Amiga's own keymap, so umlauts come out
 * right. Keys go in angle brackets, "<return>", "<ctrl c>", "<lamiga q>";
 * "<<" is a "<". rc 5 at the first character or key name that is not known;
 * what came before it is typed. */
static LONG rexx_type(const UBYTE *s)
{
    UBYTE *text = rexx_arg(s), *p, *end;
    LONG rc = 0;

    if (!text)
        return RC_ERROR;
    for (p = text; *p && !rc; p++) {
        if (*p == '<' && p[1] == '<') {
            if (!type_char('<', 0))
                rc = RC_WARN;
            p++;
        } else if (*p == '<' && (end = (UBYTE *)strchr((char *)p + 1, '>')) != NULL) {
            if (!press_named(p + 1, end - p - 1))
                rc = RC_WARN;
            p = end;
        } else if (!type_char(*p, 0))
            rc = RC_WARN;
    }
    FreeVec(text);
    return rc;
}

/* KEY <name>: one key, written as inside the brackets of TYPE. */
static LONG rexx_key(const UBYTE *s)
{
    UBYTE *name = rexx_arg(s);
    LONG rc;

    if (!name)
        return RC_ERROR;
    rc = *name && press_named(name, strlen((char *)name)) ? 0 : RC_WARN;
    FreeVec(name);
    return rc;
}

/* ACTIVATEWINDOW <pattern>: the first window on any screen whose title
 * matches the AmigaDOS pattern ("*" works as "#?"); plain text matches a part
 * of the title. Its screen and the window come to the front and it gets the
 * keys. The result is the title; rc 5 when no window matched. */
static LONG rexx_activate(const UBYTE *s, struct text *t)
{
    UBYTE *arg = rexx_arg(s), *pat = NULL, *src = NULL, *q;
    const UBYTE *a;
    struct Screen *scr;
    struct Window *win, *found = NULL;
    ULONG lock;
    LONG n, rc = RC_WARN;
    BOOL wild = FALSE;

    if (!arg)
        return RC_ERROR;
    n = strlen((char *)arg);
    for (a = arg; *a; a++)
        if (strchr("#?*()|~[]%'", *a))
            wild = TRUE;
    if (n && (src = AllocVec(2 * n + 5, MEMF_ANY)) && (pat = AllocVec(4 * n + 12, MEMF_ANY))) {
        q = src;
        if (!wild)
            *q++ = '#', *q++ = '?';
        for (a = arg; *a; a++)
            if (*a == '*')
                *q++ = '#', *q++ = '?';
            else
                *q++ = *a;
        if (!wild)
            *q++ = '#', *q++ = '?';
        *q = 0;
        if (ParsePatternNoCase(src, pat, 4 * n + 12) >= 0) {
            lock = LockIBase(0);
            for (scr = IntuitionBase->FirstScreen; scr && !found; scr = scr->NextScreen)
                for (win = scr->FirstWindow; win && !found; win = win->NextWindow)
                    if (win->Title && MatchPatternNoCase(pat, win->Title)) {
                        found = win;
                        t_str(t, win->Title, FALSE);
                    }
            UnlockIBase(lock);
        } else
            rc = RC_ERROR;
    }
    if (found) {  /* Intuition must not be called under LockIBase */
        ScreenToFront(found->WScreen);
        WindowToFront(found);
        ActivateWindow(found);
        rc = 0;
    }
    FreeVec(pat);
    FreeVec(src);
    FreeVec(arg);
    return rc;
}

/* All messages waiting on the port: answer each, a result string if asked. */
static void rexx_messages(void)
{
    struct RexxMsg *m;

    while (rexx_port && (m = (struct RexxMsg *)GetMsg(rexx_port))) {
        struct text t = {NULL, 0, 0, FALSE};
        const UBYTE *s = (const UBYTE *)m->rm_Args[0];
        LONG rc = 0;

        while (s && (*s == ' ' || *s == '\t'))
            s++;
        if ((m->rm_Action & RXCODEMASK) != RXCOMM || !s)
            rc = RC_ERROR;
        else if (is_word(s, "STATUS"))
            rexx_status(&t);
        else if (is_word(s, "CLIENTS"))
            rexx_clients(&t);
        else if (is_word(s, "VERSION"))
            t_str(&t, (const UBYTE *)version + 6, FALSE);  /* past "$VER: " */
        else if (is_word(s, "PAUSE"))
            rc = rexx_pause(s + 5, &t);
        else if (is_word(s, "RESUME")) {
            set_pause(0);
            t_num(&t, 0);
        } else if (is_word(s, "ACTIVATEWINDOW"))
            rc = rexx_activate(s + 14, &t);
        else if (is_word(s, "TYPE"))
            rc = rexx_type(s + 4);
        else if (is_word(s, "KEY"))
            rc = rexx_key(s + 3);
        else if (is_word(s, "HELP"))
            t_str(&t, (const UBYTE *)"STATUS CLIENTS VERSION PAUSE RESUME ACTIVATEWINDOW TYPE KEY HELP QUIT", FALSE);
        else if (is_word(s, "QUIT")) {
            t_str(&t, (const UBYTE *)"bye", FALSE);
            quit = TRUE;  /* the main loop sees it on its next turn */
        } else
            rc = RC_ERROR;
        m->rm_Result1 = rc;
        m->rm_Result2 = 0;
        if (!rc && (m->rm_Action & RXFF_RESULT)) {
            if (t.broke)
                m->rm_Result1 = RC_ERROR;
            else  /* the text minus a trailing newline, as ARexx likes it */
                m->rm_Result2 = (LONG)CreateArgstring((STRPTR)(t.buf ? t.buf : (UBYTE *)""),
                                                      t.len && t.buf[t.len - 1] == '\n' ? t.len - 1 : t.len);
        }
        FreeVec(t.buf);
        ReplyMsg((struct Message *)m);
    }
}


static BOOL broken(void)
{
    return (SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) != 0;
}

/* At boot the network may come up after us, so keep asking. */
static struct Library *wait_for(const char *name, ULONG version)
{
    struct Library *lib;

    while (!(lib = OpenLibrary(name, version))) {
        if (broken())
            return NULL;
        Delay(100);
    }
    return lib;
}

static BOOL listen_on(UWORD port)
{
    struct sockaddr_in addr;
    LONG one = 1, tries;
    BOOL running;

    for (tries = 0; tries < 30 && !broken(); tries++) {
        listener = socket(AF_INET, SOCK_STREAM, 0);
        if (listener >= 0) {
            setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
            memset(&addr, 0, sizeof(addr));
            addr.sin_len = sizeof(addr);
            addr.sin_family = AF_INET;
            addr.sin_port = htons(port);
            addr.sin_addr.s_addr = htonl(INADDR_ANY);
            if (bind(listener, (struct sockaddr *)&addr, sizeof(addr)) == 0 &&
                listen(listener, 2) == 0)
                return TRUE;
            CloseSocket(listener);
            listener = -1;
        }
        Forbid();
        running = FindPort("AMIGENT") != NULL;  /* our own port opens later */
        Permit();
        if (running)
            break;  /* another amigentd has the port: no point in waiting for it */
        Delay(100);
    }
    return FALSE;
}

/* To the Shell, or in a requester when started from Workbench. fmt takes one
 * %ld or %s, as RawDoFmt. */
static void complain(const char *fmt, LONG arg)
{
    struct EasyStruct es = {sizeof(struct EasyStruct), 0, (UBYTE *)"amigentd", NULL, (UBYTE *)"OK"};
    struct Library *ib = (struct Library *)IntuitionBase;

    if (quiet)
        return;
    if (!from_wb) {
        PutStr("amigentd: ");
        VPrintf((STRPTR)fmt, &arg);
        PutStr("\n");
        return;
    }
    if (!ib && !(IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 36)))
        return;
    es.es_TextFormat = (UBYTE *)fmt;
    EasyRequestArgs(NULL, &es, NULL, &arg);
    if (!ib) {
        CloseLibrary((struct Library *)IntuitionBase);
        IntuitionBase = NULL;
    }
}

static void set_key_file(const char *name)
{
    strncpy(secret_file, name, sizeof(secret_file) - 1);
    secret_file[sizeof(secret_file) - 1] = 0;
}

/* The tool types of our icon, then those of any project icons we were started
 * with, later ones winning. */
static void tool_types(struct WBStartup *wb, UWORD *port)
{
    struct WBArg *arg = wb->sm_ArgList;
    struct DiskObject *icon;
    STRPTR v, *tt;
    LONG i, n;
    BPTR old;

    if (!(IconBase = OpenLibrary("icon.library", 36)))
        return;
    for (i = 0; i < wb->sm_NumArgs; i++, arg++) {
        old = CurrentDir(arg->wa_Lock);
        if ((icon = GetDiskObject(arg->wa_Name))) {
            tt = (STRPTR *)icon->do_ToolTypes;
            if ((v = FindToolType(tt, "PORT")) && StrToLong(v, &n) > 0)
                *port = n;
            if (FindToolType(tt, "SETUP"))
                setup_mode = TRUE;
            if (FindToolType(tt, "QUIET"))
                quiet = TRUE;
            if (FindToolType(tt, "TLSONLY"))
                tls_only = TRUE;
            if ((v = FindToolType(tt, "KEY")))
                set_key_file((const char *)v);
            FreeDiskObject(icon);
        }
        CurrentDir(old);
    }
    CloseLibrary(IconBase);
    IconBase = NULL;
}

int main(int argc, char **argv)
{
    LONG args[5] = {0, 0, 0, 0, 0};
    struct RDArgs *rda;
    UWORD port = DEFAULT_PORT;
    BOOL twin;
    int rc = RETURN_FAIL;
    LONG i, n;

    if (argc == 0) {  /* Workbench: argv is the WBStartup message */
        from_wb = TRUE;
        tool_types((struct WBStartup *)argv, &port);
    } else {
        rda = ReadArgs("PORT/K/N,SETUP/S,KEY/K,QUIET/S,TLSONLY/S", args, NULL);
        if (!rda) {
            PrintFault(IoErr(), "amigentd");
            return RETURN_FAIL;
        }
        if (args[0])
            port = *(LONG *)args[0];
        setup_mode = args[1] != 0;
        if (args[2])
            set_key_file((const char *)args[2]);
        quiet = args[3] != 0;
        tls_only = args[4] != 0;
        FreeArgs(rda);
    }

    server = FindTask(NULL);
    SetTaskPri(server, 2);
    /* No "insert volume" requesters: nobody may be at the Amiga to answer one,
     * and it would hold up the command, a file transfer or the whole daemon.
     * A missing volume is an error instead. Our processes and every command
     * they run inherit it; the Shell gets its own setting back at the end. */
    old_window = ((struct Process *)server)->pr_WindowPtr;
    ((struct Process *)server)->pr_WindowPtr = (APTR)-1;
    for (i = 0; i < PENDING; i++)
        pend[i].fd = -1;
    InitSemaphore(&picture);
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 37);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 37);
    KeymapBase = OpenLibrary("keymap.library", 37);
    if (!IntuitionBase || !GfxBase || !KeymapBase) {
        complain("needs OS 2.04 or newer", 0);
        goto out;
    }
    os39 = GfxBase->LibNode.lib_Version >= 39;
    CyberGfxBase = OpenLibrary("cybergraphics.library", 40);  /* only with RTG */
    if (!(SocketBase = wait_for("bsdsocket.library", 4)))
        goto out;
    timer_port = CreateMsgPort();
    timer_io = (struct timerequest *)CreateIORequest(timer_port, sizeof(*timer_io));
    if (!timer_io || OpenDevice("timer.device", UNIT_MICROHZ, (struct IORequest *)timer_io, 0)) {
        if (timer_io)
            DeleteIORequest((struct IORequest *)timer_io);
        timer_io = NULL;
        complain("cannot open %s", (LONG)"timer.device");
        goto out;
    }
    TimerBase = timer_io->tr_node.io_Device;
    start_ms = now_ms();
    input_port = CreateMsgPort();
    if (!input_port) {
        complain("out of memory", 0);
        goto out;
    }
    input_io = (struct IOStdReq *)CreateIORequest(input_port, sizeof(*input_io));
    if (!input_io || OpenDevice("input.device", 0, (struct IORequest *)input_io, 0)) {
        if (input_io)
            DeleteIORequest((struct IORequest *)input_io);
        input_io = NULL;
        complain("cannot open %s", (LONG)"input.device");
        goto out;
    }
    if ((chan_gone = AllocSignal(-1)) < 0 || (reader_gone = AllocSignal(-1)) < 0) {
        complain("out of signals", 0);
        goto out;
    }
    reader = (struct Task *)CreateNewProcTags(NP_Entry, (ULONG)reader_main,
                                              NP_Name, (ULONG)"amigentd reader",
                                              NP_Priority, -1, NP_StackSize, 16384, TAG_END);
    if (!reader) {
        complain("out of memory", 0);
        goto out;
    }
    Forbid();
    twin = port == DEFAULT_PORT && FindPort("AMIGENT");  /* siblings take a PORT of their own */
    Permit();
    if (twin || !listen_on(port)) {
        complain("cannot listen on port %ld.\nIf amigentd runs already, double-click\n"
                 "Amigent-Update to replace it.", port);
        goto out;
    }
    plain_port = port;
    rexx_open(port);
    Forbid();
    children++;
    Permit();
    if ((tls_task = (struct Task *)CreateNewProcTags(NP_Entry, (ULONG)tls_main,
                                                     NP_Name, (ULONG)"amigentd tls listener",
                                                     NP_Priority, 0, NP_StackSize, 32768, TAG_END)))
        Signal(tls_task, SIGBREAKF_CTRL_F);
    else {
        Forbid();
        children--;
        Permit();
    }
    if (setup_mode && !from_wb && !quiet)
        PutStr("amigentd: SETUP mode, waiting for amigent-setup\n");
    serve();
    rc = RETURN_OK;

out:
    if (client >= 0 || input_io)
        drop_client();
    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0)
            close_pending(i);
    if (listener >= 0)
        CloseSocket(listener);
    /* Channels and runners run our code: wait until the last one is gone. */
    for (;;) {
        Forbid();
        for (i = 0; i < CHANNELS; i++)
            if (chans[i].proc)
                Signal(&chans[i].proc->pr_Task, SIGBREAKF_CTRL_C);
        stop_tls();
        n = children;
        Permit();
        if (!n)
            break;
        Wait(1UL << chan_gone);
    }
    rexx_close();
    if (chan_gone >= 0)
        FreeSignal(chan_gone);
    if (reader) {
        reader_quit = TRUE;
        Signal(reader, SIGBREAKF_CTRL_C);
        Wait(1UL << reader_gone);
    }
    if (reader_gone >= 0)
        FreeSignal(reader_gone);
    ((struct Process *)server)->pr_WindowPtr = old_window;
    free_frame();
    if (input_io) {
        CloseDevice((struct IORequest *)input_io);
        DeleteIORequest((struct IORequest *)input_io);
    }
    if (input_port)
        DeleteMsgPort(input_port);
    if (timer_io) {
        CloseDevice((struct IORequest *)timer_io);
        DeleteIORequest((struct IORequest *)timer_io);
    }
    if (timer_port)
        DeleteMsgPort(timer_port);
    if (SocketBase) CloseLibrary(SocketBase);
    if (CyberGfxBase) CloseLibrary(CyberGfxBase);
    if (KeymapBase) CloseLibrary(KeymapBase);
    if (GfxBase) CloseLibrary((struct Library *)GfxBase);
    if (IntuitionBase) CloseLibrary((struct Library *)IntuitionBase);
    return rc;
}
