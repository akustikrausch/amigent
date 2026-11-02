/*
 * amigentd: the front screen as 64x64 tiles, plus mouse and keyboard, over TCP.
 * The wire format is in docs/PROTOCOL.md. MIT licence, see LICENSE.
 *
 *   amigentd [PORT <n>] [SETUP] [KEY <file>]
 *
 * SETUP also accepts files (PUT) and command lines (RUN) until SETUP_END.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <dos/dos.h>
#include <dos/dostags.h>
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
#include <proto/exec.h>
#include <proto/timer.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/graphics.h>
#include <proto/keymap.h>
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

#define PROTOCOL_VERSION 2
#define DEFAULT_PORT 6801
#define TILE 64
#define RGB_MAX 512  /* FRAME_RGB: largest side in pixels */
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

enum {
    CMD_INFO = 1, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING,
    CMD_CHAR, CMD_PUT, CMD_RUN, CMD_SETUP_END, CMD_SCREENS, CMD_SCREEN_FRONT,
    CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS, CMD_AUTH, CMD_CLIENT
};

const char version[] = "$VER: amigentd 1.0.0 (02.11.2026)";

struct Device *TimerBase;
struct IntuitionBase *IntuitionBase;
struct GfxBase *GfxBase;
struct Library *KeymapBase;
struct Library *CyberGfxBase;
struct Library *SocketBase;

static struct MsgPort *input_port, *timer_port;
static struct IOStdReq *input_io;
static struct timerequest *timer_io;

static LONG listener = -1, client = -1;
static BOOL setup_mode, quit, authed;
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

static UBYTE out[OUT_SIZE];
static LONG out_len;
static UBYTE tile_buf[12 + TILE * TILE * 2];

/* ---- network ---------------------------------------------------------- */

/* Waits until the client can take data or has some. FALSE when a newcomer wants
 * to take over, on Ctrl-C, or after IO_SECONDS without progress. */
static BOOL ready(BOOL writing)
{
    fd_set r, w;
    struct timeval tv = {IO_SECONDS, 0};
    ULONG sigs = SIGBREAKF_CTRL_C;
    LONG n;

    FD_ZERO(&r);
    FD_ZERO(&w);
    FD_SET(listener, &r);
    FD_SET(client, writing ? &w : &r);
    n = WaitSelect((client > listener ? client : listener) + 1, &r, &w, NULL, &tv, &sigs);
    if (sigs & SIGBREAKF_CTRL_C)
        quit = TRUE;
    return n > 0 && !quit && !FD_ISSET(listener, &r);
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

/* Only a screen that is still in the list gets brought to the front. */
static BOOL screen_front(ULONG handle)
{
    struct Screen *s;
    ULONG lock;

    lock = LockIBase(0);
    for (s = IntuitionBase->FirstScreen; s && (ULONG)s != handle; s = s->NextScreen)
        ;
    UnlockIBase(lock);
    if (!s)
        return reply(1);
    ScreenToFront(s);
    input_ms = now_ms();  /* what was read before shows another screen */
    return reply(0);
}

static BOOL frame_rgb(LONG x, LONG y, LONG w, LONG h)
{
    struct Screen *scr;
    ULONG lock;
    UBYTE head[4], *buf = NULL;
    BOOL ok;

    lock = LockIBase(0);
    scr = IntuitionBase->FirstScreen;
    if (!w || x + w > scr->Width) w = scr->Width - x;
    if (!h || y + h > scr->Height) h = scr->Height - y;
    if (w <= 0 || h <= 0 || w > RGB_MAX || h > RGB_MAX || !(buf = AllocVec(w * h * 3, MEMF_ANY)))
        w = h = 0;
    else
        read_rgb(scr, x, y, w, h, buf);
    UnlockIBase(lock);
    be16(head, w);
    be16(head + 2, h);
    ok = put(head, 4) && put(buf, w * h * 3) && flush();
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

static BOOL auth(UWORD len)
{
    UBYTE got[KEY_MAX], diff;
    LONG i;

    memset(got, 0, sizeof(got));
    if (!receive(got, len < KEY_MAX ? len : KEY_MAX) || (len > KEY_MAX && !skip(len - KEY_MAX)))
        return FALSE;
    diff = len != secret_len;
    for (i = 0; i < KEY_MAX; i++)  /* the same work whatever matches */
        diff |= got[i] ^ secret[i];
    if (secret_len && diff)
        return refuse();
    authed = TRUE;
    return reply(0);
}

static void take_client(void)
{
    struct sockaddr_in addr;
    LONG len = sizeof(addr), fd, one = 1, low = SEND_STEP, buffer = SEND_BUFFER;
    UBYTE hello[8] = {'A', 'M', 'G', 'T', 0, PROTOCOL_VERSION, 0, 0};

    fd = accept(listener, (struct sockaddr *)&addr, &len);
    if (fd < 0)
        return;
    drop_client();  /* the newcomer wins */
    client = fd;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    IoctlSocket(fd, FIONBIO, (char *)&one);  /* waits go through ready() */
    setsockopt(fd, SOL_SOCKET, SO_SNDLOWAT, &low, sizeof(low));
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer));
    load_key();
    authed = secret_len == 0;
    client_id = 0;
    hello[7] = secret_len ? 1 : 0;
    if (!put(hello, 8) || !flush())
        drop_client();
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
        return frame_rgb(x, y, w, h);
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
    LONG have = 0, n;
    ULONG heard = 0;

    while (!quit) {
        fd_set r;
        struct timeval tv = {0, 0};
        ULONG sigs = SIGBREAKF_CTRL_C;
        LONG top = listener, left;

        FD_ZERO(&r);
        FD_SET(listener, &r);
        if (client >= 0) {
            FD_SET(client, &r);
            if (client > top)
                top = client;
            left = IDLE_SECONDS * 1000 - (LONG)(now_ms() - heard);
            if (left <= 0) {
                drop_client();  /* idle */
                have = 0;
                continue;
            }
            tv.tv_sec = left / 1000;
            tv.tv_usec = left % 1000 * 1000;
        }
        n = WaitSelect(top + 1, &r, NULL, NULL, client >= 0 ? &tv : NULL, &sigs);
        if (sigs & SIGBREAKF_CTRL_C)
            return;
        if (n <= 0)
            continue;
        if (FD_ISSET(listener, &r)) {
            take_client();
            heard = now_ms();
            have = 0;
            continue;
        }
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

int main(void)
{
    LONG args[3] = {0, 0, 0};
    struct RDArgs *rda;
    UWORD port = DEFAULT_PORT;
    int rc = RETURN_FAIL;

    rda = ReadArgs("PORT/K/N,SETUP/S,KEY/K", args, NULL);
    if (!rda) {
        PrintFault(IoErr(), "amigentd");
        return RETURN_FAIL;
    }
    if (args[0])
        port = *(LONG *)args[0];
    setup_mode = args[1] != 0;
    if (args[2]) {
        strncpy(secret_file, (const char *)args[2], sizeof(secret_file) - 1);
        secret_file[sizeof(secret_file) - 1] = 0;
    }
    FreeArgs(rda);

    server = FindTask(NULL);
    SetTaskPri(server, 2);
    InitSemaphore(&picture);
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 39);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 39);
    KeymapBase = OpenLibrary("keymap.library", 37);
    if (!IntuitionBase || !GfxBase || !KeymapBase)
        goto out;
    CyberGfxBase = OpenLibrary("cybergraphics.library", 40);  /* only with RTG */
    if (!(SocketBase = wait_for("bsdsocket.library", 4)))
        goto out;
    timer_port = CreateMsgPort();
    timer_io = (struct timerequest *)CreateIORequest(timer_port, sizeof(*timer_io));
    if (!timer_io || OpenDevice("timer.device", UNIT_MICROHZ, (struct IORequest *)timer_io, 0)) {
        if (timer_io)
            DeleteIORequest((struct IORequest *)timer_io);
        timer_io = NULL;
        goto out;
    }
    TimerBase = timer_io->tr_node.io_Device;
    input_port = CreateMsgPort();
    if (!input_port)
        goto out;
    input_io = (struct IOStdReq *)CreateIORequest(input_port, sizeof(*input_io));
    if (!input_io || OpenDevice("input.device", 0, (struct IORequest *)input_io, 0)) {
        if (input_io)
            DeleteIORequest((struct IORequest *)input_io);
        input_io = NULL;
        goto out;
    }
    if ((reader_gone = AllocSignal(-1)) < 0)
        goto out;
    reader = (struct Task *)CreateNewProcTags(NP_Entry, (ULONG)reader_main,
                                              NP_Name, (ULONG)"amigentd reader",
                                              NP_Priority, -1, NP_StackSize, 16384, TAG_END);
    if (!reader)
        goto out;
    if (!listen_on(port)) {
        PutStr("amigentd: cannot listen on that port\n");
        goto out;
    }
    if (setup_mode)
        PutStr("amigentd: SETUP mode, waiting for amigent-setup\n");
    serve();
    rc = RETURN_OK;

out:
    if (client >= 0 || input_io)
        drop_client();
    if (listener >= 0)
        CloseSocket(listener);
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
