/*
 * amigentd: the front screen as 64x64 tiles, mouse and keyboard, and command
 * channels for command lines and files, over TCP.
 * The wire format is in docs/PROTOCOL.md. MIT licence, see LICENSE.
 *
 *   amigentd [PORT <n>] [SETUP] [KEY <file>] [QUIET]
 *
 * SETUP also accepts files (PUT) and command lines (RUN) until SETUP_END.
 * From Workbench the same come as tool types: PORT=<n>, SETUP, KEY=<file>, QUIET.
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
#include <netinet/tcp.h>
#include <sys/errno.h>
#include <sys/ioctl.h>
#include <string.h>

/* The SDK here has the CGX inline stubs but no clib or cybergraphx headers;
 * the two values below are from cybergraphx/cybergraphics.h. */
extern struct Library *CyberGfxBase;
#include <inline/cybergraphics_protos.h>

#define RECTFMT_RGB 0
#define CYBRMATTR_ISCYBERGFX 0x80000008

#define PROTOCOL_VERSION 3
#define DEFAULT_PORT 6801
#define TILE 64
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
#define OUT_SIZE 16384
#define SEND_BUFFER 16384  /* little queued, so a takeover is not stuck behind it */
#define SEND_STEP 2048  /* send low water mark: writable means this much room */
#define CHUNK 32768
#define PENDING 4  /* connections that have not said yet what they are */
#define CHANNELS 4  /* command channels next to the screen client */
#define HELLO_SECONDS 5  /* a newcomer's key has to arrive this fast */

#ifndef UNIQUE_ID
#define UNIQUE_ID (-1)
#endif

enum {
    CMD_INFO = 1, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING,
    CMD_CHAR, CMD_PUT, CMD_RUN, CMD_SETUP_END, CMD_SCREENS, CMD_SCREEN_FRONT,
    CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS, CMD_AUTH, CMD_CLIENT,
    CMD_CHANNEL = 20, CMD_EXEC, CMD_READ_FILE, CMD_WRITE_FILE, CMD_DIR, CMD_STAT, CMD_DELETE,
    CMD_RENAME, CMD_MAKEDIR, CMD_CHECKSUM, CMD_SYSINFO, CMD_VOLUMES
};

const char version[] = "$VER: amigentd 1.0.0 (02.11.2026)";

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
static struct timerequest *timer_io;

static LONG listener = -1, client = -1;
static BOOL setup_mode, quit, authed, from_wb, quiet;
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
    ULONG since;
} pend[PENDING];

/* Command channels run in processes of their own, see channel_main. */
static struct channel {
    struct Process *proc;
    LONG id;
    volatile BOOL busy;
    volatile ULONG last;
} chans[CHANNELS];
static BYTE chan_gone = -1;  /* a channel or exec runner ended */
static LONG children;  /* channel and runner processes alive, changed under Forbid */
static ULONG runs;  /* numbers temporary names, under Forbid */

static LONG watch(fd_set *r, LONG top);
static BOOL newcomers(fd_set *r);

static UBYTE out[OUT_SIZE];
static LONG out_len;
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
        ULONG sigs = SIGBREAKF_CTRL_C;
        LONG n, top;

        FD_ZERO(&r);
        FD_ZERO(&w);
        FD_SET(client, writing ? &w : &r);
        top = watch(&r, client);
        n = WaitSelect(top + 1, &r, &w, NULL, &tv, &sigs);
        if (sigs & SIGBREAKF_CTRL_C)
            quit = TRUE;
        if (n <= 0 || quit || newcomers(&r))
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
    UBYTE c = ch, keys[16];
    LONG n, i;

    if (ch > 255)
        return FALSE;
    n = MapANSI(&c, 1, keys, sizeof(keys) / 2, NULL);
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

static ULONG now_ms(void)
{
    struct timeval tv;
    ULONG ms;

    GetSysTime(&tv);
    ms = tv.tv_secs * 1000 + tv.tv_micro / 1000;
    return ms ? ms : 1;  /* 0 means never read */
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
    rgb = AllocVec(w * TILE * 3, MEMF_ANY);
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

/* Chipset screens: pens through the palette, HAM6 and HAM8 decoded along each row. */
static BOOL read_planar(struct Screen *scr, LONG x0, LONG y0, LONG w, LONG h, UBYTE *out)
{
    struct BitMap *bm = scr->RastPort.BitMap, *line;
    struct RastPort tmp;
    ULONG depth = GetBitMapAttr(bm, BMA_DEPTH), colours, i;
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
    line = AllocBitMap((width + 15) & ~15, 1, depth, 0, NULL);
    if (!table || !pens || !line) {
        FreeVec(table);
        FreeVec(pens);
        FreeBitMap(line);
        return FALSE;
    }
    GetRGB32(scr->ViewPort.ColorMap, 0, colours, table);
    memset(pal, 0, sizeof(pal));
    for (i = 0; i < 3 * colours; i++)
        pal[i] = table[i] >> 24;
    tmp = scr->RastPort;
    tmp.Layer = NULL;
    tmp.BitMap = line;
    for (y = 0; y < h; y++) {
        UBYTE *dst = out + y * w * 3;
        ReadPixelLine8(&scr->RastPort, from, y0 + y, width, pens, &tmp);
        if (!ham) {
            for (i = 0; i < (ULONG)w; i++, dst += 3) {
                const UBYTE *c = pal + 3 * pens[i];
                dst[0] = c[0], dst[1] = c[1], dst[2] = c[2];
            }
        } else {
            ham_row(pens, width, depth, pal, dst, x0 - from);
        }
    }
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

/* Any screen, as RGB888 rows of w pixels. Callers hold LockIBase. */
static void read_rgb(struct Screen *scr, LONG x0, LONG y0, LONG w, LONG h, UBYTE *out)
{
    if (is_rtg(scr) || !read_planar(scr, x0, y0, w, h, out)) {
        if (CyberGfxBase)
            ReadPixelArray(out, 0, 0, w * 3, &scr->RastPort, x0, y0, w, h, RECTFMT_RGB);
        else
            memset(out, 0, w * h * 3);  /* deeper than 8 planes, nothing to read it with */
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
    LONG x0 = tx0 * TILE, y0 = ty * TILE, x, n, i;
    LONG cw = ((tx1 + 1) * TILE < ref_w ? (tx1 + 1) * TILE : ref_w) - x0;
    LONG th = ref_h - y0 < TILE ? ref_h - y0 : TILE;
    ULONG ms = now_ms();
    UBYTE *src = rgb;

    read_rgb(scr, x0, y0, cw, th, rgb);
    for (n = 0; n < th; n++) {
        UBYTE *dst = cur + (y0 + n) * ref_w + x0;
        for (i = 0; i < cw; i++, src += 3)
            dst[i] = rgb332(src[0], src[1], src[2]);
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

static BOOL info(void)
{
    UBYTE msg[8];
    ULONG lock;

    lock = LockIBase(0);
    be16(msg, IntuitionBase->FirstScreen->Width);
    be16(msg + 2, IntuitionBase->FirstScreen->Height);
    UnlockIBase(lock);
    be16(msg + 4, TILE);
    be16(msg + 6, 0);
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
        be16(b + 8, GetBitMapAttr(s->RastPort.BitMap, BMA_DEPTH));
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
                            NP_StackSize, 65536, NP_Priority, 0, TAG_END);
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
    UBYTE buf[KEY_MAX];
    BPTR fh = Open(secret_file, MODE_OLDFILE);
    LONG n = 0, i;

    secret_len = 0;
    if (!fh)
        return;
    n = Read(fh, buf, sizeof(buf));
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
    hello[7] = secret_len ? 1 : 0;
    l.base = SocketBase;
    l.fd = fd;
    if (!l_send(&l, hello, 8, HELLO_SECONDS)) {
        close_fd(fd);
        return;
    }
    pend[i].fd = fd;
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
    char buf[100];

    if (!code)
        return c_fail(l, 1, "failed");
    Fault(code, NULL, buf, sizeof(buf));
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

static BOOL c_sysinfo(struct link *l)
{
    struct Library *SocketBase = l->base;
    struct text t = {0};

    t_pair(&t, "chip_free", AvailMem(MEMF_CHIP));
    t_pair(&t, "chip_total", AvailMem(MEMF_CHIP | MEMF_TOTAL));
    t_pair(&t, "chip_largest", AvailMem(MEMF_CHIP | MEMF_LARGEST));
    t_pair(&t, "fast_free", AvailMem(MEMF_FAST));
    t_pair(&t, "fast_total", AvailMem(MEMF_FAST | MEMF_TOTAL));
    t_pair(&t, "fast_largest", AvailMem(MEMF_FAST | MEMF_LARGEST));
    t_pair(&t, "total_free", AvailMem(MEMF_ANY));
    t_version(&t, "exec_version", SysBase->LibNode.lib_Version, SysBase->LibNode.lib_Revision);
    t_version(&t, "kickstart", SysBase->LibNode.lib_Version, SysBase->SoftVer);
    t_version(&t, "bsdsocket", SocketBase->lib_Version, SocketBase->lib_Revision);
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

/* A copy of the command path of the Shell that started us, for the command's
 * Shell, which frees it. Without it the command would only find C: programs.
 * Nodes are a BPTR to the next one and a lock. */
static BPTR copy_path(void)
{
    struct CommandLineInterface *cli = BADDR(((struct Process *)server)->pr_CLI);
    BPTR first = 0, *link = &first, *from, *node;

    if (!cli)
        return 0;  /* started from Workbench */
    for (from = BADDR(cli->cli_CommandDir); from; from = BADDR(from[0])) {
        if (!(node = AllocVec(2 * sizeof(BPTR), MEMF_PUBLIC | MEMF_CLEAR)))
            break;
        node[1] = DupLock(from[1]);
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
                           NP_StackSize, 65536, NP_Priority, 0, TAG_END);
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
                             NP_StackSize, 8192, NP_Priority, 0, TAG_END);
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
        struct timeval tv;
        ULONG sigs = (1UL << r->sig) | SIGBREAKF_CTRL_C;

        if (secs) {
            if ((left = secs * 1000L - (LONG)(now_ms() - start)) <= 0)
                break;
            tv.tv_sec = left / 1000;
            tv.tv_usec = left % 1000 * 1000;
        }
        WaitSelect(0, NULL, NULL, NULL, secs ? &tv : NULL, &sigs);
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
        c->last = now_ms();
        Forbid();
        children++;
        Permit();
        proc = CreateNewProcTags(NP_Entry, (ULONG)channel_main, NP_Name, (ULONG)"amigentd channel",
                                 NP_Priority, 0, NP_StackSize, 16384, TAG_END);
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

static BOOL command(const UBYTE *c)
{
    UWORD x = c[2] << 8 | c[3], y = c[4] << 8 | c[5];
    UWORD w = c[6] << 8 | c[7], h = c[8] << 8 | c[9];

    if (c[0] == CMD_AUTH)
        return auth(x);
    if (!authed)
        return refuse();
    switch (c[0]) {
    case CMD_INFO:
        return info();
    case CMD_FRAME:
        return frame(c[1] & 1, x, y, w, h);
    case CMD_MOVE:
        input_ms = now_ms();
        move_to(x, y);
        return reply(0);
    case CMD_BUTTON:
        input_ms = now_ms();
        set_buttons(c[1] & 7);
        return reply(0);
    case CMD_RAWKEY:
        input_ms = now_ms();
        key(x & 0x7F, c[1] & 1, y);
        return reply(0);
    case CMD_PING:
        return reply(0);
    case CMD_CHAR:
        input_ms = now_ms();
        return reply(type_char(x, y) ? 0 : 1);
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
    case CMD_MOVE_REL:
        input_ms = now_ms();
        move_by((WORD)x, (WORD)y);
        return reply(0);
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
    UBYTE cmd[10];
    LONG have = 0, n, top, i;
    ULONG heard = 0;

    while (!quit) {
        fd_set r;
        struct timeval tv = {1, 0};
        ULONG sigs = SIGBREAKF_CTRL_C;

        for (i = 0; i < PENDING && !(pend[i].fd >= 0 && pend[i].screen); i++)
            ;
        if (i < PENDING) {  /* the newcomer wins */
            drop_client();
            client = pend[i].fd;
            pend[i].fd = -1;
            authed = TRUE;
            client_id = 0;
            have = 0;
            heard = now_ms();
            if (!command(pend[i].cmd))
                drop_client();
            continue;
        }
        if (client >= 0 && now_ms() - heard > IDLE_SECONDS * 1000) {
            drop_client();  /* idle */
            have = 0;
        }
        FD_ZERO(&r);
        top = watch(&r, client);
        if (client >= 0)
            FD_SET(client, &r);
        n = WaitSelect(top + 1, &r, NULL, NULL, &tv, &sigs);
        if (sigs & SIGBREAKF_CTRL_C)
            return;
        if (n <= 0)
            continue;
        newcomers(&r);
        if (client < 0 || !FD_ISSET(client, &r))
            continue;
        n = recv(client, cmd + have, sizeof(cmd) - have, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0) {
            drop_client();
            have = 0;
            continue;
        }
        heard = now_ms();
        have += n;
        if (have == sizeof(cmd)) {
            have = 0;
            if (!command(cmd))
                drop_client();
        }
    }
}

/* ---- start ------------------------------------------------------------ */

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
    LONG args[4] = {0, 0, 0, 0};
    struct RDArgs *rda;
    UWORD port = DEFAULT_PORT;
    int rc = RETURN_FAIL;
    LONG i, n;

    if (argc == 0) {  /* Workbench: argv is the WBStartup message */
        from_wb = TRUE;
        tool_types((struct WBStartup *)argv, &port);
    } else {
        rda = ReadArgs("PORT/K/N,SETUP/S,KEY/K,QUIET/S", args, NULL);
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
        FreeArgs(rda);
    }

    server = FindTask(NULL);
    SetTaskPri(server, 2);
    for (i = 0; i < PENDING; i++)
        pend[i].fd = -1;
    InitSemaphore(&picture);
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 39);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 39);
    KeymapBase = OpenLibrary("keymap.library", 37);
    if (!IntuitionBase || !GfxBase || !KeymapBase) {
        complain("needs OS 3.0 or newer", 0);
        goto out;
    }
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
    if (!listen_on(port)) {
        complain("cannot listen on port %ld; is amigentd running already?", port);
        goto out;
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
        n = children;
        Permit();
        if (!n)
            break;
        Wait(1UL << chan_gone);
    }
    if (chan_gone >= 0)
        FreeSignal(chan_gone);
    if (reader) {
        reader_quit = TRUE;
        Signal(reader, SIGBREAKF_CTRL_C);
        Wait(1UL << reader_gone);
    }
    if (reader_gone >= 0)
        FreeSignal(reader_gone);
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
