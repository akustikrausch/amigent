/*
 * amigentd: the front screen as 64x64 tiles, mouse and keyboard, and command
 * channels for command lines and files, over TCP; with AmiSSL also over TLS.
 * The wire format is in docs/PROTOCOL.md. MIT licence, see LICENSE.
 *
 *   amigentd [PORT <n>] [SETUP] [KEY <file>] [QUIET] [TLSONLY] [NOTEXT] [NODEBUG]
 *
 * SETUP also accepts files (PUT) and command lines (RUN) until SETUP_END.
 * TLSONLY: remote plaintext is refused whether or not the TLS port opened.
 * Loopback stays open for the TLS pump, and SETUP keeps the plain port open.
 * NOTEXT, NODEBUG: don't record the text programs draw, or their debug output.
 * From Workbench the same come as tool types: PORT=<n>, SETUP, KEY=<file>, QUIET,
 * TLSONLY.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <exec/execbase.h>
#include <exec/resident.h>
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
#include <graphics/rastport.h>
#include <graphics/text.h>
#include <stdlib.h>
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

/* The SDK vbcc builds with here has the CGX inline stubs but no clib or
 * cybergraphx headers; the values below are from cybergraphx/cybergraphics.h.
 * GCC takes the headers that come with its toolchain, and the inline macros
 * it uses for the library calls declared in this file. */
#ifdef __VBCC__
extern struct Library *CyberGfxBase;
#include <inline/cybergraphics_protos.h>

#define RECTFMT_RGB 0
#define CYBRMATTR_BPPIX 0x80000002
#define CYBRMATTR_ISCYBERGFX 0x80000008
#elif !defined(AMIGENT_APOLLO)
/* Classic and Emu68 GCC builds take the toolchain's own cybergraphics
 * headers and inline stubs. */
#include <proto/cybergraphics.h>
#include <cybergraphx/cybergraphics.h>
#include <inline/macros.h>
#else
/* ApolloCrossDev's GCC 6.50 Stable ships no cybergraphics headers. The four
 * calls below are the ones this file makes, implemented in gcc_support.c at
 * the same offsets as the vbcc stubs. Apollo builds only. */
#include <inline/macros.h>
#include <utility/tagitem.h>
ULONG GetCyberMapAttr(struct BitMap *bm, ULONG attr);
void ReadPixelArray(APTR dest, ULONG destx, ULONG desty, ULONG destmod, struct RastPort *rp,
                    ULONG srcx, ULONG srcy, ULONG sizex, ULONG sizey, ULONG format);
APTR LockBitMapTagList(APTR bitmap, struct TagItem *tags);
void UnLockBitMap(APTR handle);
#define RECTFMT_RGB 0
#define CYBRMATTR_BPPIX 0x80000002
#define CYBRMATTR_ISCYBERGFX 0x80000008
#endif

#ifdef AMIGENT_APOLLO
/* The 68000 probe and this daemon share the identification. AUDIO is opcode
 * 49 on this build; EMU68 stays on opcode 48. */
#include "apollo_audio.h"
BOOL apollo_os_here(void);
UWORD apollo_board(void);
const char *apollo_board_name(UWORD board);
BOOL apollo_mmio_hit(ULONG at, ULONG n);
static volatile LONG audio_hold;
static volatile LONG audio_wait;
static struct Task *volatile audio_task;
#endif

#define PROTOCOL_VERSION 6
#define NAMES_MAX 4096  /* SYSINFO ports= and tasks= */
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

/* GCC's bsdsocket inline takes socklen_t *, which is not LONG *. vbcc's
 * prototype takes the length as this file already passes it. __saveds is
 * vbcc's base-register reload; GCC ignores it unless -fbaserel, which this
 * daemon does not use. */
#ifndef __VBCC__
#define AMIGENT_SOCKLEN(p) ((socklen_t *)(p))
#define AMIGENT_SAVEDS
#else
#define AMIGENT_SOCKLEN(p) (p)
#define AMIGENT_SAVEDS __saveds
#endif

enum {
    CMD_INFO = 1, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING,
    CMD_CHAR, CMD_PUT, CMD_RUN, CMD_SETUP_END, CMD_SCREENS, CMD_SCREEN_FRONT,
    CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS, CMD_AUTH, CMD_CLIENT, CMD_PAUSE,
    CMD_CHANNEL = 20, CMD_EXEC, CMD_READ_FILE, CMD_WRITE_FILE, CMD_DIR, CMD_STAT, CMD_DELETE,
    CMD_RENAME, CMD_MAKEDIR, CMD_CHECKSUM, CMD_SYSINFO, CMD_VOLUMES,
    CMD_TEXT, CMD_WAIT, CMD_COMPRESS, CMD_TREE, CMD_DEBUGLOG,  /* protocol 5 */
    CMD_NOTE,  /* protocol 5, "note" in SYSINFO: a line for the monitor, no answer */
    CMD_LOADED, CMD_PEEK, CMD_TASKS, CMD_TASKCTL, CMD_POKE, CMD_TRACE,  /* protocol 6 */
    CMD_LOCKS, CMD_CPU, CMD_DRAG, CMD_QUIET,  /* protocol 6, "probe" in SYSINFO */
    CMD_EMU68,  /* protocol 6, "emu68" in SYSINFO */
    CMD_AUDIO   /* protocol 6, opcode 49: ARNE mix, "audio=49" in SYSINFO (Apollo builds only) */
};

const char version[] = "$VER: amigentd 1.5.0 (21.11.2026)";

static void mon_say(UBYTE kind, const char *what, const char *arg);
static BOOL emu68_present(void);

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
static BOOL setup_mode, quit, authed, from_wb, quiet, tls_only, allow_poke;
static ULONG client_id, owner;  /* CLIENT ids: this connection's, and whose picture ref is */

/* With a key file, every connection has to send the key first. */
#define KEY_MAX 64
static char secret_file[256] = "S:amigent.key";
static UBYTE secret[KEY_MAX];
static LONG secret_len;
static UWORD buttons;
static UBYTE prev_code[2], prev_qual[2];

/* The reference: what the clients were last sent, kept across a takeover and
 * freed when the last screen client is gone (free_pictures).
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
static volatile ULONG net_in, net_out;  /* bytes on all links, for the monitor */
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
        net_out += n;
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
        net_in += n;
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

static ULONG now_ms(void);

/* 0x63 control, 0x66 left Amiga, 0x67 right Amiga. The keyboard pulls reset
 * when all three are down. A chord posted through input.device never reaches
 * that circuit, so the chord calls ColdReboot, the same reset C:Reboot runs.
 * Kickstart 2.04 has it. The three have to go down within CHORD_MS: a key
 * whose release never came (a client that broke off) must not turn two keys
 * pressed much later into a reset. */
#define CHORD_MS 5000
static void note_reset_chord(UBYTE code, BOOL down)
{
    static ULONG at[3];  /* when each went down, 0 while it is up */
    LONG i = code == 0x63 ? 0 : code == 0x66 ? 1 : code == 0x67 ? 2 : -1;
    ULONG ms;

    if (i < 0)
        return;
    if (!down) {
        at[i] = 0;
        return;
    }
    ms = now_ms() | 1;  /* never 0: that means up */
    at[i] = ms;
    for (i = 0; i < 3; i++)
        if (!at[i] || ms - at[i] > CHORD_MS)
            return;
    if (SysBase->LibNode.lib_Version >= 36)
        ColdReboot();
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
    note_reset_chord(code, down);
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

/* rgb332() through lookups for red and green, 256 bytes each; blue is a mask
 * anyway (#43). The tables reach rgb_to_332() through a pointer, so the
 * compiler keeps them in a register instead of loading the address a pixel. */
static UBYTE to332[2 * 256];
static UBYTE *to332_at;
static UBYTE islot[256];  /* index_tile(): colour to index, 0xFF for none, so again after each tile */

static void tables_init(void)
{
    LONG i;

    for (i = 0; i < 256; i++) {
        islot[i] = 0xFF;
        to332[i] = (UBYTE)(i >> 5);
        to332[256 + i] = (UBYTE)(i >> 5 << 3);
    }
    to332_at = to332;
}

/* n RGB888 pixels to RGB332. */
static void rgb_to_332(const UBYTE *src, UBYTE *dst, LONG n)
{
    const UBYTE *r = to332_at, *g = r + 256;
    UBYTE *end = dst + n, c;

    while (dst < end) {
        c = r[*src++];
        c |= g[*src++];
        *dst++ = c | (*src++ & 0xC0);
    }
}

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
    UBYTE pal[3 * 256];
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
        for (x = 0; x < 3 * 256; x++)  /* the palette as bytes once, not three shifts a pixel */
            pal[x] = table[x] >> 24;
        for (y = 0; y < h; y++) {
            const UBYTE *p = pens + y * pw;
            UBYTE *o = out + 3 * y * w;
            for (x = 0; x < w; x++, o += 3) {
                const UBYTE *c = pal + 3 * p[x];
                o[0] = c[0];
                o[1] = c[1];
                o[2] = c[2];
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

/* ---- ZZ9000 ------------------------------------------------------------- */

/* A ZZ9000 keeps its graphics card screen in the card's memory, so every read
 * of it crosses the Zorro bus: a 1280x720 Workbench took a 68060 over a second
 * to look at, changed or not. The ARM on the card has a console encoder
 * (zz9k.library, service 0x8200, written for AmiNetXDuo's web console) that
 * compares the screen with what it told its client last time and answers with
 * the tiles that changed: PackBits, XOR with the old tile, or a scroll. amigentd
 * keeps what it was told in mirror, at one or two bytes a pixel, and puts what
 * changed into cur. A quiet screen then costs a few calls to the card instead
 * of reading it. On anything unexpected the screen is read the usual way and
 * the next pass asks the card for the whole picture again.
 *
 * The encoder serves one client at a time. If AmiNetXDuo's console takes it,
 * the next answer is a whole picture, which this handles like any other. */

struct Library *ZZ9KBase;

#define ZZ_SERVICE 0x8200UL  /* the console encoder, and its one opcode */
#define ZZ_CODEC 0x0600UL    /* the codec service; its first opcode inflates */
#define ZZ_CODEC_ZLIB (1UL << 17)  /* service flag: zlib streams */
#define ZZ_ZLIB 2            /* ZZ9K_COMPRESSION_ZLIB */
#define ZZ_EXPECT_END 1      /* decompress flag: the input is the whole stream */
#define ZZ_FB_HANDLE 0x80000000UL  /* ZZ9K_SURFACE_HANDLE_FRAMEBUFFER: whatever the card shows */
#define ZZ_SURF_INDEX8 4     /* SDK surface formats */
#define ZZ_SURF_BGRA8888 7
#define ZZ_RFB_CLUT8 1       /* encoder formats */
#define ZZ_RFB_RGB565 2
#define ZZ_TILE_W 32         /* encoder tile: bytes */
#define ZZ_TILE_H 16         /* rows */
#define ZZ_ENC_FLAGS 0x7FUL  /* PackBits, XOR, changed planes, best of, scroll, only after a busy frame, runs of 2 */
#define ZZ_RESET 1           /* request flag: start from black */
#define ZZ_KEYFRAME 1        /* answer flag: this started from black */
#define ZZ_DEFLATED 1        /* message flag: the ops are zlib */
#define ZZ_OK 0              /* ZZ9K_STATUS_OK */
#define ZZ_QUEUED 1          /* ZZ9K_STATUS_QUEUED */
#define ZZ_WAIT_MS 2000      /* an answer later than this: the card is left alone */
#define ZZ_GIVE_UP 3         /* failed passes in a row before the card is left alone */
#define ZZ_SHARE_MS 30000    /* another client of the encoder twice this close: leave it the card this long */
#define ZZ_BAND 15           /* tile rows a call at most: every call holds up the card's other work */

/* zz9k.library's own structures, from the ZZ9000 SDK's zz9k/host.h, abi.h and
 * library.h. */
struct zz_entry {
    ULONG request_id;
    UWORD opcode, status, flags, payload_len;
    ULONG user_cookie;
    UBYTE payload[48];
};
struct zz_shared {
    ULONG handle;
    APTR data;
    ULONG length;
};
struct zz_surface {
    ULONG handle, arm_addr;
    APTR data;
    ULONG width, height, pitch, format, flags, length;
};
struct zz_service {
    ULONG service_id, version, caps, flags, opcode_base, opcode_count, max_inline;
    char name[22];
};
struct zz_async {
    struct Message message;
    struct zz_entry request, reply;
    ULONG request_id;
    LONG status;
    UBYTE completed, queued;
    APTR callback, user_data;
    struct MsgPort *reply_port;
    APTR next;
    UBYTE spare[16];  /* room, should the library's alignment differ from ours */
};

#ifdef __VBCC__
LONG __ZZ9KQueryService(__reg("a6") struct Library *, __reg("d0") ULONG id,
                        __reg("a0") struct zz_service *info) = "\tjsr\t-36(a6)";
LONG __ZZ9KAllocShared(__reg("a6") struct Library *, __reg("d0") ULONG length, __reg("d1") ULONG align,
                       __reg("d2") ULONG flags, __reg("a0") struct zz_shared *buf) = "\tjsr\t-72(a6)";
LONG __ZZ9KFreeShared(__reg("a6") struct Library *, __reg("d0") ULONG handle) = "\tjsr\t-78(a6)";
LONG __ZZ9KMapFramebufferSurface(__reg("a6") struct Library *,
                                 __reg("a0") struct zz_surface *surface) = "\tjsr\t-114(a6)";
LONG __ZZ9KCallAsyncMsg(__reg("a6") struct Library *, __reg("a0") struct zz_async *async,
                        __reg("a1") struct zz_entry *req, __reg("a2") struct MsgPort *port) = "\tjsr\t-132(a6)";
LONG __ZZ9KCancelAsync(__reg("a6") struct Library *, __reg("a0") struct zz_async *async) = "\tjsr\t-144(a6)";
#define ZZ9KQueryService(id, info) __ZZ9KQueryService(ZZ9KBase, (id), (info))
#define ZZ9KAllocShared(length, align, flags, buf) __ZZ9KAllocShared(ZZ9KBase, (length), (align), (flags), (buf))
#define ZZ9KFreeShared(handle) __ZZ9KFreeShared(ZZ9KBase, (handle))
#define ZZ9KMapFramebufferSurface(surface) __ZZ9KMapFramebufferSurface(ZZ9KBase, (surface))
#define ZZ9KCallAsyncMsg(async, req, port) __ZZ9KCallAsyncMsg(ZZ9KBase, (async), (req), (port))
#define ZZ9KCancelAsync(async) __ZZ9KCancelAsync(ZZ9KBase, (async))
#else
#define ZZ9KQueryService(id, info) LP2(36, LONG, ZZ9KQueryService, ULONG, (id), d0, \
    struct zz_service *, (info), a0, struct Library *, ZZ9KBase)
#define ZZ9KAllocShared(length, align, flags, buf) LP4(72, LONG, ZZ9KAllocShared, ULONG, (length), d0, \
    ULONG, (align), d1, ULONG, (flags), d2, struct zz_shared *, (buf), a0, struct Library *, ZZ9KBase)
#define ZZ9KFreeShared(handle) LP1(78, LONG, ZZ9KFreeShared, ULONG, (handle), d0, struct Library *, ZZ9KBase)
#define ZZ9KMapFramebufferSurface(surface) LP1(114, LONG, ZZ9KMapFramebufferSurface, \
    struct zz_surface *, (surface), a0, struct Library *, ZZ9KBase)
#define ZZ9KCallAsyncMsg(async, req, port) LP3(132, LONG, ZZ9KCallAsyncMsg, struct zz_async *, (async), a0, \
    struct zz_entry *, (req), a1, struct MsgPort *, (port), a2, struct Library *, ZZ9KBase)
#define ZZ9KCancelAsync(async) LP1(144, LONG, ZZ9KCancelAsync, struct zz_async *, (async), a0, \
    struct Library *, ZZ9KBase)
#endif

#ifndef LBMI_BASEADDRESS
#define LBMI_BASEADDRESS 0x84001007
#endif

static struct {
    struct zz_shared out;   /* where the card writes its answer; handle 0 when there is none */
    struct zz_shared plain; /* and inflates it */
    struct zz_async async;  /* the request in flight */
    UBYTE *mirror;          /* the screen as the card told it: rows of bpr bytes */
    UBYTE *ops;             /* an answer copied out */
    UBYTE *tile;            /* one tile unpacked */
    ULONG *table;           /* CLUT8: the palette as RGB32 */
    ULONG cap;              /* bytes in out, plain and ops */
    UWORD w, h, bpr, tiles_x, tiles_y, band, seq;
    UBYTE format, bpp;      /* encoder format and its bytes a pixel */
    BOOL reset;             /* the next pass starts from black */
    BOOL live;              /* this look's tiles come from the card */
    BOOL shown;             /* the last look at the screen went through the card, for SYSINFO */
    struct Screen *scr;     /* the screen the last pass looked at */
    UBYTE fails;
    ULONG foreign_ms;       /* when another client of the encoder last took a turn */
    ULONG off_until;        /* the card is left to it until then; 0 for not */
    ULONG ms;               /* when the last good pass started; 0 for none */
    UBYTE pal[256];         /* CLUT8: each pen as RGB332 */
    UBYTE *lut;             /* RGB565 to RGB332, 64 KB */
    UBYTE *cur;             /* the cur the mirror was put into; NULL once anything else wrote there */
} zz;

static ULONG zz_get16(const UBYTE *p) { return (ULONG)p[0] << 8 | p[1]; }
static ULONG zz_get32(const UBYTE *p) { return zz_get16(p) << 16 | zz_get16(p + 2); }

/* zz9k.library and a firmware with the console encoder and zlib, or nothing. */
static void zz_open(void)
{
    struct zz_service s, c;
    ULONG i;

    if (!CyberGfxBase || !(ZZ9KBase = OpenLibrary("zz9k.library", 2)))
        return;
    memset(&s, 0, sizeof(s));
    memset(&c, 0, sizeof(c));
    if (ZZ9KQueryService(ZZ_SERVICE, &s) || s.opcode_base != ZZ_SERVICE || !s.opcode_count ||
        ZZ9KQueryService(ZZ_CODEC, &c) || !(c.flags & ZZ_CODEC_ZLIB) ||
        !(zz.lut = AllocVec(0x10000, MEMF_ANY))) {
        CloseLibrary(ZZ9KBase);
        ZZ9KBase = NULL;
        return;
    }
    for (i = 0; i < 0x10000; i++)  /* rgb332() of the RGB888 the card made the RGB565 of */
        zz.lut[i] = (UBYTE)(i >> 13 | (i >> 5 & 0x38) | (i << 3 & 0xC0));
}

/* Buffers for another geometry: all gone. */
static void zz_free(void)
{
    if (zz.out.handle)
        ZZ9KFreeShared(zz.out.handle);
    if (zz.plain.handle)
        ZZ9KFreeShared(zz.plain.handle);
    FreeVec(zz.mirror);
    FreeVec(zz.ops);
    FreeVec(zz.tile);
    FreeVec(zz.table);
    zz.out.handle = zz.plain.handle = 0;
    zz.mirror = zz.ops = zz.tile = NULL;
    zz.table = NULL;
    zz.w = zz.h = 0;
}

static void zz_close(void)
{
    if (ZZ9KBase) {
        zz_free();
        FreeVec(zz.lut);
        CloseLibrary(ZZ9KBase);
        ZZ9KBase = NULL;
    }
}

/* One request to the card. The answer comes through the card's interrupt: the
 * library's own wait polls once a tick, which put 30 ms on every call. FALSE on
 * an error; after ZZ_WAIT_MS without an answer the card is left alone for good,
 * its buffers and the port kept, since the answer may still come. */
static BOOL zz_call(struct zz_entry *req, struct zz_entry *reply)
{
    struct MsgPort *port = CreateMsgPort();
    BOOL got = FALSE;

    if (!port)
        return FALSE;
    memset(&zz.async, 0, sizeof(zz.async));
    if (ZZ9KCallAsyncMsg(&zz.async, req, port) != ZZ_QUEUED) {
        DeleteMsgPort(port);
        return FALSE;
    }
    while (!(got = GetMsg(port) != NULL) && wait_ms(ZZ_WAIT_MS, 1UL << port->mp_SigBit))
        ;
    if (!got) {
        ZZ9KCancelAsync(&zz.async);
        zz.fails = ZZ_GIVE_UP;
        return FALSE;
    }
    DeleteMsgPort(port);
    *reply = zz.async.reply;
    return zz.async.status == 0;
}

static void zz_request(struct zz_entry *req, ULONG opcode)
{
    memset(req, 0, sizeof(*req));
    req->opcode = opcode;
    req->payload_len = 48;
}

/* Is the card showing scr, in a format the encoder serves? Then the buffers fit
 * it. A new geometry starts a new session on the card, from black. */
static BOOL zz_fit(struct Screen *scr)
{
    struct zz_surface fb;
    struct TagItem tags[2];
    ULONG base = 0, bpr, size = 0;
    APTR lock;
    UBYTE format, bpp;
    UWORD band;

    if (!is_rtg(scr))
        return FALSE;
    tags[0].ti_Tag = LBMI_BASEADDRESS;
    tags[0].ti_Data = (ULONG)&base;
    tags[1].ti_Tag = TAG_DONE;
    if (!(lock = LockBitMapTagList(scr->RastPort.BitMap, tags)))
        return FALSE;
    UnLockBitMap(lock);
    /* The framebuffer handle names whatever the card shows when it is used. */
    memset(&fb, 0, sizeof(fb));
    if (ZZ9KMapFramebufferSurface(&fb) || !base || (ULONG)fb.data != base ||
        fb.width != (ULONG)scr->Width || fb.height != (ULONG)scr->Height)
        return FALSE;
    if (fb.format == ZZ_SURF_BGRA8888) {  /* the card makes RGB565 of it */
        format = ZZ_RFB_RGB565;
        bpp = 2;
        bpr = fb.width * 2;
    } else if (fb.format == ZZ_SURF_INDEX8) {
        format = ZZ_RFB_CLUT8;
        bpp = 1;
        bpr = fb.pitch;
    } else
        return FALSE;
    if (bpr > 0xFFFF || fb.handle != ZZ_FB_HANDLE)
        return FALSE;
    if (zz.mirror && zz.w == fb.width && zz.h == fb.height && zz.bpr == bpr && zz.format == format)
        return TRUE;
    zz_free();
    zz.tiles_x = (bpr + ZZ_TILE_W - 1) / ZZ_TILE_W;
    zz.tiles_y = (fb.height + ZZ_TILE_H - 1) / ZZ_TILE_H;
    if (!(zz.mirror = AllocVec(bpr * fb.height, MEMF_ANY | MEMF_CLEAR)) ||
        !(zz.tile = AllocVec(ZZ_TILE_W * ZZ_TILE_H, MEMF_ANY)) ||
        !(zz.table = AllocVec(256 * 3 * sizeof(ULONG), MEMF_ANY | MEMF_CLEAR))) {
        zz_free();
        return FALSE;
    }
    /* Bands of tile rows keep each call short, so the card's other work (the
     * network, the blitter) never waits long; fewer rows if memory is short. */
    for (band = ZZ_BAND; band; band /= 2) {
        size = band * zz.tiles_x * (ZZ_TILE_W * ZZ_TILE_H + 6) + 32;  /* every tile raw, plus a scroll */
        if (ZZ9KAllocShared(size, 16, 0, &zz.out) == ZZ_OK)
            break;
        zz.out.handle = 0;  /* refused: fewer rows */
    }
    if (band && ZZ9KAllocShared(size, 16, 0, &zz.plain) != ZZ_OK)
        zz.plain.handle = 0;
    if (!band || !zz.plain.handle || !(zz.ops = AllocVec(size, MEMF_ANY))) {
        zz_free();
        return FALSE;
    }
    zz.cap = size;
    zz.band = band;
    zz.w = fb.width;
    zz.h = fb.height;
    zz.bpr = bpr;
    zz.format = format;
    zz.bpp = bpp;
    zz.reset = TRUE;
    zz.cur = NULL;
    return TRUE;
}

/* Puts the mirror's pixels x..x+w (bytes), rows y..y+h into cur, and marks the
 * tiles there as changed: the next look checks them against ref. */
static void zz_show(ULONG x, ULONG y, ULONG w, ULONG h)
{
    ULONG px0 = x / zz.bpp, px1 = (x + w + zz.bpp - 1) / zz.bpp, r, i, tx, ty, bpr = zz.bpr;
    const UBYTE *m0 = zz.mirror + y * bpr + px0 * zz.bpp, *lut = zz.lut, *pal = zz.pal;
    UBYTE *c0 = cur + y * ref_w + px0;

    if (px1 > zz.w)
        px1 = zz.w;
    if (px0 >= px1)
        return;
    /* Locals throughout: a store through c could change zz.lut for all a
     * compiler knows, and it would load it again for every pixel. */
    for (r = h; r; r--, m0 += bpr, c0 += ref_w) {
        UBYTE *c = c0;
        if (zz.format == ZZ_RFB_RGB565) {
            const UWORD *m = (const UWORD *)m0;
            for (i = px1 - px0; i; i--)
                *c++ = lut[*m++];
        } else {
            const UBYTE *m = m0;
            for (i = px1 - px0; i; i--)
                *c++ = pal[*m++];
        }
    }
    for (ty = y / TILE; ty * TILE < y + h; ty++)
        for (tx = px0 / TILE; tx * TILE < px1; tx++)
            stamp[ty * cols + tx] = 0;
}

/* PackBits of unit 1 or 2 bytes into exactly size bytes; FALSE on bad data. */
static BOOL zz_unpack(const UBYTE *in, ULONG n, UBYTE *out, ULONG size)
{
    const UBYTE *end = in + n;
    UBYTE *o = out, *oend = out + size;
    ULONG unit = zz.bpp, c, k;

    while (in < end) {
        c = *in++;
        if (c < 128) {
            k = (c + 1) * unit;
            if ((ULONG)(end - in) < k || (ULONG)(oend - o) < k)
                return FALSE;
            memcpy(o, in, k);
            in += k;
            o += k;
        } else if (c > 128) {
            k = 257 - c;
            if ((ULONG)(end - in) < unit || (ULONG)(oend - o) < k * unit)
                return FALSE;
            if (unit == 1)
                memset(o, *in, k);
            else {
                UWORD v, *q = (UWORD *)o;  /* o is even: units of 2 */
                memcpy(&v, in, 2);  /* in may be odd */
                for (c = k; c; c--)
                    *q++ = v;
            }
            o += k * unit;
            in += unit;
        }
    }
    return o == oend;
}

/* The ops of one answer into the mirror, and from there into cur. Format: the
 * ZZ9000 firmware's rfb/rfb_encode.h. FALSE on anything malformed. */
static BOOL zz_apply(const UBYTE *p, ULONG n)
{
    const UBYTE *end = p + n;
    ULONG op, x0, w, y0, h, r, idx, top, tw, th, size, code, len;
    LONG dy;
    UBYTE *at;

    while (p < end) {
        op = *p++;
        if (op == 0)  /* the end */
            return p == end;
        if (op == 1) {  /* a scroll: rows y0.. take the rows dy further down */
            if (end - p < 10)
                return FALSE;
            x0 = zz_get16(p);
            w = zz_get16(p + 2);
            y0 = zz_get16(p + 4);
            h = zz_get16(p + 6);
            dy = (WORD)zz_get16(p + 8);
            p += 10;
            if (x0 + w > zz.bpr || y0 + h > zz.h || (LONG)y0 + dy < 0 || (LONG)(y0 + h) + dy > zz.h)
                return FALSE;
            if (dy > 0)
                for (r = 0; r < h; r++)
                    memmove(zz.mirror + (y0 + r) * zz.bpr + x0, zz.mirror + (y0 + r + dy) * zz.bpr + x0, w);
            else
                for (r = h; r; r--)
                    memmove(zz.mirror + (y0 + r - 1) * zz.bpr + x0,
                            zz.mirror + (ULONG)((LONG)(y0 + r - 1) + dy) * zz.bpr + x0, w);
            zz_show(x0, y0, w, h);
        } else if (op == 3) {  /* one tile of a chunky screen */
            if (end - p < 3)
                return FALSE;
            idx = zz_get16(p);
            code = p[2];
            p += 3;
            if (idx / zz.tiles_x >= zz.tiles_y)
                return FALSE;
            x0 = idx % zz.tiles_x * ZZ_TILE_W;
            top = idx / zz.tiles_x * ZZ_TILE_H;
            tw = zz.bpr - x0 < ZZ_TILE_W ? zz.bpr - x0 : ZZ_TILE_W;
            th = zz.h - top < ZZ_TILE_H ? zz.h - top : ZZ_TILE_H;
            size = tw * th;
            at = zz.mirror + top * zz.bpr + x0;
            if (code == 0) {  /* raw */
                if ((ULONG)(end - p) < size)
                    return FALSE;
                for (r = 0; r < th; r++)
                    memcpy(at + r * zz.bpr, p + r * tw, tw);
                p += size;
            } else if (code == 1 || code == 2) {  /* PackBits, of the tile or of its XOR with the old one */
                if (end - p < 2)
                    return FALSE;
                len = zz_get16(p);
                p += 2;
                if ((ULONG)(end - p) < len || !zz_unpack(p, len, zz.tile, size))
                    return FALSE;
                p += len;
                for (r = 0; r < th; r++) {
                    UBYTE *d = at + r * zz.bpr;
                    const UBYTE *s = zz.tile + r * tw;
                    if (code == 1)
                        memcpy(d, s, tw);
                    else if (!(((ULONG)d | tw) & 3))  /* the tile buffer is long aligned */
                        for (len = tw >> 2; len; len--)
                            *(ULONG *)d ^= *(const ULONG *)s, d += 4, s += 4;
                    else
                        for (len = tw; len; len--)
                            *d++ ^= *s++;
                }
            } else
                return FALSE;
            zz_show(x0, top, tw, th);
        } else
            return FALSE;
    }
    return FALSE;  /* no end */
}

/* All of the mirror into cur: after another screen, a new cur or palette. */
static void zz_show_all(void)
{
    zz_show(0, 0, zz.bpr, zz.h);
    zz.cur = cur;
}

/* The card starts over from black: mirror and cur are black, every tile due. */
static void zz_blank(void)
{
    LONG r;

    memset(zz.mirror, 0, zz.bpr * zz.h);
    for (r = 0; r < zz.h; r++)
        memset(cur + r * ref_w, zz.format == ZZ_RFB_RGB565 ? 0 : zz.pal[0], zz.w);
    memset(stamp, 0, cols * rows * sizeof(ULONG));
    zz.cur = cur;
}

/* Another client of the encoder (AmiNetXDuo's web console) had a turn, so the
 * card started over from black for one of us. Twice within ZZ_SHARE_MS: leave
 * it the card for that long instead of both starting over on every look. */
static void zz_foreign(void)
{
    ULONG ms = now_ms();

    if (zz.foreign_ms && ms - zz.foreign_ms < ZZ_SHARE_MS)
        zz.off_until = (ms + ZZ_SHARE_MS) | 1;
    zz.foreign_ms = ms;
}

/* One look at the whole screen through the card. FALSE when the mirror can't
 * be trusted any more. */
static BOOL zz_pass(void)
{
    struct zz_entry req, rep;
    ULONG n;
    UWORD ty, ty1, seq;
    UBYTE head[4];

    for (ty = 0; ty < zz.tiles_y; ty = ty1) {
        ty1 = ty + zz.band < zz.tiles_y ? ty + zz.band : zz.tiles_y;
        zz_request(&req, ZZ_SERVICE);
        be32(req.payload, ZZ_FB_HANDLE);
        be32(req.payload + 4, zz.out.handle);
        be32(req.payload + 8, zz.cap);
        be32(req.payload + 12, ZZ_ENC_FLAGS);
        be16(req.payload + 16, zz.w);
        be16(req.payload + 18, zz.h);
        be16(req.payload + 20, zz.bpr);
        be16(req.payload + 22, ty);
        be16(req.payload + 24, ty1);
        be16(req.payload + 26, !ty && zz.reset ? ZZ_RESET : 0);
        req.payload[30] = zz.bpp * 8;
        req.payload[31] = ZZ_TILE_W;
        req.payload[32] = ZZ_TILE_H;
        req.payload[33] = zz.format;
        if (!zz_call(&req, &rep) || (n = zz_get32(rep.payload)) < 5 || n > zz.cap)
            return FALSE;
        CopyMem(zz.out.data, head, 4);
        if (head[0] != 1)  /* encoder version */
            return FALSE;
        seq = zz_get16(head + 2);
        if (zz_get16(rep.payload + 6) & ZZ_KEYFRAME) {  /* from black, asked for or not */
            if (ty) {  /* the bands before this one would stay black: start over */
                zz_foreign();
                return FALSE;
            }
            if (!zz.reset)
                zz_foreign();
            zz_blank();
            zz.reset = FALSE;
        } else if (zz.reset || seq != (UWORD)(zz.seq + 1)) {
            zz_foreign();
            return FALSE;  /* someone else got an answer in between */
        }
        zz.seq = seq;
        n -= 4;
        if (head[1] & ZZ_DEFLATED) {
            /* The card packs its answer with zlib, for AmiNetXDuo's network.
             * Here it crosses the Zorro bus, faster than a 68k inflates:
             * the card unpacks it again into plain. */
            zz_request(&req, ZZ_CODEC);
            be32(req.payload, zz.out.handle);
            be32(req.payload + 4, 4);
            be32(req.payload + 8, n);
            be32(req.payload + 12, zz.plain.handle);
            be32(req.payload + 20, zz.cap);
            be32(req.payload + 24, ZZ_ZLIB);
            be32(req.payload + 28, ZZ_EXPECT_END);
            if (!zz_call(&req, &rep) || (n = zz_get32(rep.payload + 4)) > zz.cap)
                return FALSE;
            CopyMem(zz.plain.data, zz.ops, n);
        } else
            CopyMem((UBYTE *)zz.out.data + 4, zz.ops, n);
        if (!zz_apply(zz.ops, n))
            return FALSE;
    }
    return TRUE;
}

/* Brings cur up to date through the card when it shows scr; FALSE when the
 * screen has to be read the usual way. Callers hold LockIBase and picture. */
static BOOL zz_sync(struct Screen *scr, BOOL force)
{
    ULONG ms = now_ms(), i;
    struct Screen *last = zz.scr;
    BOOL again;

    /* Until this look goes through the card, the last pass answers for no
     * screen: whatever was read the usual way in between may have changed it. */
    zz.live = zz.shown = FALSE;
    zz.scr = NULL;
    if (zz.off_until && (LONG)(ms - zz.off_until) < 0)
        return FALSE;
    zz.off_until = 0;
    if (!ZZ9KBase || zz.fails >= ZZ_GIVE_UP || !zz_fit(scr))
        return FALSE;
    if (zz.format == ZZ_RFB_CLUT8) {  /* a new palette changes every pixel and no tile */
        UBYTE pal[256];
        ULONG colours = scr->ViewPort.ColorMap ? scr->ViewPort.ColorMap->Count : 0;
        if (colours > 256)
            colours = 256;
        if (colours)
            get_rgb32(scr->ViewPort.ColorMap, 0, colours, zz.table);
        for (i = 0; i < 256; i++)
            pal[i] = rgb332(zz.table[3 * i] >> 24, zz.table[3 * i + 1] >> 24, zz.table[3 * i + 2] >> 24);
        if (memcmp(pal, zz.pal, 256)) {
            memcpy(zz.pal, pal, 256);
            zz.cur = NULL;
        }
    }
    /* A pass answers for the screen as long as a tile read then would (stale()),
     * and only for the screen it looked at: another one of the same size in
     * front starts over in front(), and so does this. */
    again = force || zz.reset || !zz.ms || scr != last || ms - zz.ms > MAX_AGE_MS ||
            (input_ms && (LONG)(zz.ms - input_ms) < SETTLE_MS);
    if (again) {
        if (!zz_pass()) {
            zz.reset = TRUE;
            zz.ms = 0;
            zz.fails++;
            return FALSE;
        }
        zz.fails = 0;
        zz.ms = ms;
    }
    if (zz.cur != cur)
        zz_show_all();
    zz.live = zz.shown = TRUE;
    zz.scr = scr;
    return TRUE;
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

/* Reads tiles tx0..tx1 of tile row ty from the screen into cur, as RGB332;
 * through the card, zz_sync() has put them there already. */
static void read_tiles(struct Screen *scr, LONG ty, LONG tx0, LONG tx1)
{
    LONG y0 = ty * TILE, x, n, t0;
    LONG th = ref_h - y0 < TILE ? ref_h - y0 : TILE;
    ULONG ms = now_ms();

    for (t0 = tx0; t0 <= tx1 && !zz.live; t0 += RGB_TILES) {  /* a couple of tiles at a time */
        LONG x0 = t0 * TILE, t1 = t0 + RGB_TILES - 1 < tx1 ? t0 + RGB_TILES - 1 : tx1;
        LONG cw = ((t1 + 1) * TILE < ref_w ? (t1 + 1) * TILE : ref_w) - x0;
        UBYTE *src = rgb;

        read_rgb(scr, x0, y0, cw, th, rgb);
        zz.cur = NULL;  /* cur holds what the card's mirror doesn't */
        for (n = 0; n < th; n++, src += 3 * cw)
            rgb_to_332(src, cur + (y0 + n) * ref_w + x0, cw);
    }
    for (x = tx0; x <= tx1; x++) {
        UBYTE *v = valid + ty * cols + x;
        /* Through the card, a tile it did not change kept its stamp: still as
         * different from ref as last time, so no compare of 4 KB. */
        if (zz.live && stamp[ty * cols + x])
            ;
        else if (differs(x * TILE, y0, (x + 1) * TILE < ref_w ? TILE : ref_w - x * TILE, th))
            *v |= DIFF;
        else
            *v &= ~DIFF;
        stamp[ty * cols + x] = zz.live ? zz.ms : ms;  /* through the card: when it looked */
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
    zz_sync(scr, force);
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

/* One step of background reading: up to eight wanted tiles of one tile row, or
 * through the card all of them. */
static void refresh(void)
{
    struct Screen *scr;
    ULONG lock;
    LONG t, tx0, tx1;

    if (!wanted || !(scr = front(&lock)))
        return;
    zz_sync(scr, TRUE);  /* the reader is there to look again */
    for (t = 0; t < cols * rows; t = t - tx0 + cols) {
        for (; t < cols * rows && !want[t]; t++)
            ;
        if (t == cols * rows)
            break;
        tx0 = t % cols;
        for (tx1 = cols - 1; !want[t - tx0 + tx1]; tx1--)
            ;
        if (tx1 > tx0 + 7 && !zz.live)  /* small steps keep everyone else waiting only briefly */
            tx1 = tx0 + 7;
        read_tiles(scr, t / cols, tx0, tx1);
        if (!zz.live)
            break;
    }
    UnlockIBase(lock);
}

/* Reads ahead at priority -1, a few tiles at a time, whenever a frame asks for it. */
static void AMIGENT_SAVEDS reader_main(void)
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
        } while (more && !reader_quit
#ifdef AMIGENT_APOLLO
                 && !audio_hold
#endif
                );
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
 * takes an odd address as a personal insult. PackBits of noise is a byte in
 * 128 longer than the tile, and a scrolled delta two more. */
#define PENC_LONGS ((8 * 8 * TILE + 8 * 8 * TILE / 128 + 16) / 4)
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
#ifdef __VBCC__
LONG cmp_longs(__reg("a0") const ULONG *a, __reg("a1") const ULONG *b, __reg("d0") LONG n) =
    "\tsubq.w\t#1,d0\n\tcmpm.l\t(a0)+,(a1)+\n\tdbne\td0,*-2\n\text.l\td0";
#else
static inline LONG cmp_longs(const ULONG *a, const ULONG *b, LONG n)
{
    __asm__ volatile("subq.w #1,%0\n1:\tcmpm.l (%1)+,(%2)+\n\tdbne %0,1b\n\text.l %0"
                     : "+d"(n), "+a"(a), "+a"(b) : : "cc", "memory");
    return n;
}
#endif

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
 * The search for a run keeps to a few registers and one load a byte; the
 * bookkeeping only happens at a run: the 68000 has no time for second looks. */
static LONG packbits(const UBYTE *s, LONG len, UBYTE *d)
{
    const UBYTE *end = s + len, *stop = end - 2, *lit = s, *r, *q, *lim;
    UBYTE *start = d, c, n;
    LONG k;

    if (len >= 3) {
        c = *s++;  /* s is one past the byte a run might start at, c */
        for (;;) {
            while (s <= stop) {
                if ((n = *s++) != c) {
                    c = n;
                    continue;
                }
                if (*s == c)  /* three alike */
                    goto run;
                c = *s++;  /* two alike, then another: look on from there */
            }
            break;
run:
            s -= 2;
            while (lit < s) {  /* literals waiting in front of the run */
                k = s - lit > 128 ? 128 : s - lit;
                *d++ = (UBYTE)(k - 1);
                memcpy(d, lit, k);
                d += k, lit += k;
            }
            q = end - s > 16 ? s + 16 : end;
            for (r = s + 3; r < q && *r == c; r++)
                ;
            if (r == q && r < end) {  /* a long run: four bytes a compare once aligned */
                ULONG four = c;
                lim = end - s > 128 ? s + 128 : end;
                for (; r < lim && ((ULONG)r & 3) && *r == c; r++)
                    ;
                four |= four << 8;
                four |= four << 16;
                if (!((ULONG)r & 3))
                    while (lim - r >= 4 && *(const ULONG *)r == four)
                        r += 4;
                for (; r < lim && *r == c; r++)
                    ;
            }
            *d++ = (UBYTE)(257 - (r - s));
            *d++ = c;
            s = lit = r;
            if (s >= stop)
                break;
            c = *s++;
        }
    }
    while (lit < end) {
        k = end - lit > 128 ? 128 : end - lit;
        *d++ = (UBYTE)(k - 1);
        memcpy(d, lit, k);
        d += k, lit += k;
    }
    return d - start;
}

/* No PackBits of n bytes is shorter than this: every code covers 128 bytes at
 * most and takes two at least. A method that cannot beat the best so far is
 * not tried, and the tile comes out the same, only sooner. */
#define PB_MIN(n) ((((n) + 127) >> 7) << 1)

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
/* FRAME arg 8 or 16: tiles may come deflated as well (method bit 32), when that
 * is shorter. Level 1 or 2, as the client asked for this frame. */
static LONG frame_level;
static struct zpack *frame_z;
static struct zpack *z_new(LONG level, LONG osize);
static void z_free(struct zpack *z);
static LONG z_pack(struct zpack *z, const UBYTE *data, LONG n);
static UBYTE *zpack_out(struct zpack *z);
static LONG zpack_level(struct zpack *z);

static UBYTE *pack_tile(UBYTE *data, LONG *len, LONG *method)
{
    LONG n;

    if (!frame_level || *len < 64)
        return data;
    if (frame_z && zpack_level(frame_z) != frame_level) {
        z_free(frame_z);
        frame_z = NULL;
    }
    if (!frame_z && !(frame_z = z_new(frame_level, TILE * TILE + TILE * TILE / 8 + 256)))
        return data;
    n = z_pack(frame_z, data, *len);
    if (n + 8 >= *len)
        return data;
    *len = n;
    *method |= 32;
    return zpack_out(frame_z);
}

/* Every way we know for planar tile ttx, tty, the shortest wins, as
 * chunky_tile() does it. ext: row XOR too. */
static LONG planar_tile(LONG ttx, LONG tty, LONG bw, LONG th, BOOL ext, BOOL had, LONG dy,
                        LONG *pmethod, LONG *pb)
{
    LONG size, raw, best, method, k, b = 0;

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
    if (had && PB_MIN(raw) < best) {  /* the client has the old tile: send the difference */
        ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *o = (ULONG *)pold;
        tile_bytes(pref, ttx, tty, bw, th, pold);
        for (k = (raw + 3) >> 2; k > 0; k--)
            *a++ = *c++ ^ *o++;
        if ((size = packbits(pwork, raw, penc(1 - b))) < best)
            best = size, method = 2, b = 1 - b;
    }
    if (dy && PB_MIN(raw) + 2 < best) {  /* scrolled: the old rows, moved */
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
    *pmethod = method;
    *pb = b;
    return best;
}

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
    zz.shown = FALSE;  /* a chipset screen, not through the card: the next look */
    zz.scr = NULL;     /* through it takes a new pass */
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
        LONG t = ((UWORD *)porder)[i], best, method, p, b;
        LONG ttx = t % pcols, tty = t / pcols, px = ttx * TILE, py = tty * TILE;
        LONG tw = px + TILE < pw ? TILE : pw - px, th = py + TILE < ph ? TILE : ph - py;
        LONG bw = (tw + 7) >> 3;
        BOOL had = !force && (pvalid[t] & 1);
        UBYTE *data;

        best = planar_tile(ttx, tty, bw, th, ext, had, had && dy && shift_ok(ttx, tty, th, dy) ? dy : 0,
                           &method, &b);
        data = ext ? pack_tile(penc(b), &best, &method) : penc(b);
        be16(tile_buf, px);
        be16(tile_buf + 2, py);
        be16(tile_buf + 4, tw);
        be16(tile_buf + 6, th);
        tile_buf[8] = method;
        be32(tile_buf + 9, best);
        /* Out the door as soon as there is a packet's worth, and a look for
         * input between tiles: the pointer must not wait for the picture. */
        if (!put(tile_buf, 13) || !put(data, best) ||
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
 * has too many colours; *head gets 1 + k. A pixel like the one before costs a
 * compare; the rows go through idx_row1/2/4(), with the shifts fixed and the
 * lookup table in a register. */

/* n pixels of a row as 1 bit indices through slot, top bits first. */
static UBYTE *idx_row1(const UBYTE *p, LONG n, UBYTE *o, const UBYTE *slot)
{
    UBYTE a;

    for (; n >= 8; n -= 8) {
        a = slot[*p++];
        a = (a + a) | slot[*p++];
        a = (a + a) | slot[*p++];
        a = (a + a) | slot[*p++];
        a = (a + a) | slot[*p++];
        a = (a + a) | slot[*p++];
        a = (a + a) | slot[*p++];
        *o++ = (a + a) | slot[*p++];
    }
    if (n > 0) {
        LONG i;
        for (a = 0, i = 0; i < 8; i++)
            a = (a + a) | (i < n ? slot[p[i]] : 0);
        *o++ = a;
    }
    return o;
}

static UBYTE *idx_row2(const UBYTE *p, LONG n, UBYTE *o, const UBYTE *slot)
{
    UBYTE a;

    for (; n >= 4; n -= 4) {
        a = slot[*p++] << 2;
        a = (a | slot[*p++]) << 2;
        a = (a | slot[*p++]) << 2;
        *o++ = a | slot[*p++];
    }
    if (n > 0) {
        LONG i;
        for (a = 0, i = 0; i < 4; i++)
            a = a << 2 | (i < n ? slot[p[i]] : 0);
        *o++ = a;
    }
    return o;
}

static UBYTE *idx_row4(const UBYTE *p, LONG n, UBYTE *o, const UBYTE *slot)
{
    for (; n >= 2; n -= 2, p += 2)
        *o++ = slot[p[0]] << 4 | slot[p[1]];
    if (n > 0)
        *o++ = slot[p[0]] << 4;
    return o;
}

/* The colours of n pixels into col, in the order they come, k known so far,
 * through slot; the new k, or 17 at the 17th. px is long aligned: four pixels
 * like the last one cost one compare. */
#define IDX_PIXEL(v)                            \
    if ((c = (v)) != last && slot[last = c] == 0xFF) { \
        if (k == 16)                            \
            return 17;                          \
        slot[c] = (UBYTE)k;                     \
        col[k++] = c;                           \
    }

static LONG idx_colours(const UBYTE *px, LONG n, UBYTE *col, LONG k, UBYTE *slot)
{
    const ULONG *q = (const ULONG *)px, *qend = q + (n >> 2);
    const UBYTE *p, *end;
    UBYTE last = col[k - 1], c;
    ULONG four = last;

    four |= four << 8;
    four |= four << 16;
    for (;;) {
        while (q < qend && *q == four)
            q++;
        if (q == qend)
            break;
        p = (const UBYTE *)q++;
        IDX_PIXEL(p[0])
        IDX_PIXEL(p[1])
        IDX_PIXEL(p[2])
        IDX_PIXEL(p[3])
        four = last;
        four |= four << 8;
        four |= four << 16;
    }
    for (p = (const UBYTE *)q, end = px + n; p < end; p++)  /* the odd few at the end */
        IDX_PIXEL(*p)
    return k;
}

static LONG index_tile(const UBYTE *px, LONG tw, LONG th, UBYTE *dst, LONG *head)
{
    UBYTE *rows, *col = dst + 1;
    LONG k, i, y, rb;

    col[0] = *px;
    islot[*px] = 0;
    if ((k = idx_colours(px, tw * th, col, 1, islot)) > 16) {
        for (i = 0; i < 16; i++)
            islot[col[i]] = 0xFF;
        return 0;
    }
    dst[0] = (UBYTE)k;
    *head = 1 + k;
    rows = dst + *head;
    if (k == 1) {  /* one colour: nothing but zeros */
        rb = (tw + 7) >> 3;
        memset(rows, 0, rb * th);
    } else if (k == 2) {
        rb = (tw + 7) >> 3;
        for (y = 0; y < th; y++, px += tw)
            rows = idx_row1(px, tw, rows, islot);
    } else if (k <= 4) {
        rb = (tw + 3) >> 2;
        for (y = 0; y < th; y++, px += tw)
            rows = idx_row2(px, tw, rows, islot);
    } else {
        rb = (tw + 1) >> 1;
        for (y = 0; y < th; y++, px += tw)
            rows = idx_row4(px, tw, rows, islot);
    }
    for (i = 0; i < k; i++)
        islot[col[i]] = 0xFF;
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

/* Every way we know for the tile at px, py, the shortest wins: its size, and
 * in *pmethod and *pb how it went and which penc() holds it. had: the client
 * has the old tile in ref; dy: it scrolled by that much, 0 for not. A way that
 * cannot beat the best so far (PB_MIN) is not tried. */
static LONG chunky_tile(LONG px, LONG py, LONG tw, LONG th, BOOL had, LONG dy, LONG *pmethod, LONG *pb)
{
    LONG raw, best, size, method, k, b = 0, ihead, irows;

    raw = chunky_bytes(cur, px, py, tw, th, ptile);
    if ((irows = index_tile(ptile, tw, th, pold, &ihead))) {
        /* A few colours: indices win nine times out of ten, so the full
         * bytes don't even get a PackBits pass. Saves the slow CPUs half. */
        UBYTE *o = penc(0);
        memcpy(o, pold, ihead);
        best = ihead + packbits(pold + ihead, irows, o + ihead);
        method = 1 | 16;
        if (ihead + PB_MIN(irows) < best) {
            o = penc(1 - b);
            memcpy(o, pold, ihead);
            xor_rows(pold + ihead, pwork, irows / th, th);
            if ((size = ihead + packbits(pwork, irows, o + ihead)) < best)
                best = size, method = 1 | 4 | 16, b = 1 - b;
        }
    } else {
        best = packbits(ptile, raw, penc(0));
        method = 1;
        if (best > raw / 4) {  /* as for planar: only where there is room to win */
            xor_rows(ptile, pwork, tw, th);
            if ((size = packbits(pwork, raw, penc(1 - b))) < best)
                best = size, method = 1 | 4, b = 1 - b;
        }
    }
    if (had && PB_MIN(raw) < best) {  /* the client has the old tile */
        ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *o = (ULONG *)pold;
        chunky_bytes(ref, px, py, tw, th, pold);
        for (k = (raw + 3) >> 2; k > 0; k--)
            *a++ = *c++ ^ *o++;
        if ((size = packbits(pwork, raw, penc(1 - b))) < best)
            best = size, method = 2, b = 1 - b;
    }
    if (dy && PB_MIN(raw) + 2 < best) {  /* scrolled */
        ULONG *a = (ULONG *)pwork, *c = (ULONG *)ptile, *ol = (ULONG *)pold;
        UBYTE *o = pold;
        LONG yy;
        for (yy = 0; yy < th; yy++, o += tw) {
            LONG sy = py + yy + dy;
            if (sy < 0 || sy >= ref_h)
                memset(o, 0, tw);
            else
                memcpy(o, ref + sy * ref_w + px, tw);
        }
        for (k = (raw + 3) >> 2; k > 0; k--)
            *a++ = *c++ ^ *ol++;
        if ((size = packbits(pwork, raw, penc(1 - b) + 2) + 2) < best) {
            be16(penc(1 - b), (UWORD)dy);
            best = size, method = 2 | 8, b = 1 - b;
        }
    }
    if (best >= raw) {
        memcpy(penc(b), ptile, raw);
        best = raw, method = 0;
    }
    *pmethod = method;
    *pb = b;
    return best;
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
        LONG best, method, b;
        BOOL had = !force && (valid[t] & 1);
        UBYTE *data;

        best = chunky_tile(px, py, tw, th, had, had && dy && chunky_shift_ok(ttx, py, th, dy) ? dy : 0,
                           &method, &b);
        data = pack_tile(penc(b), &best, &method);
        be16(tile_buf, px);
        be16(tile_buf + 2, py);
        be16(tile_buf + 4, tw);
        be16(tile_buf + 6, th);
        tile_buf[8] = method;
        be32(tile_buf + 9, best);
        if (!put(tile_buf, 13) || !put(data, best) ||
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

/* Whether vp is one of the view's viewports and not hidden; under LockIBase. */
static BOOL in_view(struct View *v, struct ViewPort *vp)
{
    struct ViewPort *p;
    WORD n = 0;

    for (p = v ? v->ViewPort : NULL; p && n < 256; p = p->Next, n++)
        if (p == vp)
            return !(vp->Modes & VP_HIDE);
    return FALSE;
}

static BOOL screens(void)
{
    struct text t = {0};
    struct Screen *s;
    struct View *shown;
    ULONG lock;
    UBYTE b[16] = {0};
    UWORD count = 0, all;
    BOOL ok, chip = FALSE;

    t_add(&t, b, 2);  /* count, filled in below */
    lock = LockIBase(0);
    /* What the chipset puts out is GfxBase->ActiView. Intuition's own is ViewLord;
     * anything else is a program's LoadView. A copper list poked straight into
     * the hardware leaves ActiView alone and can't be seen from here. */
    shown = GfxBase->ActiView;
    all = 0x8000;  /* these display bits are filled in */
    if (shown != &IntuitionBase->ViewLord) all |= 0x4000;
    for (s = IntuitionBase->FirstScreen; s; s = s->NextScreen) {
        const UBYTE *title = screen_title(s);
        UWORD flags = all, n = title ? strlen((const char *)title) : 0;
        BOOL rtg = is_rtg(s);
        if (s == IntuitionBase->FirstScreen) flags |= 1;
        if ((s->Flags & SCREENTYPE) == WBENCHSCREEN) flags |= 2;
        if (rtg) flags |= 4;
        if (GetVPModeID(&s->ViewPort) & HAM_KEY) flags |= 8;
        if (!rtg && in_view(shown, &s->ViewPort)) {
            flags |= 32;  /* its ViewPort is in the view on display */
            if (!chip) flags |= 16;  /* the frontmost of those: what the chipset shows */
            chip = TRUE;
        }
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
static void AMIGENT_SAVEDS front_main(void)
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

/* ---- what holds input back: menus and popups ------------------------- */

#define PROBE_MS 400  /* a helper that takes longer counts as held up */
static BYTE probe_sig = -1;  /* a probe helper is done */
static volatile BOOL held_waiting;

/* Waits until *flag is clear, at most ms. */
static void wait_flag(volatile BOOL *flag, LONG ms)
{
    ULONG end = now_ms() + ms;
    LONG left;

    while (*flag && (left = (LONG)(end - now_ms())) > 0)
        wait_ms(left, 1UL << probe_sig);
}

static void helper_done(volatile BOOL *flag)
{
    Forbid();
    *flag = FALSE;
    children--;
    Signal(server, 1UL << probe_sig | 1UL << chan_gone);
}

/* A ScreenToFront of the screen in front changes nothing, and comes back at
 * once unless Intuition holds back new windows and screens. */
static void AMIGENT_SAVEDS held_main(void)
{
    struct Screen *s;

    Wait(SIGBREAKF_CTRL_F);
    if ((s = open_screen((ULONG)FindTask(NULL)->tc_UserData)))
        ScreenToFront(s);
    helper_done(&held_waiting);
}

static BOOL start_helper(volatile BOOL *flag, void (*entry)(void), const char *name, APTR data)
{
    struct Process *proc;

    if (probe_sig < 0)
        return FALSE;
    SetSignal(0, 1UL << probe_sig);
    Forbid();
    children++;
    *flag = TRUE;
    Permit();
    proc = CreateNewProcTags(NP_Entry, (ULONG)entry, NP_Name, (ULONG)name, NP_Priority, 0,
                             NP_StackSize, 4096, TAG_END);
    if (!proc) {
        Forbid();
        children--;
        *flag = FALSE;
        Permit();
        return FALSE;
    }
    proc->pr_Task.tc_UserData = data;
    Signal(&proc->pr_Task, SIGBREAKF_CTRL_F);
    return TRUE;
}

/* Is Intuition holding up input? 'm': a menu is open (or a window is being
 * dragged), which locks the screen's layers. 'h': new windows and screens
 * wait, as behind the popup a short right click leaves on a Directory Opus
 * desktop, without any lock to see. 0: neither. */
static char held(void)
{
    struct Screen *s;
    ULONG lock, handle = 0;
    LONG i;
    BOOL free_ = FALSE;

    for (i = 0; i < 3 && !free_; i++) {
        if (i)
            Delay(1);
        lock = LockIBase(0);
        s = IntuitionBase->FirstScreen;
        handle = (ULONG)s;
        if (!s || AttemptSemaphore(&s->LayerInfo.Lock)) {
            if (s)
                ReleaseSemaphore(&s->LayerInfo.Lock);
            free_ = TRUE;
        }
        UnlockIBase(lock);
    }
    if (!free_)
        return 'm';
    if (!held_waiting && handle) {
        if (!start_helper(&held_waiting, held_main, "amigentd probe", (APTR)handle))
            return 0;
        wait_flag(&held_waiting, PROBE_MS);
    }
    return held_waiting ? 'h' : 0;
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

/* The screen FRAME_RGB reads: a handle from SCREENS, set by SCREEN_FRONT with
 * arg 1; 0 for the front one. Ends with the connection. A screen that has
 * closed meanwhile gives the front one. */
static ULONG aim;

static BOOL frame_rgb(BOOL packed, LONG x, LONG y, LONG w, LONG h)
{
    struct Screen *scr;
    ULONG lock, len, n = 0;
    UBYTE head[8], *buf = NULL, *runs = NULL;
    BOOL ok;

    lock = LockIBase(0);
    for (scr = IntuitionBase->FirstScreen; aim && scr && (ULONG)scr != aim; scr = scr->NextScreen)
        ;
    if (!scr)
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

/* want: a screen handle from SCREENS, or 0 for the front screen; a handle that
 * isn't in the list any more gives the front screen (the S line tells which). */
static BOOL ui_tree(ULONG want, BOOL screen_only)
{
    static const char *const sys[] = {"", "size", "drag", "sdrag", "depth", "sdepth",
                                      "zoom", "", "close"};
    struct text t = {0};
    struct Screen *s;
    struct Layer *l;
    ULONG lock;
    char hold = held();

    lock = LockIBase(0);
    for (s = IntuitionBase->FirstScreen; want && s && (ULONG)s != want; s = s->NextScreen)
        ;
    if (!s)
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
    t_tab(&t);  /* from 1.3.0: m menu open, h new windows held back */
    if (hold)
        t_chr(&t, hold);
    t_tab(&t);  /* from 1.3.0: where Intuition has the pointer on this screen */
    t_num(&t, s->MouseX);
    t_tab(&t);
    t_num(&t, s->MouseY);
    t_chr(&t, '\n');
    /* Layers run front to back; the window list does not. */
    for (l = screen_only ? NULL : s->LayerInfo.top_layer; l; l = l->back) {
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
        mon_say('S', "screen link closed", NULL);
        /* Reset rather than close: whatever still sits in the send buffer would
         * otherwise hold up the next client on a slow link. */
        struct linger now = {1, 0};
        setsockopt(client, SOL_SOCKET, SO_LINGER, &now, sizeof(now));
        CloseSocket(client);
        client = -1;
    }
    out_len = 0;
    in_have = nacks = 0;
    aim = 0;
    set_buttons(0);
    ObtainSemaphore(&picture);
    if (want)
        memset(want, 0, cols * rows);
    wanted = 0;
    ReleaseSemaphore(&picture);
}

/* Nobody looks at the screen any more: both pictures and the frame packer go
 * back to free memory, so an idle amigentd costs a small Amiga next to
 * nothing. The next frame starts from scratch, as after a new screen size,
 * and CLIENT answers 1. The reader only touches the classic picture, under
 * the semaphore, and with nothing wanted it doesn't fit a new one. */
static void free_pictures(void)
{
    ObtainSemaphore(&picture);
    free_frame();
    if (zz.fails < ZZ_GIVE_UP)  /* after a timeout the card may still write to its buffers */
        zz_free();  /* the ZZ9000 mirror too; the next look starts the card over */
    zz.reset = TRUE;
    zz.cur = NULL;  /* a new cur may get the old address */
    ReleaseSemaphore(&picture);
    free_planar();
    z_free(frame_z);
    frame_z = NULL;
    owner = powner = 0;
    cconn = pconn = 0;
}

/* The screen client went away of itself (closed, broke off or fell silent).
 * serve() may already be holding a newcomer that asked for the screen, and
 * the failure of its first command is one way this is reached. Freeing here
 * would drop the picture that newcomer still uses. With nobody waiting,
 * free_pictures() releases both pictures and, unless the ZZ9000 has been
 * given up, its mirror. */
static void lose_client(void)
{
    LONG i;

    drop_client();
    for (i = 0; i < PENDING; i++)
        if (pend[i].fd >= 0 && pend[i].screen)
            return;
    free_pictures();
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
    if (!key_matches(got, len)) {
        mon_say('E', "refused a wrong key", NULL);
        return refuse();
    }
    authed = TRUE;
    mon_say('S', "screen link up", NULL);
    return reply(0);
}

/* ---- links: small blocking transfers on any socket, from any process ---- */

struct zpack;

struct link {
    struct Library *base;  /* each process has its own bsdsocket.library */
    LONG fd;
    struct zpack *zp;  /* after COMPRESS: bulk replies go out packed */
};

static BOOL z_send(struct link *l, const UBYTE *data, ULONG len);
static BOOL z_send_file(struct link *l, BPTR fh, ULONG size, UBYTE *buf);

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
        net_out += n;
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
        net_in += n;
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

/* Waits as l_wait does, IO_SECONDS at most for the next bytes, and never past
 * deadline_ms: a sender that stops is dropped, one that trickles ends too. */
static BOOL l_wait_until(struct link *l, BOOL writing, ULONG deadline_ms)
{
    struct Library *SocketBase = l->base;
    struct timeval tv;
    ULONG sigs = SIGBREAKF_CTRL_C;
    fd_set s;
    LONG left;

    left = (LONG)(deadline_ms - now_ms());
    if (left <= 0)
        return FALSE;
    if (left > IO_SECONDS * 1000L)
        left = IO_SECONDS * 1000L;
    tv.tv_sec = left / 1000;
    tv.tv_usec = (left % 1000) * 1000;
    FD_ZERO(&s);
    FD_SET(l->fd, &s);
    if (WaitSelect(l->fd + 1, writing ? NULL : &s, writing ? &s : NULL, NULL, &tv, &sigs) > 0)
        return TRUE;
    if (sigs & SIGBREAKF_CTRL_C)
        SetSignal(SIGBREAKF_CTRL_C, SIGBREAKF_CTRL_C);
    return FALSE;
}

static BOOL l_recv_until(struct link *l, void *data, ULONG len, ULONG deadline_ms)
{
    struct Library *SocketBase = l->base;
    UBYTE *p = data;
    LONG n;

    while (len > 0) {
        if (!l_wait_until(l, FALSE, deadline_ms))
            return FALSE;
        n = recv(l->fd, p, len, 0);
        if (n < 0 && Errno() == EWOULDBLOCK)
            continue;
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
        net_in += n;
    }
    return TRUE;
}

static BOOL l_skip_until(struct link *l, ULONG len, ULONG deadline_ms)
{
    UBYTE scratch[512];

    while (len > 0) {
        ULONG n = len > sizeof(scratch) ? sizeof(scratch) : len;
        if (!l_recv_until(l, scratch, n, deadline_ms))
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

    fd = accept(listener, (struct sockaddr *)&addr, AMIGENT_SOCKLEN(&len));
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
    /* Operational TLSONLY closes remote plaintext whether or not the TLS
     * port is listening. SETUP is only the operator's SETUP argument. */
    locked = tls_only && !setup_mode;
    hello[7] = (secret_len ? 1 : 0) | (tls_ready ? 2 : 0) | (locked ? 4 : 0);
    l.base = SocketBase;
    l.fd = fd;
    /* Strangers get the hello, so they know where the door is, then out.
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
    mon_say('E', "refused a newcomer: wrong key", NULL);
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

    mon_say('E', "failed:", text);

    head[0] = status;
    be16(head + 1, n);
    return l_send(l, head, 3, IO_SECONDS) && l_send(l, text, n, IO_SECONDS);
}

/* The AmigaDOS text for an error code. */
static BOOL c_fault(struct link *l, LONG code)
{
    ULONG words[25];  /* even address for the 68000 */
    char *buf = (char *)words;
    LONG n, i;
    char num[12];

    if (!code)
        return c_fail(l, 1, "failed");
    /* The text is in the Amiga's language; " [code]" lets the PC tell which it was. */
    Fault(code, NULL, buf, sizeof(words) - 14);
    for (n = code, i = 0; n > 0 && i < 10; n /= 10)
        num[i++] = '0' + (char)(n % 10);
    for (n = 0; buf[n]; n++)
        ;
    buf[n++] = ' ';
    buf[n++] = '[';
    while (i > 0)
        buf[n++] = num[--i];
    buf[n++] = ']';
    buf[n] = 0;
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
    ok = l_send(l, head, 5, IO_SECONDS) &&
         (l->zp ? z_send(l, t->buf, t->len) : l_send(l, t->buf, t->len, IO_SECONDS));
    FreeVec(t->buf);
    return ok;
}

/* A name of len bytes from the request. NULL with *alive still set: no memory. */
/* ---- monitor: a line per request for amigent-monitor's window ---------- */

/* amigent-monitor opens this public port; each line goes there as a message
 * the monitor frees (no reply). Nobody listening costs one FindPort. */
#define MON_PORT "Amigent.monitor"
#define MON_TEXT 160

struct monmsg {
    struct Message m;
    UBYTE kind;  /* A agent, C command, I input, T typed, V screen, S link, E error */
    UBYTE pad;
    char text[2];
};

/* The monitor's task, so waiting for a text skips its window (its log would
 * match what amiga_wait_text looks for) and TEXT marks it. Refreshed by every
 * mon_say, the statistics among them once a second. */
static struct Task *volatile mon_task;
static volatile ULONG mon_quiet_until;  /* now_ms(): the monitor gets nothing before, see QUIET */

static void mon_say(UBYTE kind, const char *what, const char *arg)
{
    struct MsgPort *port;
    struct monmsg *m;
    LONG a = strlen(what), b = arg ? strlen(arg) : 0, i;

    if (kind != 'Q' && mon_quiet_until && (LONG)(mon_quiet_until - now_ms()) > 0)
        return;  /* a measurement runs: its window must not draw meanwhile */

    Forbid();
    port = FindPort(MON_PORT);
    mon_task = port ? port->mp_SigTask : NULL;
    Permit();
    if (!port)
        return;
    if (a + b > MON_TEXT)
        b = a > MON_TEXT ? 0 : MON_TEXT - a;
    if (!(m = AllocVec(sizeof(*m) + a + b + 1, MEMF_PUBLIC | MEMF_CLEAR)))
        return;
    m->m.mn_Node.ln_Type = NT_MESSAGE;
    m->m.mn_Length = sizeof(*m) + a + b + 1;
    m->kind = kind;
    memcpy(m->text, what, a);
    if (b) {
        m->text[a] = ' ';
        memcpy(m->text + a + 1, arg, b);
    }
    for (i = 0; m->text[i]; i++)
        if ((UBYTE)m->text[i] < 32 || (UBYTE)m->text[i] == 127)
            m->text[i] = ' ';
    Forbid();
    if ((port = FindPort(MON_PORT)))
        PutMsg(port, &m->m);
    Permit();
    if (!port)
        FreeVec(m);
}

/* x,y as text into out (at least 16 bytes). */
static void mon_xy(char *out, LONG x, LONG y)
{
    char *p = out;
    LONG v, i, d;

    for (i = 0; i < 2; i++) {
        v = i ? y : x;
        if (v < 0) {
            *p++ = '-';
            v = -v;
        }
        for (d = 10000; d > 1 && v < d; d /= 10)
            ;
        for (; d; d /= 10)
            *p++ = '0' + v / d % 10;
        if (!i)
            *p++ = ',';
    }
    *p = 0;
}

static void dec(char *p, ULONG v);

/* Once a second: bytes in and out so far, command links open, screen link
 * up; the monitor turns that into rates and keeps no log line for it. */
static void mon_stats(void)
{
    static ULONG last;
    struct channel *c;
    char buf[64];
    LONG n = 0;
    ULONG now = now_ms();

    if (now - last < 1000)
        return;
    last = now;
    for (c = chans; c < chans + CHANNELS; c++)
        if (c->proc)
            n++;
    dec(buf, net_in);
    strcat(buf, " ");
    dec(buf + strlen(buf), net_out);
    strcat(buf, " ");
    dec(buf + strlen(buf), n);
    strcat(buf, client >= 0 ? " 1" : " 0");
    mon_say('K', buf, NULL);
}

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
    mon_say('C', itself ? "stat" : "list", path);
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

/* from: the first byte to send (arg bit 1, from 1.3.0), for following a log. */
static BOOL c_read(struct link *l, UWORD len, ULONG from)
{
    struct FileInfoBlock *fib = AllocDosObject(DOS_FIB, NULL);
    UBYTE *buf = AllocVec(CHUNK, MEMF_ANY), head[5];
    BPTR fh = 0;
    LONG err = 0, n;
    ULONG size = 0;
    BOOL alive;
    char *path = c_name(l, len, &alive);

    if (alive && path)
        mon_say('C', "read", path);
    if (alive) {
        if (!path || !fib || !buf)
            err = ERROR_NO_FREE_STORE;
        else if (!(fh = Open(path, MODE_OLDFILE)) || !ExamineFH(fh, fib))
            err = IoErr();
        else {
            size = (ULONG)fib->fib_Size > from ? fib->fib_Size - from : 0;
            if (size)
                Seek(fh, from, OFFSET_BEGINNING);
            /* Some handlers open a read protected file and only refuse the
             * Read: try one before the size goes out, so that is an error
             * reply and not a dropped link. */
            if (size && Read(fh, buf, size > CHUNK ? CHUNK : size) <= 0)
                err = IoErr() ? IoErr() : ERROR_READ_PROTECTED;
            else
                Seek(fh, size ? from : 0, OFFSET_BEGINNING);
        }
        if (err)
            alive = c_fault(l, err);
        else {
            head[0] = 0;
            be32(head + 1, size);
            alive = l_send(l, head, 5, IO_SECONDS);
            if (alive && l->zp)
                alive = z_send_file(l, fh, size, buf), size = 0;
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

static struct zin *zi_new(void);
static void zi_free(struct zin *z);
static BOOL zi_into(struct link *l, ULONG size, BPTR fh, LONG *err);

/* The content of from into the existing file to, in place: for a file that
 * can't be deleted because someone holds a lock on it. */
static BOOL copy_over(const char *from, const char *to, UBYTE *buf)
{
    BPTR in = Open((STRPTR)from, MODE_OLDFILE), out = in ? Open((STRPTR)to, MODE_READWRITE) : 0;
    LONG n, total = 0;
    BOOL ok = in && out;

    while (ok && (n = Read(in, buf, CHUNK)) > 0) {
        ok = Write(out, buf, n) == n;
        total += n;
    }
    if (ok && n < 0)
        ok = FALSE;
    if (ok)
        ok = SetFileSize(out, total, OFFSET_BEGINNING) >= 0;
    if (out)
        Close(out);
    if (in)
        Close(in);
    return ok;
}

/* A new file is written under a temporary name next to it and renamed at the
 * end, so a broken transfer never leaves half a file. packed: the data are a
 * raw deflate stream of size bytes. */
static BOOL c_write(struct link *l, BOOL append, BOOL packed, UWORD len, ULONG size)
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
    if (path)
        mon_say('C', append ? "append" : "write", path);
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
    if (packed) {
        alive = zi_into(l, size, err ? 0 : fh, &err);
        size = 0;
    }
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
            LONG gone = DeleteFile(path) ? 0 : IoErr();  /* Rename doesn't replace */
            if (!Rename(tmp, path)) {
                err = IoErr();
                /* The old file can't go (someone keeps a lock on it), or comes
                 * right back (a handler or program that watches it): write the
                 * new content into it instead, and say why if that fails too. */
                if ((gone && gone != ERROR_OBJECT_NOT_FOUND) || err == ERROR_OBJECT_EXISTS) {
                    if (copy_over(tmp, path, buf)) {
                        err = 0;
                        DeleteFile(tmp);
                    } else if (gone && gone != ERROR_OBJECT_NOT_FOUND)
                        err = gone;
                }
            }
        }
        if (!alive || err)
            DeleteFile(tmp);
    }
    FreeVec(tmp);
    FreeVec(path);
    FreeVec(buf);
    if (!alive)
        return FALSE;
    if (err == -1)
        return c_fail(l, 1, "bad compressed data");
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
    mon_say('C', cmd == CMD_CHECKSUM ? "checksum" : cmd == CMD_DELETE ? "delete" : "makedir", path);
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
        if (from && to) {
            mon_say('C', "rename", from);
            mon_say('C', "    to", to);
        }
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

static ULONG pack_rate[3];
static volatile LONG pack_measured;  /* 1 while a channel measures, 2 done */
static BOOL text_on, debug_on;
static void measure_pack(void);
static void hooks_again(void);

/* key=name|name|... of every node on the lists, copied under Forbid: public
 * ports, or tasks ready and waiting (the caller itself is running). */
static void t_names(struct text *t, const char *key, struct List *a, struct List *b)
{
    UBYTE *buf = AllocVec(NAMES_MAX, MEMF_ANY);
    struct List *lists[2];
    struct Node *n;
    LONG used = 0, i, k;

    if (!buf)
        return;
    lists[0] = a;
    lists[1] = b;
    Forbid();
    for (i = 0; i < 2; i++)
        for (n = lists[i] ? lists[i]->lh_Head : NULL; n && n->ln_Succ; n = n->ln_Succ) {
            const UBYTE *name = (const UBYTE *)n->ln_Name;
            LONG len = 40;

            if (n->ln_Type == NT_PROCESS && ((struct Process *)n)->pr_CLI) {
                /* a Shell process is "Background CLI": the command it runs says more */
                struct CommandLineInterface *cli = BADDR(((struct Process *)n)->pr_CLI);
                const UBYTE *cmd = cli->cli_CommandName ? BADDR(cli->cli_CommandName) : NULL;

                if (cmd && cmd[0]) {
                    len = cmd[0];
                    name = cmd + 1;
                    for (k = len - 1; k >= 0; k--)
                        if (name[k] == ':' || name[k] == '/')
                            break;
                    name += k + 1;
                    len -= k + 1;
                }
            }
            if (!name || !*name || len <= 0)
                continue;
            for (k = 0; k < len && name[k] && k < 40 && used < NAMES_MAX - 2; k++)
                buf[used++] = name[k] == '|' || name[k] < ' ' ? ' ' : name[k];
            if (used >= NAMES_MAX - 2)
                break;
            buf[used++] = '|';
        }
    Permit();
    t_str(t, (const UBYTE *)key, FALSE);
    if (used)
        t_add(t, buf, used - 1);
    t_chr(t, '\n');
    FreeVec(buf);
}

static char self_path[256];

/* PROGDIR: and the program's name, as a path the Shell can run */
static void find_self(int argc, char **argv)
{
    BPTR dir = GetProgramDir();
    char name[108];
    const char *file = NULL;

    if (argc == 0) {
        struct WBStartup *wb = (struct WBStartup *)argv;
        if (wb && wb->sm_NumArgs > 0)
            file = (const char *)wb->sm_ArgList[0].wa_Name;
    } else if (GetProgramName(name, sizeof(name)))
        file = (const char *)FilePart(name);
    if (!dir || !file || !NameFromLock(dir, self_path, sizeof(self_path)) ||
        !AddPart(self_path, file, sizeof(self_path)))
        self_path[0] = 0;
}

static BOOL c_sysinfo(struct link *l)
{
    struct Library *SocketBase = l->base;
    struct text t = {0};
    LONG i;

    t_pair(&t, "chip_free", AvailMem(MEMF_CHIP));
    t_pair(&t, "chip_total", mem_total(MEMF_CHIP));
    t_pair(&t, "chip_largest", AvailMem(MEMF_CHIP | MEMF_LARGEST));
    t_pair(&t, "fast_free", AvailMem(MEMF_FAST));
    t_pair(&t, "fast_total", mem_total(MEMF_FAST));
    t_pair(&t, "fast_largest", AvailMem(MEMF_FAST | MEMF_LARGEST));
    t_pair(&t, "total_free", AvailMem(MEMF_ANY));
    t_pair(&t, "screen_paused", pause_left());  /* seconds left, see PAUSE */
    {
        /* Since the Amiga started, by the E clock, and since amigentd did: after
           a lost connection the client tells a restart from a network fault. */
        struct EClockVal ev;
        ULONG hz = ReadEClock(&ev);

        t_pair(&t, "uptime", hz ? (ULONG)((((unsigned long long)ev.ev_hi << 32) | ev.ev_lo) / hz) : 0);
        t_pair(&t, "daemon_uptime", (now_ms() - start_ms) / 1000);
    }
    if (SysBase->LastAlert[0] != 0xFFFFFFFFUL) {  /* the guru before the last reset */
        t_str(&t, (const UBYTE *)"last_alert=", FALSE);
        t_hex(&t, SysBase->LastAlert[0]);
        t_chr(&t, '\n');
        t_str(&t, (const UBYTE *)"last_alert_where=", FALSE);  /* protocol 6: task or address */
        t_hex(&t, SysBase->LastAlert[1]);
        t_chr(&t, '\n');
    }
    t_version(&t, "exec_version", SysBase->LibNode.lib_Version, SysBase->LibNode.lib_Revision);
    t_version(&t, "kickstart", SysBase->LibNode.lib_Version, SysBase->SoftVer);
    t_version(&t, "bsdsocket", SocketBase->lib_Version, SocketBase->lib_Revision);
    t_version(&t, "graphics", GfxBase->LibNode.lib_Version, GfxBase->LibNode.lib_Revision);
    {
        UWORD f = SysBase->AttnFlags;
#ifdef AMIGENT_APOLLO
        BOOL is_apollo = apollo_os_here();
        t_pair(&t, "cpu", (f & (1L << 10)) ? 68080 :
                          f & AFF_68060 ? 68060 : f & AFF_68040 ? 68040 : f & AFF_68030 ? 68030 :
                          f & AFF_68020 ? 68020 : f & AFF_68010 ? 68010 : 68000);
        t_pair(&t, "attn_flags", f);  /* raw, for bits newer than the NDK, such as a 68080's */
        t_str(&t, (const UBYTE *)"os=", FALSE);
        t_str(&t, (const UBYTE *)(is_apollo ? "ApolloOS" : "AmigaOS"), FALSE);
        t_chr(&t, '\n');
        if (is_apollo || (f & (1L << 10))) {
            UWORD board = apollo_board();

            t_str(&t, (const UBYTE *)"board=", FALSE);
            t_hex(&t, board);
            t_chr(&t, '\n');
            t_str(&t, (const UBYTE *)"board_name=", FALSE);
            t_str(&t, (const UBYTE *)apollo_board_name(board), FALSE);
            t_chr(&t, '\n');
            t_pair(&t, "audio", CMD_AUDIO);  /* opcode 49 on Apollo/68080 hardware */
        }
#else
        t_pair(&t, "cpu", f & AFF_68060 ? 68060 : f & AFF_68040 ? 68040 : f & AFF_68030 ? 68030 :
                          f & AFF_68020 ? 68020 : f & AFF_68010 ? 68010 : 68000);
        t_pair(&t, "attn_flags", f);  /* raw, for bits newer than the NDK, such as a 68080's */
#endif
    }
    /* From protocol 5: how fast this Amiga packs, in KB/s, so the PC can tell
     * whether packing beats the line; and what amigentd records. */
    Forbid();
    i = pack_measured;
    if (!i)
        pack_measured = 1;
    Permit();
    if (!i) {
        measure_pack();
        pack_measured = 2;
    }
    while (pack_measured != 2)
        Delay(5);
    t_pair(&t, "deflate_fast", pack_rate[0]);
    t_pair(&t, "deflate_best", pack_rate[1]);
    t_pair(&t, "inflate", pack_rate[2]);
    hooks_again();
    t_pair(&t, "text", text_on);
    t_pair(&t, "debug", debug_on);
    t_pair(&t, "note", 1);  /* NOTE is understood */
    t_pair(&t, "readfrom", 1);  /* READ takes a start offset */
    t_pair(&t, "loaded", 1);  /* protocol 6: LOADED, PEEK, TASKS, TASKCTL, TRACE */
#ifdef AMIGENT_APOLLO
    t_pair(&t, "trace", 0);  /* TRACE set refused on Apollo/68080 until validated */
#else
    t_pair(&t, "trace", 1);
#endif
    t_pair(&t, "poke", allow_poke);  /* POKE only with ALLOWPOKE */
    t_pair(&t, "probe", 1);  /* LOCKS, CPU, DRAG, QUIET */
    t_pair(&t, "emu68", emu68_present());  /* EMU68 is understood; 1: Emu68 runs here */
    t_str(&t, (const UBYTE *)"execbase=", FALSE);
    t_hex(&t, (ULONG)SysBase);
    t_chr(&t, '\n');
    if (self_path[0]) {  /* where this amigentd lives, for "amigentd FOLLOW" in jobs */
        t_str(&t, (const UBYTE *)"self=", FALSE);
        t_str(&t, (const UBYTE *)self_path, FALSE);
        t_chr(&t, '\n');
    }
    t_pair(&t, "zz9000", zz.shown);  /* the last look went through a ZZ9000's encoder */
    t_names(&t, "ports=", &SysBase->PortList, NULL);  /* for the PC to spot other agents */
    t_names(&t, "tasks=", &SysBase->TaskReady, &SysBase->TaskWait);
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

static void AMIGENT_SAVEDS runner_main(void)
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
        if (alive && l->zp)
            alive = z_send_file(l, fh, size, buf), size = 0;
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
    mon_say('C', "exec", r->line);
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

/* ---- deflate (RFC 1951) ------------------------------------------------- */

/* Packing with the fixed Huffman codes: a block that would not get shorter
 * goes out stored. Level 1 takes the one candidate the hash remembers, level 2
 * walks a chain and looks one byte ahead. Positions count from the start of
 * the context, so a new stream only moves the floor: nothing to clear. */
#define Z_WIN 32768L
#define Z_BUF (2 * Z_WIN)
#define Z_HASH 32768L
#define Z_CHAIN 32
#define Z_NICE 128

struct zpack {
    UBYTE *buf;     /* the last Z_WIN bytes and what is being packed */
    LONG have;      /* bytes in buf */
    ULONG base;     /* position of buf[0] */
    ULONG floor;    /* this stream starts here; nothing before it may match */
    ULONG *head;    /* per hash the last position + 1 */
    ULONG *prev;    /* per position & (Z_WIN - 1) the one before, level 2 */
    ULONG bits;
    LONG nbits, level, stored;  /* stored: blocks in a row that did not pack */
    UBYTE *out;
    LONG olen;
};

static UWORD z_lcode[288];
static UBYTE z_llen[288], z_lsym[259], z_dcode[30];
static const UWORD z_lbase[29] = {3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
                                  35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258};
static const UBYTE z_lextra[29] = {0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
                                   3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0};
static const UWORD z_dbase[30] = {1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193,
                                  257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145,
                                  8193, 12289, 16385, 24577};
static const UBYTE z_dextra[30] = {0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
                                   7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13};
static BOOL z_ready;

static UWORD z_rev(UWORD code, LONG len)
{
    UWORD r = 0;

    while (len-- > 0) {
        r = r << 1 | (code & 1);
        code >>= 1;
    }
    return r;
}

/* The fixed codes, bit reversed: the stream is filled from the low bit up. */
static void z_tables(void)
{
    LONG i, l;

    Forbid();
    if (!z_ready) {
        for (i = 0; i < 288; i++) {
            if (i < 144) z_lcode[i] = z_rev(0x30 + i, 8), z_llen[i] = 8;
            else if (i < 256) z_lcode[i] = z_rev(0x190 + i - 144, 9), z_llen[i] = 9;
            else if (i < 280) z_lcode[i] = z_rev(i - 256, 7), z_llen[i] = 7;
            else z_lcode[i] = z_rev(0xC0 + i - 280, 8), z_llen[i] = 8;
        }
        for (i = 0; i < 30; i++)
            z_dcode[i] = z_rev(i, 5);
        for (l = 3, i = 0; l <= 258; l++) {
            while (i < 28 && l >= z_lbase[i + 1])
                i++;
            z_lsym[l] = i;
        }
        z_ready = TRUE;
    }
    Permit();
}

static void z_free(struct zpack *z)
{
    if (!z)
        return;
    FreeVec(z->buf);
    FreeVec(z->head);
    FreeVec(z->prev);
    FreeVec(z->out);
    FreeVec(z);
}

/* out holds osize bytes; a block of n bytes needs n + n / 8 + 64. */
static struct zpack *z_new(LONG level, LONG osize)
{
    struct zpack *z = AllocVec(sizeof(*z), MEMF_CLEAR);

    z_tables();
    if (!z)
        return NULL;
    z->level = level;
    z->buf = AllocVec(Z_BUF, MEMF_ANY);
    z->head = AllocVec(Z_HASH * 4, MEMF_CLEAR);
    z->out = AllocVec(osize, MEMF_ANY);
    if (level > 1)
        z->prev = AllocVec(Z_WIN * 4, MEMF_CLEAR);
    if (!z->buf || !z->head || !z->out || (level > 1 && !z->prev)) {
        z_free(z);
        return NULL;
    }
    return z;
}

/* A new stream: what came before is out of reach for matches. */
static void z_start(struct zpack *z)
{
    z->floor = z->base + z->have;
    z->bits = 0;
    z->nbits = 0;
    z->olen = 0;
}

static void z_put(struct zpack *z, ULONG v, LONG n)
{
    z->bits |= v << z->nbits;
    z->nbits += n;
    while (z->nbits >= 8) {
        z->out[z->olen++] = (UBYTE)z->bits;
        z->bits >>= 8;
        z->nbits -= 8;
    }
}

static void z_byte_align(struct zpack *z)
{
    if (z->nbits > 0)
        z_put(z, 0, 8 - z->nbits);
}

#define Z_HASH_OF(p) ((((ULONG)(p)[0] << 10) ^ ((ULONG)(p)[1] << 5) ^ (p)[2]) & (Z_HASH - 1))

static void z_insert(struct zpack *z, LONG i)
{
    ULONG h = Z_HASH_OF(z->buf + i), pos = z->base + i;

    if (z->prev)
        z->prev[pos & (Z_WIN - 1)] = z->head[h];
    z->head[h] = pos + 1;
}

/* The longest match for buf[i] among earlier positions; *dist gets its distance. */
static LONG z_match(struct zpack *z, LONG i, LONG end, LONG *dist)
{
    ULONG pos = z->base + i, cand = z->head[Z_HASH_OF(z->buf + i)];
    ULONG lowest = pos > (ULONG)Z_WIN ? pos - Z_WIN : 0;
    LONG best = 0, chain = z->prev ? Z_CHAIN : 1, max = end - i, n;
    const UBYTE *a = z->buf + i, *b;

    if (lowest < z->floor) lowest = z->floor;
    if (lowest < z->base) lowest = z->base;
    if (max > 258) max = 258;
    while (cand && chain-- > 0) {
        ULONG c = cand - 1;
        if (c < lowest || c >= pos)
            break;
        b = z->buf + (c - z->base);
        if (b[best] == a[best] && b[0] == a[0]) {
            for (n = 0; n < max && a[n] == b[n]; n++)
                ;
            if (n > best) {
                best = n;
                *dist = pos - c;
                if (n >= max || n >= Z_NICE)
                    break;
            }
        }
        if (!z->prev)
            break;
        cand = z->prev[c & (Z_WIN - 1)];
    }
    return best >= 3 ? best : 0;
}

static void z_literal(struct zpack *z, LONG c)
{
    z_put(z, z_lcode[c], z_llen[c]);
}

static void z_copy(struct zpack *z, LONG len, LONG dist)
{
    LONG s = z_lsym[len], d = 0;

    z_put(z, z_lcode[257 + s], z_llen[257 + s]);
    if (z_lextra[s])
        z_put(z, len - z_lbase[s], z_lextra[s]);
    while (d < 29 && dist >= z_dbase[d + 1])
        d++;
    z_put(z, z_dcode[d], 5);
    if (z_dextra[d])
        z_put(z, dist - z_dbase[d], z_dextra[d]);
}

/* n bytes (at most Z_WIN) as one block onto out; last closes the stream. */
static void z_block(struct zpack *z, const UBYTE *data, LONG n, BOOL last)
{
    LONG i, start, end, olen = z->olen, nbits = z->nbits, len, dist, len2, dist2;
    ULONG bits = z->bits;
    BOOL try = z->stored < 2 || (z->stored & 7) == 0;

    if (z->have + n > Z_BUF) {  /* keep the last window, drop the rest */
        LONG keep = z->have > Z_WIN ? Z_WIN : z->have;
        memmove(z->buf, z->buf + z->have - keep, keep);
        z->base += z->have - keep;
        z->have = keep;
    }
    start = z->have;
    if (n)
        CopyMem((APTR)data, z->buf + start, n);
    z->have += n;
    end = start + n;
    if (try) {
        z_put(z, last ? 1 : 0, 1);
        z_put(z, 1, 2);  /* fixed codes */
        for (i = start; i < end;) {
            len = i + 2 < end ? z_match(z, i, end, &dist) : 0;
            if (len && z->level > 1 && len < Z_NICE && i + 3 < end) {
                z_insert(z, i);
                len2 = z_match(z, i + 1, end, &dist2);
                if (len2 > len) {  /* one later is better: a literal first */
                    z_literal(z, z->buf[i]);
                    i++;
                    len = len2, dist = dist2;
                }
            } else if (i + 2 < end)
                z_insert(z, i);
            if (len) {
                LONG k, stop = i + len;
                z_copy(z, len, dist);
                for (k = i + 1; k < stop && k + 2 < end; k++)
                    if (z->level > 1 || len <= 16)
                        z_insert(z, k);
                i = stop;
            } else {
                z_literal(z, z->buf[i]);
                i++;
            }
        }
        z_literal(z, 256);
    }
    /* Stored costs n + 4 bytes plus the header; take it when it is shorter. */
    if (!try || (z->olen - olen) * 8 + z->nbits - nbits > (n + 5) * 8 + 8) {
        z->olen = olen;
        z->bits = bits;
        z->nbits = nbits;
        z->stored++;
        if (!try)
            for (i = start; i + 2 < end; i += 4)  /* keep some of it findable */
                z_insert(z, i);
        z_put(z, last ? 1 : 0, 1);
        z_put(z, 0, 2);
        z_byte_align(z);
        z->out[z->olen++] = (UBYTE)n;
        z->out[z->olen++] = (UBYTE)(n >> 8);
        z->out[z->olen++] = (UBYTE)~n;
        z->out[z->olen++] = (UBYTE)(~n >> 8);
        CopyMem(z->buf + start, z->out + z->olen, n);
        z->olen += n;
    } else
        z->stored = 0;
    if (last)
        z_byte_align(z);
}

static UBYTE *zpack_out(struct zpack *z)
{
    return z->out;
}

static LONG zpack_level(struct zpack *z)
{
    return z->level;
}

/* One whole stream from data into z->out: for a tile. Returns its length. */
static LONG z_pack(struct zpack *z, const UBYTE *data, LONG n)
{
    z_start(z);
    z_block(z, data, n, TRUE);
    return z->olen;
}

/* ---- inflate ------------------------------------------------------------ */

/* Unpacks a raw deflate stream that comes from a socket (or memory, for the
 * speed test) into a file. Decoding goes code length by code length, as the
 * RFC lays out canonical codes. */
struct huff {
    UWORD count[16];
    UWORD symbol[320];
};

struct zin {
    struct link *l;     /* source, or mem */
    const UBYTE *mem;
    ULONG left;         /* compressed bytes still to come */
    UBYTE *ibuf;
    LONG ipos, ilen;
    ULONG bits;
    LONG nbits;
    BOOL bad, dead;     /* bad data; dead link */
    UBYTE *win;         /* 64 KB ring, written out by halves */
    ULONG wpos, flushed;
    BPTR fh;            /* 0: thrown away */
    LONG err;
    struct huff lit, dist;
};

#define ZIN_BUF 4096

static LONG zi_byte(struct zin *z)
{
    if (z->mem) {
        if (!z->left) {
            z->bad = TRUE;
            return 0;
        }
        z->left--;
        return *z->mem++;
    }
    if (z->ipos == z->ilen) {
        LONG n = z->left > ZIN_BUF ? ZIN_BUF : z->left;
        if (!n || z->dead) {
            z->bad = TRUE;
            return 0;
        }
        if (!l_recv(z->l, z->ibuf, n, IO_SECONDS)) {
            z->dead = z->bad = TRUE;
            return 0;
        }
        z->left -= n;
        z->ipos = 0;
        z->ilen = n;
    }
    return z->ibuf[z->ipos++];
}

static ULONG zi_bits(struct zin *z, LONG n)
{
    ULONG v;

    while (z->nbits < n) {
        z->bits |= (ULONG)zi_byte(z) << z->nbits;
        z->nbits += 8;
    }
    v = z->bits & ((1UL << n) - 1);
    z->bits >>= n;
    z->nbits -= n;
    return v;
}

static void zi_flush(struct zin *z, ULONG upto)
{
    while (z->flushed < upto) {
        ULONG at = z->flushed & 0xFFFF, n = upto - z->flushed;
        if (n > 0x10000 - at)
            n = 0x10000 - at;
        if (z->fh && !z->err && Write(z->fh, z->win + at, n) != (LONG)n)
            z->err = IoErr() ? IoErr() : ERROR_DISK_FULL;
        z->flushed += n;
    }
}

static void zi_out(struct zin *z, UBYTE c)
{
    z->win[z->wpos & 0xFFFF] = c;
    if ((++z->wpos & 0x7FFF) == 0)
        zi_flush(z, z->wpos);
}

/* Canonical code from lengths; FALSE for a set that can't be a code. */
static BOOL zi_build(struct huff *h, const UBYTE *len, LONG n)
{
    UWORD offs[16];
    LONG i, left = 1;

    memset(h->count, 0, sizeof(h->count));
    for (i = 0; i < n; i++)
        h->count[len[i]]++;
    h->count[0] = 0;
    for (i = 1; i < 16; i++) {
        left = (left << 1) - h->count[i];
        if (left < 0)
            return FALSE;
    }
    offs[1] = 0;
    for (i = 1; i < 15; i++)
        offs[i + 1] = offs[i] + h->count[i];
    for (i = 0; i < n; i++)
        if (len[i])
            h->symbol[offs[len[i]]++] = i;
    return TRUE;
}

static LONG zi_decode(struct zin *z, struct huff *h)
{
    LONG code = 0, first = 0, index = 0, len, count;

    for (len = 1; len < 16; len++) {
        code |= zi_bits(z, 1);
        count = h->count[len];
        if (code - first < count)
            return h->symbol[index + code - first];
        index += count;
        first = (first + count) << 1;
        code <<= 1;
        if (z->bad)
            break;
    }
    z->bad = TRUE;
    return 256;
}

static void zi_codes(struct zin *z)
{
    LONG sym, len, dist;

    while (!z->bad) {
        sym = zi_decode(z, &z->lit);
        if (sym < 256)
            zi_out(z, sym);
        else if (sym == 256)
            return;
        else {
            sym -= 257;
            if (sym >= 29) {
                z->bad = TRUE;
                return;
            }
            len = z_lbase[sym] + zi_bits(z, z_lextra[sym]);
            sym = zi_decode(z, &z->dist);
            if (sym >= 30) {
                z->bad = TRUE;
                return;
            }
            dist = z_dbase[sym] + zi_bits(z, z_dextra[sym]);
            if ((ULONG)dist > z->wpos || dist > Z_WIN) {
                z->bad = TRUE;
                return;
            }
            while (len-- > 0)
                zi_out(z, z->win[(z->wpos - dist) & 0xFFFF]);
        }
    }
}

static void zi_fixed(struct zin *z)
{
    UBYTE len[320];
    LONG i;

    for (i = 0; i < 288; i++)
        len[i] = i < 144 ? 8 : i < 256 ? 9 : i < 280 ? 7 : 8;
    zi_build(&z->lit, len, 288);
    for (i = 0; i < 30; i++)
        len[i] = 5;
    zi_build(&z->dist, len, 30);
}

static void zi_dynamic(struct zin *z)
{
    static const UBYTE order[19] = {16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15};
    UBYTE len[320];
    LONG nlen = zi_bits(z, 5) + 257, ndist = zi_bits(z, 5) + 1, ncode = zi_bits(z, 4) + 4, i, sym, rep;

    if (nlen > 286 || ndist > 30) {
        z->bad = TRUE;
        return;
    }
    memset(len, 0, 19);
    for (i = 0; i < ncode; i++)
        len[order[i]] = zi_bits(z, 3);
    if (!zi_build(&z->lit, len, 19)) {
        z->bad = TRUE;
        return;
    }
    for (i = 0; i < nlen + ndist && !z->bad;) {
        sym = zi_decode(z, &z->lit);
        if (sym < 16)
            len[i++] = sym;
        else {
            UBYTE v = 0;
            if (sym == 16) {
                if (!i) {
                    z->bad = TRUE;
                    return;
                }
                v = len[i - 1];
                rep = 3 + zi_bits(z, 2);
            } else if (sym == 17)
                rep = 3 + zi_bits(z, 3);
            else
                rep = 11 + zi_bits(z, 7);
            if (i + rep > nlen + ndist) {
                z->bad = TRUE;
                return;
            }
            while (rep-- > 0)
                len[i++] = v;
        }
    }
    if (z->bad || !zi_build(&z->lit, len, nlen) || !zi_build(&z->dist, len + nlen, ndist))
        z->bad = TRUE;
}

/* The whole stream of size bytes. All of it is taken from the link, also after
 * an error, so the next request lines up. FALSE when the link broke. */
static BOOL zi_run(struct zin *z)
{
    LONG last, type, n;

    do {
        last = zi_bits(z, 1);
        type = zi_bits(z, 2);
        if (type == 0) {  /* stored */
            z->bits = 0;
            z->nbits = 0;
            n = zi_byte(z);
            n |= zi_byte(z) << 8;
            zi_byte(z);
            zi_byte(z);
            while (n-- > 0 && !z->bad)
                zi_out(z, zi_byte(z));
        } else if (type == 1) {
            zi_fixed(z);
            zi_codes(z);
        } else if (type == 2) {
            zi_dynamic(z);
            zi_codes(z);
        } else
            z->bad = TRUE;
    } while (!last && !z->bad);
    zi_flush(z, z->wpos);
    if (!z->mem && !z->dead && z->left)
        z->dead = !l_skip(z->l, z->left, IO_SECONDS);
    return !z->dead;
}

static struct zin *zi_new(void)
{
    struct zin *z = AllocVec(sizeof(*z), MEMF_CLEAR);

    if (z && (!(z->win = AllocVec(0x10000, MEMF_ANY)) || !(z->ibuf = AllocVec(ZIN_BUF, MEMF_ANY)))) {
        FreeVec(z->win);
        FreeVec(z);
        z = NULL;
    }
    return z;
}

static void zi_free(struct zin *z)
{
    if (z) {
        FreeVec(z->win);
        FreeVec(z->ibuf);
        FreeVec(z);
    }
}

/* WRITE with packed data: inflate size bytes from the link into fh (0: only
 * take them). *err gets -1 for bad data, or the DOS error. */
static BOOL zi_into(struct link *l, ULONG size, BPTR fh, LONG *err)
{
    struct zin *z = zi_new();
    BOOL alive;

    if (!z) {
        *err = ERROR_NO_FREE_STORE;
        return l_skip(l, size, IO_SECONDS);
    }
    z->l = l;
    z->left = size;
    z->fh = fh;
    alive = zi_run(z);
    if (alive && !*err)
        *err = z->bad ? -1 : z->err;
    zi_free(z);
    return alive;
}

/* ---- how fast this Amiga packs ------------------------------------------ */

/* KB/s for deflate level 1, level 2 and inflate (pack_rate), measured once on
 * text-like data: the PC packs only when that beats the line. */

static ULONG eclock_us(struct EClockVal *a)
{
    struct EClockVal b;
    ULONG hz = ReadEClock(&b);
    unsigned long long t = (((unsigned long long)b.ev_hi << 32) | b.ev_lo) -
                           (((unsigned long long)a->ev_hi << 32) | a->ev_lo);
    return hz ? (ULONG)(t * 1000000ULL / hz) : 0;
}

static void measure_pack(void)
{
    static const char *const words[] = {"Workbench", "  ", "Copy", "ALL", "QUIET", "SYS:", "Libs/",
                                        "\n", "amigent", "1994", "dir", " ", "the", "and", "Disk",
                                        ".info", "0", "12", "LIBS:", "devs", "  ", "\t", "rwed"};
    UBYTE *sample = AllocVec(8192, MEMF_ANY);
    struct zpack *z = NULL;
    struct zin *zi = NULL;
    struct EClockVal start;
    ULONG seed = 12345, us, reps, done, packed = 0;
    LONG i, n, level;

    if (!sample)
        return;
    for (n = 0; n < 8192;) {
        const char *w;
        seed = seed * 1103515245UL + 12345;
        w = words[(seed >> 16) % (sizeof(words) / sizeof(words[0]))];
        for (; *w && n < 8192; w++)
            sample[n++] = *w;
    }
    for (level = 1; level <= 2; level++) {
        if (!(z = z_new(level, 8192 + 2048)))
            break;
        reps = 1;
        do {  /* until it took long enough to tell */
            ReadEClock(&start);
            for (i = 0; i < reps; i++)
                z_pack(z, sample, 8192);
            us = eclock_us(&start);
            done = reps;
            reps *= 4;
        } while (us < 50000 && reps < 4096);
        pack_rate[level - 1] = us ? (ULONG)((unsigned long long)done * 8 * 1000000ULL / us) : 999999;
        if (level == 2 && (zi = zi_new())) {
            packed = z->olen;
            reps = 1;
            do {
                ReadEClock(&start);
                for (i = 0; i < reps; i++) {
                    zi->mem = z->out;
                    zi->left = packed;
                    zi->ipos = zi->ilen = zi->nbits = 0;
                    zi->bits = zi->wpos = zi->flushed = 0;
                    zi->bad = FALSE;
                    zi_run(zi);
                }
                us = eclock_us(&start);
                done = reps;
                reps *= 4;
            } while (us < 50000 && reps < 4096);
            pack_rate[2] = !zi->bad && us ? (ULONG)((unsigned long long)done * 8 * 1000000ULL / us) : 0;
            zi_free(zi);
        }
        z_free(z);
    }
    FreeVec(sample);
}

/* ---- packed replies on a command channel --------------------------------- */

/* After COMPRESS, bulk replies come as frames of one deflate stream: u32
 * length and data, ended by a frame of length 0. */
static BOOL z_frame(struct link *l, struct zpack *z, const UBYTE *data, LONG n, BOOL last)
{
    UBYTE head[4];

    z_block(z, data, n, last);
    if (!z->olen)
        return TRUE;
    be32(head, z->olen);
    if (!l_send(l, head, 4, IO_SECONDS) || !l_send(l, z->out, z->olen, IO_SECONDS))
        return FALSE;
    z->olen = 0;
    return last ? l_send(l, "\0\0\0\0", 4, IO_SECONDS) : TRUE;
}

/* A buffer as packed frames. */
static BOOL z_send(struct link *l, const UBYTE *data, ULONG len)
{
    struct zpack *z = l->zp;
    ULONG n;

    z_start(z);
    do {
        n = len > (ULONG)CHUNK ? CHUNK : len;
        if (!z_frame(l, z, data, n, n == len))
            return FALSE;
        data += n;
        len -= n;
    } while (len > 0);
    return TRUE;
}

/* size bytes of an open file as packed frames; FALSE when the link broke. */
static BOOL z_send_file(struct link *l, BPTR fh, ULONG size, UBYTE *buf)
{
    struct zpack *z = l->zp;
    LONG n;

    z_start(z);
    do {
        n = size > (ULONG)CHUNK ? CHUNK : size;
        if (n && Read(fh, buf, n) != n)
            return FALSE;  /* the size is out already: only a broken link tells */
        if (!z_frame(l, z, buf, n, (ULONG)n == size))
            return FALSE;
        size -= n;
    } while (size > 0);
    return TRUE;
}

/* COMPRESS: 0 off, 1 fast, 2 best. */
static BOOL c_compress(struct link *l, LONG level)
{
    if (l->zp && (level == 0 || l->zp->level != level)) {
        z_free(l->zp);
        l->zp = NULL;
    }
    if (level > 2)
        level = 2;
    if (level && !l->zp && !(l->zp = z_new(level, CHUNK + CHUNK / 8 + 1024)))
        return c_fault(l, ERROR_NO_FREE_STORE);
    return c_ok(l);
}

/* ---- hooks: what programs draw, and their debug output -------------------- */

/* Each patched function gets a stub of its own in front, from src/native/amiga/daemon/hookstub.s:
 * it counts itself busy, calls our hook while one is set, and goes on to the
 * original with all registers as they came. Stubs are never freed, since a task
 * may have just stepped into one when the vector goes back; when a later patch
 * sits on top, the vector can't go back at all and the stub stays in the chain,
 * passing calls through. Either way the next amigentd finds them by name. */
static const UBYTE stub_code[60] = {
    0x48, 0xe7, 0xff, 0xfe, 0x4b, 0xfa, 0x00, 0x26, 0x52, 0xad, 0x00, 0x04, 0x2e, 0x15, 0x67, 0x0c,
    0x28, 0x47, 0x2f, 0x0f, 0x4e, 0x94, 0x58, 0x8f, 0x4b, 0xfa, 0x00, 0x12, 0x53, 0xad, 0x00, 0x04,
    0x4c, 0xdf, 0x7f, 0xff, 0x2f, 0x3a, 0x00, 0x0e, 0x4e, 0x75, 0x4e, 0x71, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0x41, 0x4d, 0x48, 0x54};
#define STUB_HOOK 44
#define STUB_BUSY 48
#define STUB_ORIG 52
#define STUB_SIZE 64

enum { H_TEXT, H_RECTFILL, H_ERASE, H_SETRAST, H_SCROLL, H_SCROLLBF, H_DELLAYER, H_RAWPUT,
       H_CLIPBLIT, H_BLTRP, H_FREEBM, HOOKS };
/* The number goes up with HOOKS: an older amigentd's set has fewer stubs. */
#define HOOKS_NAME "amigent hooks 2"

struct hookset {
    struct SignalSemaphore sem;  /* whoever holds it owns the hooks */
    UBYTE *stub[HOOKS];
    UBYTE in_chain[HOOKS];
    char name[20];
};

static struct hookset *hooks;
static BOOL hooks_owned, no_text, no_debug;
static struct Library *LayersBase;

static struct Library *hook_lib(LONG i)
{
    return i == H_RAWPUT ? (struct Library *)SysBase : i == H_DELLAYER ? LayersBase : (struct Library *)GfxBase;
}

static LONG hook_lvo(LONG i)
{
    static const WORD lvo[HOOKS] = {-60, -306, -810, -234, -396, -1002, -90, -516, -552, -606, -924};
    return lvo[i];
}

static void set_hook(LONG i, void (*fn)(ULONG *))
{
    struct Library *lib = hook_lib(i);
    UBYTE *s;

    if (!lib || ((i == H_SCROLLBF || i == H_FREEBM) && lib->lib_Version < 39))
        return;
    if (!(s = hooks->stub[i])) {
        if (!(s = AllocMem(STUB_SIZE, MEMF_PUBLIC)))
            return;
        CopyMem((APTR)stub_code, s, sizeof(stub_code));
        hooks->stub[i] = s;
        CacheClearU();
    }
    Disable();  /* RawPutChar is called from interrupts: no call may see half of it */
    *(ULONG *)(s + STUB_HOOK) = (ULONG)fn;
    if (!hooks->in_chain[i]) {
        /* The jump table holds JMP abs.l: the original is the address after it. */
        *(ULONG *)(s + STUB_ORIG) = *(ULONG *)((UBYTE *)lib + hook_lvo(i) + 2);
        SetFunction(lib, hook_lvo(i), (APTR)s);
        hooks->in_chain[i] = 1;
    }
    CacheClearU();
    Enable();
}

static void unhook_all(void)
{
    LONG i, k;
    BOOL busy;

    if (!hooks_owned)
        return;
    Disable();
    for (i = 0; i < HOOKS; i++) {
        UBYTE *s = hooks->stub[i];
        if (!s)
            continue;
        *(ULONG *)(s + STUB_HOOK) = 0;
        if (hooks->in_chain[i]) {
            struct Library *lib = hook_lib(i);
            APTR now = SetFunction(lib, hook_lvo(i), *(APTR *)(s + STUB_ORIG));
            if (now == (APTR)s)
                hooks->in_chain[i] = 0;
            else
                SetFunction(lib, hook_lvo(i), now);  /* someone patched on top: stay */
        }
    }
    CacheClearU();
    Enable();
    for (k = 0; k < 250; k++) {  /* nobody may be in our code when it goes */
        busy = FALSE;
        for (i = 0; i < HOOKS; i++)
            if (hooks->stub[i] && *(volatile ULONG *)(hooks->stub[i] + STUB_BUSY))
                busy = TRUE;
        if (!busy)
            break;
        Delay(1);
    }
    ReleaseSemaphore(&hooks->sem);
    hooks_owned = FALSE;
}

/* ---- text on the screen -------------------------------------------------- */

/* What Text() drew, per layer, in layer coordinates; RectFill and EraseRect
 * wipe it, ScrollRaster moves it, SetRast and DeleteLayer clear the layer.
 * Fixed width runs are kept to the character, so a Shell typing letter by
 * letter or deleting one comes out as one line. Only layers of windows count:
 * clocks and docks drawing into layers of their own would fill the store.
 *
 * Text drawn off-screen, into a rastport without a layer or with a layer of
 * its own (alone in its Layer_Info), is kept per bitmap in bitmap coordinates
 * (TXF_OFF, at most a quarter of the store). A ClipBlit or BltBitMapRastPort
 * from there into a window copies it along, so programs that draw into a
 * bitmap and copy it to their window against flicker can be read too. */
#define TX_LEN 40
#define TX_BUCKETS 64
#define TXF_OFF 1  /* layer is a struct BitMap: drawn off-screen */
#define TXF_NEW 2  /* just copied by a blit; tx_clear leaves it */
#define TX_OLD 64

struct tx {
    struct tx *next;
    APTR layer;       /* a window's layer, or an off-screen bitmap (TXF_OFF) */
    WORD x, y, w, h;  /* box in the layer; y is the top, not the baseline */
    UWORD cw;         /* character width of a fixed width font, 0 otherwise */
    UBYTE len, flags;
    UBYTE s[TX_LEN];
};

static struct tx *tx_pool, *tx_free, *tx_head[TX_BUCKETS];
static LONG tx_size, tx_off;
static ULONG tx_used, tx_evicted, tx_since, tx_gen;
static struct Window *tx_old[TX_OLD];  /* open before recording started */

#define TX_BUCKET(l) ((((ULONG)(l)) >> 6) & (TX_BUCKETS - 1))

static void tx_drop(struct tx **at)
{
    struct tx *e = *at;

    *at = e->next;
    e->next = tx_free;
    tx_free = e;
    tx_used--;
    if (e->flags & TXF_OFF)
        tx_off--;
}

static void tx_remove(struct tx *e)
{
    struct tx **at = &tx_head[TX_BUCKET(e->layer)];

    while (*at && *at != e)
        at = &(*at)->next;
    if (*at)
        tx_drop(at);
}

/* A free entry at the end of the layer's bucket; full, the oldest goes if
 * evict is set (not while a caller walks a bucket). Off-screen text has a
 * quarter of the store: beyond, older off-screen text makes room. */
static struct tx *tx_new(APTR ly, UBYTE flags, BOOL evict)
{
    struct tx **at, *e;
    LONG b = TX_BUCKET(ly), k;

    if ((flags & TXF_OFF) && tx_off >= tx_size / 4) {
        if (!evict)
            return NULL;
        for (k = 0; k < TX_BUCKETS; k++) {
            for (at = &tx_head[(b + k) & (TX_BUCKETS - 1)]; *at && !((*at)->flags & TXF_OFF);
                 at = &(*at)->next)
                ;
            if (*at) {
                tx_drop(at);
                break;
            }
        }
    }
    if (!tx_free) {
        if (!evict)
            return NULL;
        for (k = 0; k < TX_BUCKETS && !tx_head[(b + k) & (TX_BUCKETS - 1)]; k++)
            ;
        if (k == TX_BUCKETS)
            return NULL;
        tx_drop(&tx_head[(b + k) & (TX_BUCKETS - 1)]);
        tx_evicted++;
    }
    e = tx_free;
    tx_free = e->next;
    tx_used++;
    e->next = NULL;
    e->layer = ly;
    e->flags = flags;
    if (flags & TXF_OFF)
        tx_off++;
    for (at = &tx_head[b]; *at; at = &(*at)->next)
        ;
    *at = e;
    return e;
}

static BOOL tx_blank(const struct tx *e)
{
    LONG i;

    for (i = 0; i < e->len; i++)
        if (e->s[i] != ' ')
            return FALSE;
    return TRUE;
}

/* The fixed width run e loses characters from..to-1; what is right of them
 * becomes an entry of its own, unless it is blank. The caller drops e when
 * nothing is left of it. */
static void tx_cut(struct tx *e, LONG from, LONG to)
{
    struct tx *r;
    LONG i;

    for (i = to; i < e->len && e->s[i] == ' '; i++)
        ;
    if (i < e->len && (r = tx_new(e->layer, e->flags & TXF_OFF, FALSE))) {
        r->cw = e->cw;
        r->y = e->y;
        r->h = e->h;
        r->x = e->x + to * e->cw;
        r->len = e->len - to;
        r->w = r->len * e->cw;
        CopyMem(e->s + to, r->s, r->len);
    }
    e->len = from;
    e->w = from * e->cw;
}

/* Text within x0..x1, y0..y1 (inclusive, layer coordinates) is gone. A box
 * that covers less than half of a run's height leaves it: an underline. */
static void tx_clear(APTR ly, LONG x0, LONG y0, LONG x1, LONG y1)
{
    struct tx **at = &tx_head[TX_BUCKET(ly)], *e;

    while ((e = *at)) {
        LONG top, bottom;

        if (e->layer != ly || (e->flags & TXF_NEW)) {
            at = &e->next;
            continue;
        }
        if (!e->len) {
            tx_drop(at);
            continue;
        }
        top = y0 > e->y ? y0 : e->y;
        bottom = y1 < e->y + e->h - 1 ? y1 : e->y + e->h - 1;
        if (x1 < e->x || x0 >= e->x + e->w || (bottom - top + 1) * 2 < e->h) {
            at = &e->next;
            continue;
        }
        if (e->cw) {  /* the characters whose middle lies inside go */
            LONG half = e->cw / 2, a = x0 - e->x - half + e->cw - 1, b = x1 - e->x - half;
            a = a < 0 ? 0 : a / e->cw;
            b = b < 0 ? -1 : b / e->cw;
            if (b >= e->len)
                b = e->len - 1;
            if (a <= b)
                tx_cut(e, a, b + 1);
            if (e->len && !tx_blank(e)) {
                at = &e->next;
                continue;
            }
        }
        tx_drop(at);
    }
}

/* Fixed width runs that straddle column x are split there. */
static void tx_split(APTR ly, LONG x)
{
    struct tx *e;
    LONG k;

    for (e = tx_head[TX_BUCKET(ly)]; e; e = e->next)
        if (e->layer == ly && e->cw && e->x < x && x < e->x + e->w) {
            k = (x - e->x + e->cw / 2) / e->cw;
            if (k > 0 && k < e->len)
                tx_cut(e, k, k);  /* left keeps 0..k-1, the right part is new */
        }
}

static void tx_layer_gone(APTR ly)
{
    struct tx **at = &tx_head[TX_BUCKET(ly)];

    while (*at)
        if ((*at)->layer == ly)
            tx_drop(at);
        else
            at = &(*at)->next;
}

/* Where text drawn into rp is kept: a window's layer (layer coordinates),
 * or off-screen its bitmap (bitmap coordinates, *ox and *oy added to those of
 * rp, flags TXF_OFF). NULL: not kept, like a clock's layer on a screen. */
static APTR tx_where(struct RastPort *rp, LONG *ox, LONG *oy, UBYTE *flags)
{
    struct Layer *ly = rp->Layer;

    *ox = *oy = 0;
    *flags = 0;
    if (ly && ly->Window)
        return ly;
    if (ly) {
        if (ly->front || ly->back)
            return NULL;  /* one of several: a screen's, not a drawing buffer */
        *ox = ly->bounds.MinX - ly->Scroll_X;
        *oy = ly->bounds.MinY - ly->Scroll_Y;
    }
    if (!rp->BitMap)
        return NULL;
    *flags = TXF_OFF;
    return rp->BitMap;
}

/* regs: d0-d7 are r[0..7], a0-a6 r[8..14] */
/* Text s[n] drawn in rp with its font, left edge x, top y. */
static void tx_record(struct RastPort *rp, const UBYTE *s, LONG n, LONG x, LONG y)
{
    struct TextFont *f = rp->Font;
    LONG cw, h, i, k, total = 0, piece, ox, oy;
    WORD widths[(1024 + TX_LEN - 1) / TX_LEN];
    struct tx *e;
    UBYTE off;
    APTR ly;

    if (!f || n <= 0 || !tx_pool || !(ly = tx_where(rp, &ox, &oy, &off)))
        return;
    x += ox;
    y += oy;
    if (n > 1024)
        n = 1024;
    /* Fixed width fonts may carry kerning tables too, all alike: the flag decides. */
    cw = f->tf_Flags & FPF_PROPORTIONAL ? 0 : f->tf_XSize + rp->TxSpacing;
    if (cw < 0)
        cw = 0;
    for (i = 0, k = 0; i < n; i += TX_LEN, k++) {
        piece = n - i > TX_LEN ? TX_LEN : n - i;
        widths[k] = cw ? piece * cw : TextLength(rp, (STRPTR)s + i, piece);
        total += widths[k];
    }
    if (total <= 0)
        return;
    h = f->tf_YSize;
    Forbid();
    tx_gen++;
    tx_clear(ly, x, y, x + total - 1, y + h - 1);
    for (i = 0, k = 0; i < n; k++) {
        piece = n - i > TX_LEN ? TX_LEN : n - i;
        e = NULL;
        if (cw) {  /* goes on where a run of the same font ends: typing */
            for (e = tx_head[TX_BUCKET(ly)]; e; e = e->next)
                if (e->layer == ly && e->cw == cw && e->y == y && e->h == h &&
                    e->x + e->w == x && e->len < TX_LEN)
                    break;
            if (e && e->len + piece > TX_LEN)
                piece = TX_LEN - e->len;
        }
        if (!e && (e = tx_new(ly, off, TRUE))) {
            e->x = x;
            e->y = y;
            e->h = h;
            e->cw = cw;
            e->len = 0;
            e->w = 0;
        }
        if (e) {
            LONG j;
            for (j = 0; j < piece; j++) {
                UBYTE c = s[i + j];
                e->s[e->len + j] = c < 32 || (c >= 127 && c < 160) ? '?' : c;
            }
            e->len += piece;
            e->w += cw ? piece * cw : widths[k];
            if (tx_blank(e))
                tx_remove(e);
        }
        x += cw ? piece * cw : widths[k];
        i += piece;
    }
    Permit();
}

static void h_text(ULONG *r)
{
    struct RastPort *rp = (struct RastPort *)r[9];

    if (!rp->Font || (LONG)r[0] <= 0)
        return;
    tx_record(rp, (const UBYTE *)r[8], (LONG)r[0], rp->cp_x, rp->cp_y - rp->Font->tf_Baseline);
}

static void h_fill(ULONG *r, BOOL erase)
{
    struct RastPort *rp = (struct RastPort *)r[9];
    LONG ox, oy;
    UBYTE off;
    APTR ly;

    if (!tx_pool || (!erase && (rp->DrawMode & COMPLEMENT)) || !(ly = tx_where(rp, &ox, &oy, &off)))
        return;
    Forbid();
    tx_gen++;
    tx_clear(ly, (WORD)r[0] + ox, (WORD)r[1] + oy, (WORD)r[2] + ox, (WORD)r[3] + oy);
    Permit();
}

static void h_rectfill(ULONG *r)
{
    h_fill(r, FALSE);
}

static void h_erase(ULONG *r)
{
    h_fill(r, TRUE);
}

static void h_setrast(ULONG *r)
{
    struct RastPort *rp = (struct RastPort *)r[9];
    LONG ox, oy;
    UBYTE off;
    APTR ly;

    if (tx_pool && (ly = tx_where(rp, &ox, &oy, &off))) {
        Forbid();
        tx_gen++;
        tx_layer_gone(ly);
        Permit();
    }
}

static void h_scroll(ULONG *r)
{
    struct RastPort *rp = (struct RastPort *)r[9];
    LONG dx = (WORD)r[0], dy = (WORD)r[1], x0 = (WORD)r[2], y0 = (WORD)r[3];
    LONG x1 = (WORD)r[4], y1 = (WORD)r[5], ox, oy;
    struct tx **at, *e;
    UBYTE off;
    APTR ly;

    if (!tx_pool || !(ly = tx_where(rp, &ox, &oy, &off)))
        return;
    x0 += ox, x1 += ox, y0 += oy, y1 += oy;
    Forbid();
    tx_gen++;
    /* Cut runs where the area starts and ends, and where the part that
     * leaves it ends: what is shifted out goes, the rest moves. */
    tx_split(ly, x0);
    tx_split(ly, x1 + 1);
    if (dx > 0)
        tx_split(ly, x0 + dx);
    else if (dx < 0)
        tx_split(ly, x1 + 1 + dx);
    at = &tx_head[TX_BUCKET(ly)];
    while ((e = *at)) {
        if (e->layer != ly || e->x > x1 || e->x + e->w - 1 < x0 || e->y > y1 ||
            e->y + e->h - 1 < y0) {
            at = &e->next;
            continue;
        }
        if (e->x >= x0 && e->x + e->w - 1 <= x1 && e->y >= y0 && e->y + e->h - 1 <= y1) {
            e->x -= dx;
            e->y -= dy;
            if (e->x >= x0 && e->x + e->w - 1 <= x1 && e->y >= y0 && e->y + e->h - 1 <= y1) {
                at = &e->next;
                continue;
            }
        }
        tx_drop(at);  /* cut by the edge or scrolled out */
    }
    Permit();
}

static void h_dellayer(ULONG *r)
{
    struct Layer *ly = (struct Layer *)r[9];
    LONG i;

    if (!ly || !tx_pool)
        return;
    Forbid();
    tx_gen++;
    tx_layer_gone(ly);
    if (!ly->Window && !ly->front && !ly->back && ly->rp && ly->rp->BitMap)
        tx_layer_gone(ly->rp->BitMap);  /* a drawing buffer's layer: its text goes too */
    for (i = 0; i < TX_OLD; i++)
        if (ly->Window && tx_old[i] == (struct Window *)ly->Window)
            tx_old[i] = NULL;
    Permit();
}

/* A copy of the box sx,sy (w x h) of src to dx,dy in dst: the text there is
 * gone and what src holds in the box comes along. Only from an off-screen
 * source with text: blits of anything else (a font engine's glyphs, a picture,
 * a window scrolling itself) leave the text alone, as before. */
static void tx_blit(APTR src, LONG sx, LONG sy, APTR dst, UBYTE dflags, LONG dx, LONG dy,
                    LONG w, LONG h)
{
    struct tx *e, *c, **at;
    LONG n = 0, k, x1 = sx + w - 1, y1 = sy + h - 1, bs = TX_BUCKET(src), bd = TX_BUCKET(dst);
    BOOL known = FALSE;

    for (e = tx_head[bs]; e; e = e->next)
        if (e->layer == src) {
            known = TRUE;
            if (e->x <= x1 && e->x + e->w > sx && e->y <= y1 && e->y + e->h > sy)
                n++;
        }
    if (!known)
        return;
    tx_gen++;
    /* Room first: making it while copying could drop runs being copied. */
    while (tx_size - (LONG)tx_used < n) {
        for (k = 0; k < TX_BUCKETS; k++)
            if (k != bs && k != bd && tx_head[k])
                break;
        if (k == TX_BUCKETS)
            break;
        tx_drop(&tx_head[k]);
        tx_evicted++;
    }
    for (e = tx_head[bs]; e; e = e->next) {
        LONG a = 0, b = e->len - 1, top, bottom;

        if (e->layer != src || (e->flags & TXF_NEW) || !e->len)
            continue;
        top = sy > e->y ? sy : e->y;
        bottom = y1 < e->y + e->h - 1 ? y1 : e->y + e->h - 1;
        if (x1 < e->x || sx >= e->x + e->w || (bottom - top + 1) * 2 < e->h)
            continue;
        if (e->cw) {  /* the characters whose middle lies inside, as tx_clear */
            LONG half = e->cw / 2;
            a = sx - e->x - half + e->cw - 1;
            a = a < 0 ? 0 : a / e->cw;
            b = x1 - e->x - half;
            b = b < 0 ? -1 : b / e->cw;
            if (b >= e->len)
                b = e->len - 1;
            if (a > b)
                continue;
        } else if (e->x + e->w / 2 < sx || e->x + e->w / 2 > x1)
            continue;
        if (!(c = tx_new(dst, dflags | TXF_NEW, FALSE)))
            break;
        c->cw = e->cw;
        c->h = e->h;
        c->x = e->x + a * e->cw - sx + dx;
        c->y = e->y - sy + dy;
        c->len = b - a + 1;
        c->w = e->cw ? c->len * e->cw : e->w;
        CopyMem(e->s + a, c->s, c->len);
    }
    tx_clear(dst, dx, dy, dx + w - 1, dy + h - 1);  /* leaves the new ones */
    at = &tx_head[bd];
    while ((e = *at)) {
        if (e->layer == dst && (e->flags & TXF_NEW)) {
            e->flags &= ~TXF_NEW;
            if (tx_blank(e)) {
                tx_drop(at);
                continue;
            }
        }
        at = &e->next;
    }
}

/* ClipBlit (from a rastport) and BltBitMapRastPort (from a bitmap): a0 the
 * source, d0/d1 where in it, a1 the rastport, d2/d3 where, d4/d5 the size,
 * d6 the minterm. Only plain copies count. */
static void h_blit(ULONG *r, BOOL from_rp)
{
    struct RastPort *drp = (struct RastPort *)r[9];
    LONG w = (WORD)r[4], h = (WORD)r[5], sox = 0, soy = 0, dox, doy;
    UBYTE sflags, dflags;
    APTR src, dst;

    if (!tx_pool || (r[6] & 0xff) != 0xc0 || w <= 0 || h <= 0 || !r[8] ||
        !(dst = tx_where(drp, &dox, &doy, &dflags)))
        return;
    if (from_rp) {
        if (!(src = tx_where((struct RastPort *)r[8], &sox, &soy, &sflags)) || !(sflags & TXF_OFF))
            return;
    } else
        src = (APTR)r[8];
    if (src == dst)
        return;  /* a buffer scrolling itself, maybe for ScrollRaster: h_scroll did that */
    Forbid();
    tx_blit(src, (WORD)r[0] + sox, (WORD)r[1] + soy, dst, dflags, (WORD)r[2] + dox,
            (WORD)r[3] + doy, w, h);
    Permit();
}

static void h_clipblit(ULONG *r)
{
    h_blit(r, TRUE);
}

static void h_bltrp(ULONG *r)
{
    h_blit(r, FALSE);
}

static void h_freebm(ULONG *r)
{
    if (tx_pool && r[8]) {
        Forbid();
        tx_layer_gone((APTR)r[8]);
        Permit();
    }
}

/* ---- debug output: kprintf, Enforcer, MuForce ------------------------------ */

/* RawPutChar runs anywhere, interrupts and supervisor mode included: a byte
 * into the ring under Disable, nothing else. */
#define DBG_SIZE 65536L
static UBYTE *dbg_ring;
static volatile ULONG dbg_total;

static void h_rawput(ULONG *r)
{
    Disable();
    dbg_ring[dbg_total & (DBG_SIZE - 1)] = (UBYTE)r[0];
    dbg_total++;
    Enable();
}

static void hooks_start(void)
{
    struct Screen *s;
    struct Window *w;
    ULONG lock;
    LONG i, n = 0, size;

    Forbid();
    hooks = (struct hookset *)FindSemaphore((STRPTR)HOOKS_NAME);
    if (!hooks && (hooks = AllocMem(sizeof(*hooks), MEMF_PUBLIC | MEMF_CLEAR))) {
        strcpy(hooks->name, HOOKS_NAME);
        hooks->sem.ss_Link.ln_Name = hooks->name;
        AddSemaphore(&hooks->sem);  /* stays for good, like the stubs */
    }
    Permit();
    if (!hooks || hooks_owned || !AttemptSemaphore(&hooks->sem))
        return;  /* another amigentd on another port has them */
    hooks_owned = TRUE;
    if (!no_text) {
        size = AvailMem(MEMF_ANY) > 4000000 ? 4096 : AvailMem(MEMF_ANY) > 1000000 ? 1024 : 256;
        if ((tx_pool = AllocVec(size * sizeof(struct tx), MEMF_PUBLIC | MEMF_CLEAR))) {
            tx_size = size;
            for (i = 0; i < size; i++) {
                tx_pool[i].next = tx_free;
                tx_free = &tx_pool[i];
            }
            lock = LockIBase(0);
            for (s = IntuitionBase->FirstScreen; s; s = s->NextScreen)
                for (w = s->FirstWindow; w && n < TX_OLD; w = w->NextWindow)
                    tx_old[n++] = w;
            UnlockIBase(lock);
            tx_since = now_ms();
            set_hook(H_TEXT, h_text);
            set_hook(H_RECTFILL, h_rectfill);
            set_hook(H_ERASE, h_erase);
            set_hook(H_SETRAST, h_setrast);
            set_hook(H_SCROLL, h_scroll);
            set_hook(H_SCROLLBF, h_scroll);
            set_hook(H_DELLAYER, h_dellayer);
            set_hook(H_CLIPBLIT, h_clipblit);
            set_hook(H_BLTRP, h_bltrp);
            set_hook(H_FREEBM, h_freebm);
            text_on = TRUE;
        }
    }
    if (!no_debug && (dbg_ring = AllocVec(DBG_SIZE, MEMF_PUBLIC))) {
        set_hook(H_RAWPUT, h_rawput);
        debug_on = TRUE;
    }
}

static void hooks_stop(void)
{
    unhook_all();  /* waits until no task runs our hooks any more */
    FreeVec(tx_pool);
    tx_pool = NULL;
    FreeVec(dbg_ring);
    dbg_ring = NULL;
}

/* ---- TEXT: the front screen's text, row by row --------------------------- */

struct row {
    struct Window *win;
    LONG x, y, w, h, cw, len;
    UBYTE s[TX_LEN];
};

static int row_order(const void *a, const void *b)
{
    const struct row *p = a, *q = b;

    if (p->win != q->win)
        return 0;  /* kept in window order by the collector */
    if (p->y != q->y)
        return p->y < q->y ? -1 : 1;
    if (p->h != q->h)
        return p->h < q->h ? -1 : 1;
    return p->x < q->x ? -1 : p->x > q->x;
}

/* The runs of one window, in screen coordinates, clipped to the window. */
static LONG tx_collect(struct Window *w, struct row *out, LONG room)
{
    struct Layer *layers[2];
    LONG n = 0, k, i;

    layers[0] = w->WLayer;
    layers[1] = w->BorderRPort && w->BorderRPort->Layer != w->WLayer ? w->BorderRPort->Layer : NULL;
    for (k = 0; k < 2; k++) {
        struct Layer *ly = layers[k];
        struct tx *e;
        if (!ly)
            continue;
        for (e = tx_head[TX_BUCKET(ly)]; e && n < room; e = e->next) {
            LONG sx, sy, mx, my;
            if (e->layer != ly || !e->len)
                continue;
            sx = ly->bounds.MinX - ly->Scroll_X + e->x;
            sy = ly->bounds.MinY - ly->Scroll_Y + e->y;
            mx = sx + e->w / 2, my = sy + e->h / 2;
            if (mx < w->LeftEdge || mx >= w->LeftEdge + w->Width || my < w->TopEdge ||
                my >= w->TopEdge + w->Height)
                continue;  /* drawn outside, or scrolled out of a bigger layer */
            out[n].win = w;
            out[n].x = sx, out[n].y = sy, out[n].w = e->w, out[n].h = e->h, out[n].cw = e->cw;
            out[n].len = e->len;
            for (i = 0; i < e->len; i++)
                out[n].s[i] = e->s[i];
            n++;
        }
    }
    return n;
}

/* Gadget labels as rows: GadTools draws button labels on OS 3.2 (and under
 * VisualPrefs) without going through graphics' Text vector, so the hooks never
 * see them. The label is in the gadget itself; it goes in where no recorded
 * text covers that spot yet. Called with IntuitionBase locked. */
static LONG gadget_rows(struct Window *w, struct row *out, LONG n, LONG room)
{
    struct Gadget *g;
    struct TextFont *f = w->RPort ? w->RPort->Font : NULL;
    LONG count = 0, base = n;

    if (!f)
        return n;
    for (g = w->FirstGadget; g && n < room && count < 256; g = g->NextGadget, count++) {
        struct IntuiText *it = NULL;
        const UBYTE *txt;
        LONG box[4], len, tw, x, y, h = f->tf_YSize, k, cx, cy;
        BOOL centred = FALSE, covered = FALSE;

        if (g->GadgetType & GTYP_SYSGADGET || !g->GadgetText)
            continue;
        if ((g->Flags & GFLG_LABELMASK) == GFLG_LABELSTRING) {
            txt = (const UBYTE *)g->GadgetText;
            centred = TRUE;
        } else {
            if ((g->Flags & GFLG_LABELMASK) == GFLG_LABELITEXT)
                it = g->GadgetText;
            else {
                it = image_text((struct Image *)g->GadgetText);
                centred = TRUE;
            }
            if (!it || !it->IText)
                continue;
            txt = it->IText;
        }
        for (len = 0; txt[len] && len < TX_LEN; len++)
            ;
        if (!len)
            continue;
        gadget_box(w, g, box);
        tw = TextLength(w->RPort, (STRPTR)txt, len);
        for (k = 0; k < len; k++)  /* GadTools underscores mark the shortcut key */
            if (txt[k] == '_')
                tw -= TextLength(w->RPort, (STRPTR)"_", 1);
        if (centred || !it) {
            x = box[0] + (box[2] - tw) / 2;
            y = box[1] + (box[3] - h) / 2;
        } else {
            x = box[0] + it->LeftEdge;
            y = box[1] + it->TopEdge;
        }
        cx = x + tw / 2, cy = y + h / 2;
        for (k = 0; k < base && !covered; k++)
            covered = out[k].win == w && cx >= out[k].x && cx < out[k].x + out[k].w &&
                      cy >= out[k].y && cy < out[k].y + out[k].h;
        if (covered)
            continue;
        out[n].win = w;
        out[n].x = x, out[n].y = y, out[n].w = tw > 0 ? tw : len * f->tf_XSize, out[n].h = h;
        out[n].cw = 0;
        out[n].len = 0;
        for (k = 0; k < len; k++)
            if (txt[k] != '_')
                out[n].s[out[n].len++] = txt[k] < 32 ? '?' : txt[k];
        n++;
    }
    return n;
}

static UBYTE lower(UBYTE c)
{
    return (c >= 'A' && c <= 'Z') || (c >= 0xC0 && c <= 0xDE && c != 0xD7) ? c + 32 : c;
}

/* Rows of one window into t as L lines; with match, only the first line that
 * contains it (any case), and TRUE when there was one. */
static BOOL tx_lines(struct text *t, struct row *r, LONG n, const UBYTE *match)
{
    LONG i = 0, j, k;
    UBYTE line[1100];

    qsort(r, n, sizeof(*r), row_order);
    while (i < n) {
        LONG len = 0, x0 = r[i].x, end = r[i].x, y = r[i].y, h = r[i].h;
        /* Same row, close together: one line. Far apart they are columns or
         * buttons of their own, each with its own box to click. */
        for (j = i; j < n && r[j].y == y && r[j].h == h && (j == i || r[j].x - end <= 2 * h); j++) {
            LONG gap = r[j].x - end, spaces;
            if (j > i && gap > 1) {
                spaces = r[j].cw ? (gap + r[j].cw / 2) / r[j].cw : 1;
                if (spaces > 40)
                    spaces = 3;  /* columns far apart: a few spaces tell enough */
                while (spaces-- > 0 && len < 1024)
                    line[len++] = ' ';
            }
            for (k = 0; k < r[j].len && len < 1024; k++)
                line[len++] = r[j].s[k];
            if (r[j].x + r[j].w > end)
                end = r[j].x + r[j].w;
        }
        while (len && line[len - 1] == ' ')
            len--;
        line[len] = 0;
        if (len) {
            if (match) {
                LONG m = strlen((const char *)match), a;
                for (a = 0; a + m <= len; a++) {
                    for (k = 0; k < m && lower(line[a + k]) == lower(match[k]); k++)
                        ;
                    if (k == m)
                        break;
                }
                if (a + m > len) {
                    i = j;
                    continue;
                }
            }
            {
                LONG box[4];
                box[0] = x0, box[1] = y, box[2] = end - x0, box[3] = h;
                t_chr(t, 'L');
                t_tab(t);
                t_hex(t, (ULONG)r[i].win);
                t_box(t, box);
                t_tab(t);
                t_str(t, line, FALSE);
                t_chr(t, '\n');
            }
            if (match)
                return TRUE;
        }
        i = j;
    }
    return FALSE;
}

static BOOL tx_is_old(struct Window *w)
{
    LONG i;

    for (i = 0; i < TX_OLD; i++)
        if (tx_old[i] == w)
            return TRUE;
    return FALSE;
}

/* Text of the front screen, window by window front to back. With match only
 * the first line that has it; returns whether one did. */
static BOOL screen_text(struct text *t, const UBYTE *match)
{
    struct Screen *s;
    struct Layer *l;
    struct row *rows = tx_size ? AllocVec(tx_size * sizeof(struct row), MEMF_ANY) : NULL;
    ULONG lock;
    LONG n;
    BOOL found = FALSE;

    if (!match) {
        t_chr(t, 'I');
        t_tab(t);
        t_num(t, tx_pool ? (LONG)((now_ms() - tx_since) / 1000) : -1);
        t_tab(t);
        t_num(t, tx_evicted);
        t_tab(t);
        t_num(t, tx_used);
        t_tab(t);
        t_num(t, tx_size);
        t_chr(t, '\n');
    }
    if (!tx_pool)
        return FALSE;
    if (!rows) {
        t->broke = TRUE;
        return FALSE;
    }
    lock = LockIBase(0);
    s = IntuitionBase->FirstScreen;
    for (l = s->LayerInfo.top_layer; l && !found; l = l->back) {
        struct Window *w;
        LONG box[4];
        BOOL mon;

        for (w = s->FirstWindow; w && w != (struct Window *)l->Window; w = w->NextWindow)
            ;
        if (!w || l != w->WLayer)
            continue;
        /* amigent-monitor's log repeats what the agent looks for: no match there */
        mon = mon_task && w->UserPort && w->UserPort->mp_SigTask == mon_task;
        if (match && mon)
            continue;
        if (!match) {
            box[0] = w->LeftEdge, box[1] = w->TopEdge, box[2] = w->Width, box[3] = w->Height;
            t_chr(t, 'W');
            t_tab(t);
            t_hex(t, (ULONG)w);
            t_box(t, box);
            t_tab(t);
            if (w == IntuitionBase->ActiveWindow) t_chr(t, 'a');
            if (w->Flags & WFLG_BACKDROP) t_chr(t, 'b');
            if (tx_is_old(w)) t_chr(t, 'o');
            if (mon) t_chr(t, 'm');
            t_tab(t);
            t_str(t, w->Title, FALSE);
            t_chr(t, '\n');
        }
        Forbid();  /* the hooks change the runs under Forbid only */
        n = tx_collect(w, rows, tx_size);
        Permit();
        n = gadget_rows(w, rows, n, tx_size);
        found = tx_lines(t, rows, n, match);
    }
    UnlockIBase(lock);
    FreeVec(rows);
    return found;
}

/* Another amigentd (on another port) may have had the hooks when this one
 * started; once it is gone they are free. Asked for text or debug output,
 * try again, at most every ten seconds. */
static void hooks_again(void)
{
    static ULONG last;
    static volatile BOOL trying;
    BOOL mine = FALSE;

    if (hooks_owned)
        return;
    Forbid();
    if (!trying && now_ms() - last > 10000) {
        trying = mine = TRUE;
        last = now_ms();
    }
    Permit();
    if (!mine)
        return;
    hooks_start();
    trying = FALSE;
}

static BOOL c_screen_text(struct link *l)
{
    struct text t = {0};

    hooks_again();
    screen_text(&t, NULL);
    return c_text(l, &t);
}

/* ---- WAIT: until a window or a text turns up, or goes ----------------------- */

/* arg: 1 window appears, 2 window gone, 3 text appears, 4 text gone; +8 for 1:
 * only a window that was not there yet; +16 with 8: not there yet means not
 * among the y window addresses (u32 each) that follow the pattern, as the
 * client saw them before it started a program. w = timeout in seconds (at
 * most 3600). */
#define WAIT_SEEN 32

static struct Window *find_window(const UBYTE *pat, struct Window **seen, LONG nseen, struct text *t)
{
    struct Screen *scr;
    struct Window *win, *found = NULL;
    ULONG lock = LockIBase(0);
    LONG k;

    for (scr = IntuitionBase->FirstScreen; scr && !found; scr = scr->NextScreen)
        for (win = scr->FirstWindow; win && !found; win = win->NextWindow) {
            if (!win->Title || !MatchPatternNoCase((STRPTR)pat, win->Title))
                continue;
            for (k = 0; k < nseen && seen[k] != win; k++)
                ;
            if (k < nseen)
                continue;
            found = win;
            if (t) {
                LONG box[4];
                box[0] = win->LeftEdge, box[1] = win->TopEdge, box[2] = win->Width, box[3] = win->Height;
                t_chr(t, 'W');
                t_tab(t);
                t_hex(t, (ULONG)win);
                t_box(t, box);
                t_tab(t);
                if (win == IntuitionBase->ActiveWindow) t_chr(t, 'a');
                if (scr != IntuitionBase->FirstScreen) t_chr(t, 'h');  /* on a screen behind */
                t_tab(t);
                t_str(t, win->Title, FALSE);
                t_chr(t, '\n');
            }
        }
    UnlockIBase(lock);
    return found;
}

/* The pattern for a window title, as ACTIVATEWINDOW takes it. */
static UBYTE *title_pattern(const UBYTE *arg)
{
    LONG n = strlen((const char *)arg);
    UBYTE *src = AllocVec(2 * n + 5, MEMF_ANY), *pat = AllocVec(4 * n + 12, MEMF_ANY), *q;
    const UBYTE *a;
    BOOL wild = FALSE;

    if (src && pat) {
        for (a = arg; *a; a++)
            if (strchr("#?*()|~[]%'", *a))
                wild = TRUE;
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
        if (ParsePatternNoCase(src, pat, 4 * n + 12) < 0) {
            FreeVec(pat);
            pat = NULL;
        }
    } else {
        FreeVec(pat);
        pat = NULL;
    }
    FreeVec(src);
    return pat;
}

static BOOL c_wait(struct link *l, LONG how, UWORD len, UWORD known, UWORD secs)
{
    struct Window *seen[WAIT_SEEN];
    struct text t = {0};
    BOOL alive, got = FALSE;
    LONG nseen = 0, kind = how & 7;
    ULONG start = now_ms(), gen = ~0UL;
    UBYTE *pat = NULL;
    char *arg = c_name(l, len, &alive);
    static const char *const wait_what[] = {"wait", "wait for window", "wait till gone",
                                            "wait for text", "wait till no text"};

    if (alive)
        mon_say('C', wait_what[kind <= 4 ? kind : 0], arg);
    if (!alive)
        return FALSE;
    if (how & 16) {  /* the windows the client knew: they don't count as new */
        LONG k;
        for (k = 0; k < known; k++) {
            UBYTE b[4];
            if (!l_recv(l, b, 4, IO_SECONDS)) {
                FreeVec(arg);
                return FALSE;
            }
            if (nseen < WAIT_SEEN)
                seen[nseen++] = (struct Window *)((ULONG)b[0] << 24 | (ULONG)b[1] << 16 |
                                                  (ULONG)b[2] << 8 | b[3]);
        }
    }
    if (!arg)
        return c_fault(l, ERROR_NO_FREE_STORE);
    if (secs > 3600)
        secs = 3600;
    if (kind < 1 || kind > 4 || !len) {
        FreeVec(arg);
        return c_fail(l, 1, "bad wait");
    }
    if (kind >= 3)
        hooks_again();
    if (kind >= 3 && !tx_pool) {
        FreeVec(arg);
        return c_fail(l, 1, "no text recording");
    }
    if (kind <= 2 && !(pat = title_pattern((UBYTE *)arg))) {
        FreeVec(arg);
        return c_fail(l, 1, "bad pattern");
    }
    if (kind == 1 && (how & 8) && !(how & 16)) {  /* only new ones: remember those there */
        struct Screen *scr;
        struct Window *win;
        ULONG lock = LockIBase(0);
        for (scr = IntuitionBase->FirstScreen; scr; scr = scr->NextScreen)
            for (win = scr->FirstWindow; win && nseen < WAIT_SEEN; win = win->NextWindow)
                if (win->Title && MatchPatternNoCase((STRPTR)pat, win->Title))
                    seen[nseen++] = win;
        UnlockIBase(lock);
    }
    for (;;) {
        if (kind == 1)
            got = find_window(pat, seen, nseen, &t) != NULL;
        else if (kind == 2)
            got = find_window(pat, NULL, 0, NULL) == NULL;
        else if (tx_gen != gen || kind == 4) {
            gen = tx_gen;
            t.len = 0;
            got = screen_text(&t, (UBYTE *)arg);
            if (kind == 4)
                got = !got;
        }
        if (got || t.broke)
            break;
        t.len = 0;
        if (secs && now_ms() - start >= secs * 1000UL)
            break;
        if (wait_ms(100, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) {
            FreeVec(t.buf);
            FreeVec(pat);
            FreeVec(arg);
            return FALSE;
        }
    }
    FreeVec(pat);
    FreeVec(arg);
    if (!got) {
        FreeVec(t.buf);
        return c_fail(l, 2, "timeout");
    }
    if (kind == 2 || kind == 4)
        t.len = 0;
    return c_text(l, &t);
}

/* ---- TREE: a whole folder in one answer ------------------------------------ */

/* Lines as DIR has them, with the path below the folder as name; arg 1 adds the
 * CRC-32 of each file. w = most entries (0: 50000). Folders that can't be read
 * come as "ERR", path, the error text. */
#define TREE_DEPTH 24

static ULONG crc_table[256];

static void crc_init(void)
{
    ULONG c;
    LONG i, k;

    if (crc_table[1])
        return;
    for (i = 0; i < 256; i++) {
        for (c = i, k = 0; k < 8; k++)
            c = c & 1 ? 0xEDB88320 ^ (c >> 1) : c >> 1;
        crc_table[i] = c;
    }
}

static LONG crc_file(const char *path, ULONG *out, UBYTE *buf)
{
    ULONG crc = 0xFFFFFFFF;
    BPTR fh = Open((STRPTR)path, MODE_OLDFILE);
    LONG n, i;

    if (!fh)
        return IoErr();
    while ((n = Read(fh, buf, CHUNK)) > 0)
        for (i = 0; i < n; i++)
            crc = crc_table[(crc ^ buf[i]) & 0xFF] ^ (crc >> 8);
    Close(fh);
    if (n < 0)
        return IoErr();
    *out = ~crc;
    return 0;
}

static BOOL c_tree(struct link *l, BOOL with_crc, UWORD len, UWORD most)
{
    struct FileInfoBlock *fib[TREE_DEPTH];
    BPTR locks[TREE_DEPTH];
    LONG depth = 0, count = 0, err = 0, plen[TREE_DEPTH + 1], i;
    UBYTE *buf = with_crc ? AllocVec(CHUNK, MEMF_ANY) : NULL;
    char *full = AllocVec(1024, MEMF_ANY);
    struct text t = {0};
    BOOL alive, more = FALSE;
    char *path = c_name(l, len, &alive);

    if (alive)
        mon_say('C', "tree", path);
    for (i = 0; i < TREE_DEPTH; i++)
        fib[i] = NULL;
    if (!alive) {
        FreeVec(buf);
        FreeVec(full);
        return FALSE;
    }
    if (!most)
        most = 50000;
    crc_init();
    if (!path || !full || (with_crc && !buf) || !(fib[0] = AllocDosObject(DOS_FIB, NULL)))
        err = ERROR_NO_FREE_STORE;
    else if (!(locks[0] = Lock((STRPTR)path, SHARED_LOCK)))
        err = IoErr();
    else if (!Examine(locks[0], fib[0]) || fib[0]->fib_DirEntryType <= 0) {
        err = fib[0]->fib_DirEntryType <= 0 ? ERROR_OBJECT_WRONG_TYPE : IoErr();
        UnLock(locks[0]);
    } else {
        full[0] = 0;  /* the path below the top, '/' between the parts */
        plen[0] = 0;
        depth = 1;
    }
    while (depth > 0) {
        struct FileInfoBlock *f = fib[depth - 1];
        BPTR here = locks[depth - 1];
        LONG at, k;
        BOOL dir;

        if (!ExNext(here, f)) {
            if (IoErr() != ERROR_NO_MORE_ENTRIES) {
                t_str(&t, (const UBYTE *)"ERR\t", FALSE);
                t_str(&t, (const UBYTE *)full, FALSE);
                t_tab(&t);
                t_num(&t, IoErr());
                t_chr(&t, '\n');
            }
            UnLock(here);
            depth--;
            full[plen[depth]] = 0;
            continue;
        }
        if (count++ >= most) {
            more = TRUE;
            while (depth > 0)
                UnLock(locks[--depth]);
            break;
        }
        at = plen[depth - 1];
        if (at)
            full[at++] = '/';
        for (k = 0; f->fib_FileName[k] && at < 1000; k++)
            full[at++] = f->fib_FileName[k];
        full[at] = 0;
        dir = f->fib_DirEntryType > 0 && f->fib_DirEntryType != ST_SOFTLINK;
        t_str(&t, (const UBYTE *)(dir ? "DIR" : "FILE"), FALSE);
        t_tab(&t);
        t_str(&t, (const UBYTE *)full, FALSE);
        t_tab(&t);
        t_num(&t, dir ? 0 : f->fib_Size);
        t_tab(&t);
        t_hex(&t, f->fib_Protection);
        t_tab(&t);
        t_date(&t, &f->fib_Date);
        if (with_crc && !dir) {
            ULONG crc = 0;
            BPTR old = CurrentDir(here);
            LONG e = crc_file(f->fib_FileName, &crc, buf);
            CurrentDir(old);
            t_tab(&t);
            if (e)
                t_chr(&t, '-');
            else
                t_hex(&t, crc);
        }
        t_chr(&t, '\n');
        if (dir && depth < TREE_DEPTH) {
            BPTR old = CurrentDir(here), sub = Lock(f->fib_FileName, SHARED_LOCK);
            CurrentDir(old);
            if (sub && !fib[depth])
                fib[depth] = AllocDosObject(DOS_FIB, NULL);
            if (sub && fib[depth] && Examine(sub, fib[depth])) {
                locks[depth] = sub;
                plen[depth] = at;
                depth++;
            } else {
                if (sub)
                    UnLock(sub);
                t_str(&t, (const UBYTE *)"ERR\t", FALSE);
                t_str(&t, (const UBYTE *)full, FALSE);
                t_tab(&t);
                t_num(&t, IoErr());
                t_chr(&t, '\n');
            }
        }
        full[plen[depth - 1]] = 0;
    }
    if (more)
        t_str(&t, (const UBYTE *)"MORE\n", FALSE);
    for (i = 0; i < TREE_DEPTH; i++)
        if (fib[i])
            FreeDosObject(DOS_FIB, fib[i]);
    FreeVec(buf);
    FreeVec(full);
    FreeVec(path);
    if (err) {
        FreeVec(t.buf);
        return c_fault(l, err);
    }
    return c_text(l, &t);
}

/* ---- DEBUGLOG: what went through RawPutChar -------------------------------- */

/* w/h = the count to start from (high/low word), as the last answer told;
 * answers u32 count now, u32 length, the bytes since then (at most the ring).
 * 0xFFFFFFFF asks for the count alone. */
static BOOL c_debuglog(struct link *l, ULONG since)
{
    UBYTE head[9], *copy;
    ULONG total, from, n, i;
    BOOL ok;

    hooks_again();
    if (!dbg_ring)
        return c_fail(l, 1, "no debug capture");
    total = dbg_total;
    from = since == 0xFFFFFFFFUL ? total : since > total || total - since > DBG_SIZE ? (total > DBG_SIZE ? total - DBG_SIZE : 0) : since;
    n = total - from;
    if (!(copy = AllocVec(n + 1, MEMF_ANY)))
        return c_fault(l, ERROR_NO_FREE_STORE);
    for (i = 0; i < n; i++)
        copy[i] = dbg_ring[(from + i) & (DBG_SIZE - 1)];
    head[0] = 0;
    be32(head + 1, total);
    be32(head + 5, n);
    ok = l_send(l, head, 9, IO_SECONDS) && l_send(l, copy, n, IO_SECONDS);
    FreeVec(copy);
    return ok;
}

/* ---- protocol 6: what is loaded, and reading memory -------------------- */

/* One line per library, device or resource:
 * L|D|R  name  version  revision  open count  base  neg size  pos size  id string */
static void t_libs(struct text *t, char kind, struct List *list)
{
    struct Node *n;

    for (n = list->lh_Head; n->ln_Succ; n = n->ln_Succ) {
        struct Library *lib = (struct Library *)n;

        t_chr(t, kind);
        t_tab(t);
        t_str(t, (const UBYTE *)(n->ln_Name ? n->ln_Name : "?"), FALSE);
        t_tab(t);
        if (kind == 'R' && n->ln_Type != NT_LIBRARY && n->ln_Type != NT_RESOURCE) {
            /* some resources are a bare node, without library fields */
            t_add(t, "0\t0\t0\t", 6);
            t_hex(t, (ULONG)n);
            t_add(t, "\t0\t0\t\n", 6);
            continue;
        }
        t_num(t, lib->lib_Version);
        t_tab(t);
        t_num(t, lib->lib_Revision);
        t_tab(t);
        t_num(t, lib->lib_OpenCnt);
        t_tab(t);
        t_hex(t, (ULONG)lib);
        t_tab(t);
        t_num(t, lib->lib_NegSize);
        t_tab(t);
        t_num(t, lib->lib_PosSize);
        t_tab(t);
        if (lib->lib_IdString && (TypeOfMem(lib->lib_IdString) ||
                                  (ULONG)lib->lib_IdString >= 0xF80000UL))
            t_str(t, (const UBYTE *)lib->lib_IdString, FALSE);
        t_chr(t, '\n');
    }
}

/* H  process  command  hunk  start  size: the code of programs that run, so the
 * PC can name an address (a guru's, a debugger's) as program, hunk and offset. */
static void t_hunks(struct text *t, struct List *list)
{
    struct Node *n;

    for (n = list->lh_Head; n->ln_Succ; n = n->ln_Succ) {
        struct Process *pr = (struct Process *)n;
        const UBYTE *cmd = NULL;
        BPTR seg = 0;
        LONG hunk;

        if (n->ln_Type != NT_PROCESS)
            continue;
        if (pr->pr_CLI) {
            struct CommandLineInterface *cli = BADDR(pr->pr_CLI);
            seg = cli->cli_Module;
            cmd = cli->cli_CommandName ? BADDR(cli->cli_CommandName) : NULL;
        }
        if (!seg && pr->pr_SegList) {  /* started from Workbench or by CreateNewProc() */
            BPTR *array = BADDR(pr->pr_SegList);
            if (array && TypeOfMem(array) && array[0] >= 3 && array[0] < 16)
                seg = array[3];
        }
        for (hunk = 0; seg && hunk < 64; hunk++) {
            ULONG *p = BADDR(seg);

            /* a hunk is a size long, a link and the code; anything else is not a seglist */
            if (((ULONG)p & 3) || !TypeOfMem(p - 1) || p[-1] < 8 || p[-1] > 0x1000000UL ||
                !TypeOfMem((UBYTE *)p + p[-1] - 5))
                break;

            t_chr(t, 'H');
            t_tab(t);
            t_str(t, (const UBYTE *)(n->ln_Name ? n->ln_Name : "?"), FALSE);
            t_tab(t);
            if (cmd && cmd[0]) {
                LONG k;
                for (k = 1; k <= cmd[0]; k++)
                    t_chr(t, cmd[k] == '\t' || cmd[k] < ' ' ? ' ' : cmd[k]);
            }
            t_tab(t);
            t_num(t, hunk);
            t_tab(t);
            t_hex(t, (ULONG)(p + 1));
            t_tab(t);
            t_num(t, p[-1] - 8);  /* the size long counts itself and the link */
            t_chr(t, '\n');
            seg = *p;
        }
    }
}

/* M  name  version  start  end: resident modules, from ROM and from disk */
static void t_residents(struct text *t)
{
    ULONG *rm = (ULONG *)SysBase->ResModules;
    LONG guard = 0;

    while (rm && *rm && guard++ < 1024) {
        struct Resident *r;

        if (*rm & 0x80000000UL) {  /* a link to the next table */
            rm = (ULONG *)(*rm & 0x7FFFFFFFUL);
            continue;
        }
        r = (struct Resident *)*rm++;
        t_chr(t, 'M');
        t_tab(t);
        t_str(t, (const UBYTE *)(r->rt_Name ? r->rt_Name : "?"), FALSE);
        t_tab(t);
        t_num(t, r->rt_Version);
        t_tab(t);
        t_hex(t, (ULONG)r);
        t_tab(t);
        t_hex(t, (ULONG)r->rt_EndSkip);
        t_chr(t, '\n');
    }
}

static BOOL c_loaded(struct link *l)
{
    struct text t = {0};

    Forbid();
    t_libs(&t, 'L', &SysBase->LibList);
    t_libs(&t, 'D', &SysBase->DeviceList);
    t_libs(&t, 'R', &SysBase->ResourceList);
    t_hunks(&t, &SysBase->TaskReady);
    t_hunks(&t, &SysBase->TaskWait);
    t_residents(&t);
    Permit();
    return c_text(l, &t);
}

#define PEEK_MAX 65536

/* Only RAM that exec manages, and the Kickstart ROM: custom chips, CIAs and
 * expansion boards can change state or hang the bus when read. */
static BOOL peek_allowed(ULONG at, ULONG n)
{
    ULONG end = at + n - 1;
    struct MemHeader *mh;
    BOOL ok = FALSE;

    if (!n || n > PEEK_MAX || end < at)
        return FALSE;
#ifdef AMIGENT_APOLLO
    /* A MemHeader that covered a SAGA, PIP, VREG or ARNE window would still
     * be a read or write with side effects. Those windows are refused first. */
    if (apollo_mmio_hit(at, n))
        return FALSE;
#endif
    if (at >= 0xF80000UL && end <= 0xFFFFFFUL)
        return TRUE;
    if (end < 0x400)  /* vectors and ExecBase at 4: chip RAM below exec's list */
        return TRUE;
    Forbid();
    for (mh = (struct MemHeader *)SysBase->MemList.lh_Head; mh->mh_Node.ln_Succ;
         mh = (struct MemHeader *)mh->mh_Node.ln_Succ)
        if (at >= (ULONG)mh->mh_Lower && end < (ULONG)mh->mh_Upper) {
            ok = TRUE;
            break;
        }
    Permit();
    return ok;
}

/* The address comes as hex text in the name, the length in w:h. */
static BOOL c_peek(struct link *l, UWORD len, ULONG n)
{
    struct text t = {0};
    BOOL alive;
    char *hex = c_name(l, len, &alive), *p;
    ULONG at = 0;

    if (!hex)
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    for (p = hex; *p; p++)
        at = at << 4 | (*p <= '9' ? *p - '0' : (*p | 0x20) - 'a' + 10);
    FreeVec(hex);
    if (!peek_allowed(at, n))
        return c_fail(l, 1, "only RAM exec manages and the Kickstart ROM, at most 64 KB");
    t_add(&t, (const void *)at, n);
    return c_text(l, &t);
}

/* The hex address in a request's name; FALSE when the link broke. */
static BOOL c_address(struct link *l, UWORD len, ULONG *at, BOOL *alive)
{
    char *hex = c_name(l, len, alive), *p;

    *at = 0;
    if (!hex)
        return FALSE;
    for (p = hex; *p; p++)
        *at = *at << 4 | (*p <= '9' ? *p - '0' : (*p | 0x20) - 'a' + 10);
    FreeVec(hex);
    return TRUE;
}

/* ---- tasks: what runs, at which priority, with how much stack ---------- */

/* T  address  name  command  type  pri  state  stack_size  stack_used  sig_wait  sig_recvd */
static void t_task(struct text *t, struct Task *task, const char *state)
{
    const UBYTE *cmd = NULL;
    ULONG size = (ULONG)task->tc_SPUpper - (ULONG)task->tc_SPLower;
    ULONG used = task == SysBase->ThisTask ? 0 : (ULONG)task->tc_SPUpper - (ULONG)task->tc_SPReg;

    if (task->tc_Node.ln_Type == NT_PROCESS && ((struct Process *)task)->pr_CLI) {
        struct CommandLineInterface *cli = BADDR(((struct Process *)task)->pr_CLI);
        cmd = cli->cli_CommandName ? BADDR(cli->cli_CommandName) : NULL;
    }
    t_chr(t, 'T');
    t_tab(t);
    t_hex(t, (ULONG)task);
    t_tab(t);
    t_str(t, (const UBYTE *)(task->tc_Node.ln_Name ? task->tc_Node.ln_Name : "?"), FALSE);
    t_tab(t);
    if (cmd && cmd[0]) {
        LONG k;
        for (k = 1; k <= cmd[0]; k++)
            t_chr(t, cmd[k] == '\t' || cmd[k] < ' ' ? ' ' : cmd[k]);
    }
    t_tab(t);
    t_str(t, (const UBYTE *)(task->tc_Node.ln_Type == NT_PROCESS ? "process" : "task"), FALSE);
    t_tab(t);
    t_num(t, task->tc_Node.ln_Pri);
    t_tab(t);
    t_str(t, (const UBYTE *)state, FALSE);
    t_tab(t);
    t_num(t, size);
    t_tab(t);
    t_num(t, used > size ? 0 : used);
    t_tab(t);
    t_hex(t, task->tc_SigWait);
    t_tab(t);
    t_hex(t, task->tc_SigRecvd);
    t_chr(t, '\n');
}

static BOOL c_tasks(struct link *l)
{
    struct text t = {0};
    struct Node *n;

    Forbid();
    t_task(&t, SysBase->ThisTask, "run");
    for (n = SysBase->TaskReady.lh_Head; n->ln_Succ; n = n->ln_Succ)
        t_task(&t, (struct Task *)n, "ready");
    for (n = SysBase->TaskWait.lh_Head; n->ln_Succ; n = n->ln_Succ)
        t_task(&t, (struct Task *)n, "wait");
    Permit();
    return c_text(l, &t);
}

/* A task by address, only if it is one; call under Forbid. */
static struct Task *real_task(ULONG at)
{
    struct Node *n;

    if ((ULONG)SysBase->ThisTask == at)
        return SysBase->ThisTask;
    for (n = SysBase->TaskReady.lh_Head; n->ln_Succ; n = n->ln_Succ)
        if ((ULONG)n == at)
            return (struct Task *)n;
    for (n = SysBase->TaskWait.lh_Head; n->ln_Succ; n = n->ln_Succ)
        if ((ULONG)n == at)
            return (struct Task *)n;
    return NULL;
}

/* arg 1: priority y (-128..127); arg 2: break signals y (1 C, 2 D, 4 E, 8 F) */
static BOOL c_taskctl(struct link *l, UBYTE op, UWORD len, UWORD y)
{
    struct Task *task;
    BOOL alive;
    ULONG at;

    if (!c_address(l, len, &at, &alive))
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    Forbid();
    task = real_task(at);
    if (task && op == 1)
        SetTaskPri(task, (BYTE)(WORD)y);
    else if (task && op == 2)
        Signal(task, (ULONG)(y & 15) << 12);  /* SIGBREAKB_CTRL_C is 12 */
    Permit();
    if (!task)
        return c_fail(l, 1, "no task at that address");
    return op == 1 || op == 2 ? c_ok(l) : c_fail(l, 1, "unknown task operation");
}

/* ---- poke: only when amigentd was started with ALLOWPOKE --------------- */

static void flush_caches(void);

/* The whole POKE payload gets IO_SECONDS plus a second for every POKE_RATE
 * bytes: 64 KB at 256 bytes a second still arrives, within 286 s. */
#define POKE_RATE 256UL

static ULONG poke_budget_ms(ULONG n)
{
    if (n > PEEK_MAX)
        n = PEEK_MAX;  /* n * 1000 stays far below 2^32 */
    return IO_SECONDS * 1000UL + n * 1000UL / POKE_RATE;
}

/* Length uses the same PEEK_MAX bound as PEEK. Order: address -> size ->
 * ALLOWPOKE -> region -> receive -> write. A huge n must not allocate or
 * hang the receive before ALLOWPOKE is checked. The payload must keep
 * coming (IO_SECONDS between fragments) and be complete within
 * poke_budget_ms(n). */
static BOOL c_poke(struct link *l, UWORD len, ULONG n)
{
    BOOL alive;
    ULONG at, deadline;
    UBYTE *data;

    if (!c_address(l, len, &at, &alive))
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    if (!n)
        return c_fail(l, 1, "only RAM exec manages, at most 64 KB");
    if (n > PEEK_MAX)
        return FALSE;  /* close: do not drain an unbounded declared payload */
    deadline = now_ms() + poke_budget_ms(n);
    if (!allow_poke)
        return l_skip_until(l, n, deadline) &&
               c_fail(l, 1, "writing memory is off; start amigentd with ALLOWPOKE to allow it");
#ifdef AMIGENT_APOLLO
    /* APoke is not this command. The same RAM gate applies, and a window
     * whose read has a side effect is not a place to write either. */
    if (apollo_mmio_hit(at, n))
        return l_skip_until(l, n, deadline) &&
               c_fail(l, 1, "that range includes Apollo registers that a read can change");
#endif
    if (!peek_allowed(at, n) || at >= 0xF80000UL)
        return l_skip_until(l, n, deadline) &&
               c_fail(l, 1, "only RAM exec manages, at most 64 KB");
    if (!(data = AllocVec(n, MEMF_ANY)))
        return l_skip_until(l, n, deadline) && c_fault(l, ERROR_NO_FREE_STORE);
    if (!l_recv_until(l, data, n, deadline)) {
        FreeVec(data);
        return FALSE;  /* incomplete / deadline: close - do not treat as next command */
    }
    mon_say('C', "write memory", NULL);
    Disable();
    CopyMem(data, (APTR)at, n);
    Enable();
    CacheClearU();
    FreeVec(data);
    return c_ok(l);
}

/* ---- trace: tracepoints in a running program ----------------------------
 * A tracepoint puts TRAP #15 over one instruction of a program's code and
 * makes our trap handler (src/native/amiga/daemon/trapstub.s) that task's tc_TrapCode. When the
 * program gets there, the handler keeps the registers and a bit of the stack,
 * puts the instruction back and lets the program go on as if nothing
 * happened: it fires once, and the program never stops.
 *
 * The trap stub is the upstream 1.4.0 handler. Its behaviour on a 68080 has
 * not been validated on hardware. Apollo builds (AMIGENT_APOLLO) refuse TRACE
 * set before any code or trap-handler change; list and clear still work. */

static const UBYTE trap_code[68] = {
    0x0c, 0x97, 0x00, 0x00, 0x00, 0x2f, 0x66, 0x2a, 0x48, 0xe7, 0xff, 0xfe,
    0x4e, 0x68, 0x2f, 0x08, 0x48, 0x6f, 0x00, 0x04, 0x2e, 0x3a, 0x00, 0x22,
    0x67, 0x12, 0x28, 0x47, 0x4e, 0x94, 0x4a, 0x80, 0x67, 0x0a, 0x50, 0x8f,
    0x4c, 0xdf, 0x7f, 0xff, 0x58, 0x8f, 0x4e, 0x73, 0x50, 0x8f, 0x4c, 0xdf,
    0x7f, 0xff, 0x2f, 0x3a, 0x00, 0x08, 0x4e, 0x75, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x41, 0x4d, 0x54, 0x52};
#define TRAP_HOOK 56
#define TRAP_OLD 60
#define TRACES 8

struct trace {
    UWORD *at;
    UWORD orig;
    UBYTE armed, hit;
    struct Task *task;
    UBYTE *stub;
    ULONG regs[18];  /* d0-d7, a0-a7 (the user stack), SR, PC */
    ULONG stack[8];
    ULONG when;      /* the how manyth hit this was */
};

static struct trace traces[TRACES];
static ULONG trace_count;
static UBYTE cpu_cache;  /* 0 none, 1 68020/030, 2 68040/060 */

/* Clear the caches from supervisor mode, where CacheClearU() is no help. */
#ifdef __VBCC__
void sv_flush030(void) = "\tdc.w\t$4e7a,$0002\n\tdc.w\t$0040,$0808\n\tdc.w\t$4e7b,$0002";
void sv_flush040(void) = "\tdc.w\t$f4f8";
#else
static inline void sv_flush030(void)
{
    __asm__ volatile(".word 0x4e7a,0x0002\n\t.word 0x0040,0x0808\n\t.word 0x4e7b,0x0002" : : : "d0", "cc");
}
static inline void sv_flush040(void)
{
    __asm__ volatile(".word 0xf4f8");
}
#endif

static void flush_caches(void)
{
    CacheClearU();
}

/* Called by the stub in supervisor mode: regs is d0-d7, a0-a6, the trap
 * number, then SR (word) and PC (long) of the exception frame. */
static LONG trace_hit(ULONG *regs, ULONG usp)
{
    UBYTE *frame = (UBYTE *)(regs + 16);
    ULONG pc = *(ULONG *)(frame + 2) - 2;
    LONG i, k;

    for (i = 0; i < TRACES; i++) {
        struct trace *tr = &traces[i];

        if (!tr->armed || (ULONG)tr->at != pc)
            continue;
        for (k = 0; k < 15; k++)
            tr->regs[k] = regs[k];
        tr->regs[15] = usp;
        tr->regs[16] = *(UWORD *)frame;
        tr->regs[17] = pc;
        for (k = 0; k < 8; k++)
            tr->stack[k] = ((ULONG *)usp)[k];
        *tr->at = tr->orig;
        if (cpu_cache == 2)
            sv_flush040();
        else if (cpu_cache == 1)
            sv_flush030();
        *(ULONG *)(frame + 2) = pc;
        tr->when = ++trace_count;  /* no timer here: supervisor mode */
        tr->armed = 0;
        tr->hit = 1;
        return 1;
    }
    return 0;  /* some other TRAP #15: the task's earlier trap code gets it */
}

/* The process whose code holds at, or NULL; call under Forbid. */
static struct Task *code_owner(ULONG at)
{
    struct List *lists[2];
    struct Node *n;
    LONG i;

    lists[0] = &SysBase->TaskReady;
    lists[1] = &SysBase->TaskWait;
    for (i = 0; i < 2; i++)
        for (n = lists[i]->lh_Head; n->ln_Succ; n = n->ln_Succ) {
            struct Process *pr = (struct Process *)n;
            BPTR seg = 0;
            LONG hunk;

            if (n->ln_Type != NT_PROCESS)
                continue;
            if (pr->pr_CLI)
                seg = ((struct CommandLineInterface *)BADDR(pr->pr_CLI))->cli_Module;
            if (!seg && pr->pr_SegList) {
                BPTR *array = BADDR(pr->pr_SegList);
                if (array && TypeOfMem(array) && array[0] >= 3 && array[0] < 16)
                    seg = array[3];
            }
            for (hunk = 0; seg && hunk < 64; hunk++) {
                ULONG *p = BADDR(seg);

                if (((ULONG)p & 3) || !TypeOfMem(p - 1) || p[-1] < 8 || p[-1] > 0x1000000UL)
                    break;
                if (at >= (ULONG)(p + 1) && at + 2 <= (ULONG)(p - 1) + p[-1])
                    return (struct Task *)pr;
                seg = *p;
            }
        }
    return NULL;
}

static void trace_disarm(struct trace *tr)
{
    Disable();
    if (tr->armed) {
        if (*tr->at == 0x4E4F)  /* only our TRAP goes; the code may be gone or reused */
            *tr->at = tr->orig;
        tr->armed = 0;
    }
    Enable();
    CacheClearU();
}

/* Before amigentd goes: no TRAP #15 stays behind; the stubs stay, their hook
 * cleared, since a task keeps its tc_TrapCode. */
static void traces_off(void)
{
    LONG i;

    for (i = 0; i < TRACES; i++) {
        trace_disarm(&traces[i]);
        if (traces[i].stub)
            *(ULONG *)(traces[i].stub + TRAP_HOOK) = 0;
    }
    CacheClearU();
}

static void t_trace(struct text *t, LONG i)
{
    struct trace *tr = &traces[i];
    LONG k;

    t_str(t, (const UBYTE *)(tr->hit ? "HIT" : tr->armed ? "SET" : "OFF"), FALSE);
    t_tab(t);
    t_hex(t, (ULONG)tr->at);
    t_tab(t);
    t_hex(t, (ULONG)tr->task);
    if (tr->hit) {
        t_tab(t);
        t_num(t, tr->when);
        for (k = 0; k < 18; k++) {
            t_tab(t);
            t_hex(t, tr->regs[k]);
        }
        for (k = 0; k < 8; k++) {
            t_tab(t);
            t_hex(t, tr->stack[k]);
        }
    }
    t_chr(t, '\n');
}

/* arg 1: set at the address; 2: list; 3: clear all */
static BOOL c_trace(struct link *l, UBYTE op, UWORD len)
{
    struct text t = {0};
    struct trace *tr = NULL;
    struct Task *task;
    BOOL alive;
    ULONG at = 0;
    LONG i;

    if (len && !c_address(l, len, &at, &alive))
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    if (op == 2) {
        for (i = 0; i < TRACES; i++)
            if (traces[i].at)
                t_trace(&t, i);
        return c_text(l, &t);
    }
    if (op == 3) {
        for (i = 0; i < TRACES; i++) {
            trace_disarm(&traces[i]);
            traces[i].at = NULL;
            traces[i].hit = 0;
        }
        return c_ok(l);
    }
    if (op != 1)
        return c_fail(l, 1, "unknown trace operation");
#ifdef AMIGENT_APOLLO
    /* 68080 trap stub not hardware-validated: refuse before patching code or tc_TrapCode. */
    (void)at;
    return c_fail(l, 1, "tracepoints are not supported on this Apollo build yet");
#endif
    if (at & 1)
        return c_fail(l, 1, "code starts at an even address");
    for (i = 0; i < TRACES; i++)  /* a second one would take our TRAP for the instruction */
        if (traces[i].armed && (ULONG)traces[i].at == at)
            return c_fail(l, 1, "a tracepoint waits there already");
    for (i = 0; i < TRACES && !tr; i++)
        if (!traces[i].armed && !(traces[i].hit && traces[i].at))
            tr = &traces[i];
    if (!tr)
        return c_fail(l, 1, "all 8 tracepoints are in use; clear them first");
    Forbid();
    task = code_owner(at);
    Permit();
    if (!task)
        return c_fail(l, 1, "not in the code of a running program (see LOADED)");
    if (task == FindTask(NULL) || task == server)
        return c_fail(l, 1, "not in amigentd itself");
    if (!tr->stub || tr->task != task) {  /* one stub per task, it keeps the task's old trap code */
        LONG j;
        UBYTE *stub = NULL;
        for (j = 0; j < TRACES; j++)
            if (traces[j].stub && traces[j].task == task)
                stub = traces[j].stub;
        if (!stub) {
            if (!(stub = AllocMem(sizeof(trap_code), MEMF_PUBLIC)))
                return c_fault(l, ERROR_NO_FREE_STORE);
            CopyMem((APTR)trap_code, stub, sizeof(trap_code));
            *(ULONG *)(stub + TRAP_HOOK) = (ULONG)trace_hit;
            CacheClearU();
        }
        tr->stub = stub;
    }
    Forbid();
    if (!real_task((ULONG)task)) {
        Permit();
        return c_fail(l, 1, "the program ended meanwhile");
    }
    if (task->tc_TrapCode != (APTR)tr->stub) {
        *(ULONG *)(tr->stub + TRAP_OLD) = (ULONG)task->tc_TrapCode;
        task->tc_TrapCode = (APTR)tr->stub;
    }
    *(ULONG *)(tr->stub + TRAP_HOOK) = (ULONG)trace_hit;
    tr->task = task;
    tr->at = (UWORD *)at;
    tr->hit = 0;
    Disable();
    tr->orig = *tr->at;
    if (tr->orig == 0x4E4F) {  /* a TRAP #15 there already: not ours to take */
        Enable();
        Permit();
        return c_fail(l, 1, "there is a TRAP #15 there already");
    }
    *tr->at = 0x4E4F;  /* TRAP #15 */
    tr->armed = 1;
    Enable();
    Permit();
    CacheClearU();
    mon_say('C', "tracepoint set", NULL);
    return c_ok(l);
}

/* ---- probes: locks, CPU load, window dragging, a quiet monitor --------- */

/* The monitor stops drawing for secs seconds (0 again at once): its window
 * holds its layer while it draws, and Intuition waits for that. */
static void quiet_monitor(ULONG secs)
{
    char n[12];

    mon_quiet_until = secs ? now_ms() + secs * 1000 : 0;
    dec(n, secs);
    mon_say('Q', n, NULL);
}

/* A task's name, and its command when a Shell runs one. */
static void t_who(struct text *t, struct Task *task)
{
    if (!task) {
        t_chr(t, '-');
        return;
    }
    t_hex(t, (ULONG)task);
    t_chr(t, ' ');
    t_str(t, (const UBYTE *)(TypeOfMem(task) && task->tc_Node.ln_Name ? task->tc_Node.ln_Name : "?"), FALSE);
    if (TypeOfMem(task) && task->tc_Node.ln_Type == NT_PROCESS && ((struct Process *)task)->pr_CLI) {
        struct CommandLineInterface *cli = BADDR(((struct Process *)task)->pr_CLI);
        const UBYTE *cmd = cli->cli_CommandName ? BADDR(cli->cli_CommandName) : NULL;
        LONG k;

        if (cmd && cmd[0]) {
            t_str(t, (const UBYTE *)" (", FALSE);
            for (k = 1; k <= cmd[0]; k++)
                t_chr(t, cmd[k] < ' ' ? ' ' : cmd[k]);
            t_chr(t, ')');
        }
    }
}

/* K  kind  address  what  owner  nest  queue  waiters (", " between, s: shared) */
static void t_sem(struct text *t, const char *kind, APTR at, const UBYTE *what,
                  struct SignalSemaphore *sem)
{
    struct SemaphoreRequest *r;
    LONG n = 0;

    t_chr(t, 'K');
    t_tab(t);
    t_str(t, (const UBYTE *)kind, FALSE);
    t_tab(t);
    t_hex(t, (ULONG)at);
    t_tab(t);
    t_str(t, what ? what : (const UBYTE *)"", FALSE);
    t_tab(t);
    t_who(t, sem->ss_Owner);
    t_tab(t);
    t_num(t, sem->ss_NestCount);
    t_tab(t);
    t_num(t, sem->ss_QueueCount);
    t_tab(t);
    for (r = (struct SemaphoreRequest *)sem->ss_WaitQueue.mlh_Head;
         r->sr_Link.mln_Succ && n < 16; r = (struct SemaphoreRequest *)r->sr_Link.mln_Succ, n++) {
        ULONG w = (ULONG)r->sr_Waiter;

        if (n)
            t_str(t, (const UBYTE *)", ", FALSE);
        t_who(t, (struct Task *)(w & ~1UL));
        if (w & 1)
            t_str(t, (const UBYTE *)" s", FALSE);
    }
    t_chr(t, '\n');
}

/* Who holds the layer locks of a screen and its windows, and who waits.
 * Only Forbid, never LockIBase or LockLayerInfo: the point is to look at a
 * screen that may be stuck in them. */
static BOOL c_locks(struct link *l, UWORD len)
{
    struct text t = {0};
    struct Screen *s;
    struct Window *w;
    struct Layer *ly;
    BOOL alive;
    ULONG want;

    if (!c_address(l, len, &want, &alive))
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    Forbid();
    for (s = IntuitionBase->FirstScreen; want && s && (ULONG)s != want; s = s->NextScreen)
        ;
    if (!s)
        s = IntuitionBase->FirstScreen;
    if (s) {
        t_sem(&t, "layerinfo", s, screen_title(s), &s->LayerInfo.Lock);
        for (ly = s->LayerInfo.top_layer; ly; ly = ly->back) {
            for (w = s->FirstWindow; w && w->WLayer != ly; w = w->NextWindow)
                ;
            if (w)
                t_sem(&t, "window", w, w->Title, &ly->Lock);
            else if (ly->Lock.ss_Owner || ly->Lock.ss_QueueCount >= 0)
                t_sem(&t, "layer", ly, NULL, &ly->Lock);
        }
    }
    if (GfxBase->ActiViewCprSemaphore)
        t_sem(&t, "view", GfxBase->ActiViewCprSemaphore, (const UBYTE *)"graphics ActiView",
              GfxBase->ActiViewCprSemaphore);
    Permit();
    return c_text(l, &t);
}

#define CPU_SLOTS 64

struct cpu_slot {
    struct Task *task;
    ULONG n;
    char name[40];
};

/* What runs, sampled every ms milliseconds for secs seconds by a task above
 * everything else: each time it wakes, the head of TaskReady is what it
 * interrupted (or the one next in turn at that priority), none means idle.
 * The PC asks once; no traffic while it samples. */
static BOOL c_cpu(struct link *l, UWORD secs, UWORD ms)
{
    struct text t = {0};
    struct MsgPort *port;
    struct timerequest *tr;
    struct cpu_slot *slot;
    struct Task *me = FindTask(NULL);
    ULONG samples, i, idle = 0, other = 0, disp0 = SysBase->DispCount, idle0 = SysBase->IdleCount;
    LONG used = 0, k, old;

    if (secs < 1) secs = 1;
    if (secs > 60) secs = 60;
    if (ms < 2) ms = 2;
    if (ms > 1000) ms = 1000;
    samples = (ULONG)secs * 1000 / ms;
    if (!(slot = AllocVec(sizeof(*slot) * CPU_SLOTS, MEMF_ANY | MEMF_CLEAR)))
        return c_fault(l, ERROR_NO_FREE_STORE);
    if (!(port = CreateMsgPort())) {
        FreeVec(slot);
        return c_fault(l, ERROR_NO_FREE_STORE);
    }
    tr = (struct timerequest *)CreateIORequest(port, sizeof(*tr));
    if (!tr || OpenDevice((CONST_STRPTR)TIMERNAME, UNIT_MICROHZ, (struct IORequest *)tr, 0)) {
        if (tr)
            DeleteIORequest((struct IORequest *)tr);
        DeleteMsgPort(port);
        FreeVec(slot);
        return c_fail(l, 1, "timer.device did not open");
    }
    mon_say('C', "measure CPU load", NULL);
    quiet_monitor(secs + 2);
    old = SetTaskPri(me, 30);
    for (i = 0; i < samples; i++) {
        struct Node *n;

        tr->tr_node.io_Command = TR_ADDREQUEST;
        tr->tr_time.tv_secs = ms / 1000;
        tr->tr_time.tv_micro = (ms % 1000) * 1000UL;
        DoIO((struct IORequest *)tr);
        Forbid();
        n = SysBase->TaskReady.lh_Head;
        if (!n->ln_Succ)
            idle++;
        else {
            for (k = 0; k < used && slot[k].task != (struct Task *)n; k++)
                ;
            if (k == used && used < CPU_SLOTS) {
                slot[k].task = (struct Task *)n;
                strncpy(slot[k].name, n->ln_Name ? n->ln_Name : "?", sizeof(slot[k].name) - 1);
                used++;
            }
            if (k < used)
                slot[k].n++;
            else
                other++;
        }
        Permit();
    }
    SetTaskPri(me, old);
    quiet_monitor(0);
    CloseDevice((struct IORequest *)tr);
    DeleteIORequest((struct IORequest *)tr);
    DeleteMsgPort(port);
    /* N  samples  ms  idle  other  dispatches  idle_count (exec's counters meanwhile) */
    t_chr(&t, 'N');
    t_tab(&t);
    t_num(&t, samples);
    t_tab(&t);
    t_num(&t, ms);
    t_tab(&t);
    t_num(&t, idle);
    t_tab(&t);
    t_num(&t, other);
    t_tab(&t);
    t_num(&t, SysBase->DispCount - disp0);
    t_tab(&t);
    t_num(&t, SysBase->IdleCount - idle0);
    t_chr(&t, '\n');
    for (k = 0; k < used; k++) {  /* C  address  samples  name */
        t_chr(&t, 'C');
        t_tab(&t);
        t_hex(&t, (ULONG)slot[k].task);
        t_tab(&t);
        t_num(&t, slot[k].n);
        t_tab(&t);
        t_str(&t, (const UBYTE *)slot[k].name, FALSE);
        t_chr(&t, '\n');
    }
    FreeVec(slot);
    return c_text(l, &t);
}

static void drag_event(struct IOStdReq *io, struct InputEvent *ev)
{
    GetSysTime(&ev->ie_TimeStamp);
    io->io_Command = IND_WRITEEVENT;
    io->io_Data = ev;
    io->io_Length = sizeof(*ev);
    io->io_Flags = 0;
    DoIO((struct IORequest *)io);
}

static void drag_to(struct IOStdReq *io, struct Screen *s, WORD x, WORD y, BOOL down)
{
    struct InputEvent ev;
    struct IEPointerPixel pp;

    pp.iepp_Screen = s;
    pp.iepp_Position.X = x;
    pp.iepp_Position.Y = y;
    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_NEWPOINTERPOS;
    ev.ie_SubClass = IESUBCLASS_PIXEL;
    ev.ie_Qualifier = down ? IEQUALIFIER_LEFTBUTTON : 0;
    ev.ie_EventAddress = &pp;
    drag_event(io, &ev);
}

static void drag_button(struct IOStdReq *io, BOOL down)
{
    struct InputEvent ev;

    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_RAWMOUSE;
    ev.ie_Code = IECODE_LBUTTON | (down ? 0 : IECODE_UP_PREFIX);
    ev.ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE | (down ? IEQUALIFIER_LEFTBUTTON : 0);
    drag_event(io, &ev);
}

/* The window whose layer is frontmost at x,y on s; call under Forbid. */
static struct Window *window_at(struct Screen *s, LONG x, LONG y)
{
    struct Layer *ly;
    struct Window *w;

    for (ly = s->LayerInfo.top_layer; ly; ly = ly->back)
        if (x >= ly->bounds.MinX && x <= ly->bounds.MaxX && y >= ly->bounds.MinY && y <= ly->bounds.MaxY) {
            for (w = s->FirstWindow; w && w->WLayer != ly; w = w->NextWindow)
                ;
            return w;
        }
    return NULL;
}

/* Drag a window by its title bar, as a hand would, and time every step:
 * pattern 0 zigzag (8 steps one way, 8 back), 1 jumps (4 steps' worth each
 * way in turn). Runs at priority 21, above input.device. One line a step:
 * D  step  x  y  event_us  frame_us  left  top  mouse_x  mouse_y */
static BOOL c_drag(struct link *l, UBYTE pattern, UWORD len, UWORD steps, UWORD step)
{
    struct text t = {0};
    struct MsgPort *port;
    struct IOStdReq *io;
    struct Screen *s;
    struct Window *w, *hit = NULL;
    struct Gadget *g;
    struct Task *me = FindTask(NULL);
    struct EClockVal e0;
    LONG box[4], x, y, x0 = 0, y0 = 0, i, old, dx;
    BOOL alive, found = FALSE;
    ULONG want, frame_us;

    if (!c_address(l, len, &want, &alive))
        return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
    if (steps < 2) steps = 2;
    if (steps > 512) steps = 512;
    if (step < 1) step = 1;
    if (step > 64) step = 64;
    Forbid();
    s = IntuitionBase->FirstScreen;
    for (w = s ? s->FirstWindow : NULL; w && (ULONG)w != want; w = w->NextWindow)
        ;
    if (w) {
        for (g = w->FirstGadget; g; g = g->NextGadget)
            if ((g->GadgetType & GTYP_SYSGADGET) && (g->GadgetType & GTYP_SYSTYPEMASK) == GTYP_WDRAGGING) {
                gadget_box(w, g, box);
                found = TRUE;
                break;
            }
    }
    if (found) {
        x0 = box[0] + box[2] / 2;
        y0 = box[1] + box[3] / 2;
        hit = window_at(s, x0, y0);
    }
    Permit();
    if (!w)
        return c_fail(l, 1, "no such window on the front screen");
    if (!found)
        return c_fail(l, 1, "the window has no title bar to drag");
    if (hit != w)
        return c_fail(l, 1, "another window covers its title bar; bring it to the front first");
    if (!(port = CreateMsgPort()))
        return c_fault(l, ERROR_NO_FREE_STORE);
    io = (struct IOStdReq *)CreateIORequest(port, sizeof(*io));
    if (!io || OpenDevice((CONST_STRPTR)"input.device", 0, (struct IORequest *)io, 0)) {
        if (io)
            DeleteIORequest((struct IORequest *)io);
        DeleteMsgPort(port);
        return c_fail(l, 1, "input.device did not open");
    }
    mon_say('C', "measure window dragging", NULL);
    quiet_monitor(steps / 25 + 5);
    old = SetTaskPri(me, 21);
    drag_to(io, s, x0, y0, FALSE);
    WaitTOF();
    drag_button(io, TRUE);
    WaitTOF();
    WaitTOF();
    x = x0;
    y = y0;
    for (i = 0; i < steps; i++) {
        dx = pattern == 1 ? ((i & 1) ? -4 * step : 4 * step) : (((i >> 3) & 1) ? -step : step);
        x += dx;
        ReadEClock(&e0);
        drag_to(io, s, x, y, TRUE);
        frame_us = eclock_us(&e0);  /* the event, until input.device took it */
        WaitTOF();
        t_chr(&t, 'D');
        t_tab(&t);
        t_num(&t, i);
        t_tab(&t);
        t_num(&t, x);
        t_tab(&t);
        t_num(&t, y);
        t_tab(&t);
        t_num(&t, frame_us);
        t_tab(&t);
        t_num(&t, eclock_us(&e0));  /* until the next frame began */
        t_tab(&t);
        t_num(&t, w->LeftEdge);
        t_tab(&t);
        t_num(&t, w->TopEdge);
        t_tab(&t);
        t_num(&t, s->MouseX);
        t_tab(&t);
        t_num(&t, s->MouseY);
        t_chr(&t, '\n');
    }
    drag_button(io, FALSE);
    SetTaskPri(me, old);
    quiet_monitor(0);
    CloseDevice((struct IORequest *)io);
    DeleteIORequest((struct IORequest *)io);
    DeleteMsgPort(port);
    return c_text(l, &t);
}

/* ---- Emu68: what runs on the Raspberry Pi, and its JIT settings ---------
 *
 * Emu68 (the 68k JIT on a PiStorm and other Pi boards) offers the Amiga side
 * devicetree.resource with its own /emu68 node, the Pi firmware's mailbox,
 * and control registers that MOVEC reads and writes (docs/internals/
 * ControlRegisters.md in the Emu68 sources). Nothing here touches a control
 * register before /emu68 with an idstring was found: on any other 68k the
 * MOVEC would be an illegal instruction. Only the JIT fields Emu68 documents
 * as changeable can be written, and only in memory: nothing goes to the SD card.
 * Built with vbcc only; other builds answer emu68=0. */

#ifdef __VBCC__
#define EMU68_BUILT 1
APTR __DT_OpenKey(__reg("a6") APTR, __reg("a0") const char *name) = "\tjsr\t-6(a6)";
void __DT_CloseKey(__reg("a6") APTR, __reg("a0") APTR key) = "\tjsr\t-12(a6)";
APTR __DT_FindProperty(__reg("a6") APTR, __reg("a0") APTR key, __reg("a1") const char *name) = "\tjsr\t-24(a6)";
ULONG __DT_GetPropLen(__reg("a6") APTR, __reg("a0") APTR prop) = "\tjsr\t-36(a6)";
APTR __DT_GetPropValue(__reg("a6") APTR, __reg("a0") APTR prop) = "\tjsr\t-48(a6)";
void __MB_RawCommand(__reg("a6") APTR, __reg("a0") ULONG *command) = "\tjsr\t-6(a6)";
/* Supervisor mode only. MOVEC Rc,D0 is $4e7a,$0xxx; MOVEC D0,Rc is $4e7b,$0xxx. */
void emu68_movec_all(__reg("a0") ULONG *r) =
    "\tdc.w\t$4e7a,$00e0\n\tmove.l\td0,(a0)+\n"   /* CNTFRQ */
    "\tdc.w\t$4e7a,$00e2\n\tmove.l\td0,(a0)+\n"   /* CNTVALHI */
    "\tdc.w\t$4e7a,$00e1\n\tmove.l\td0,(a0)+\n"   /* CNTVALLO */
    "\tdc.w\t$4e7a,$00e2\n\tmove.l\td0,(a0)+\n"   /* CNTVALHI again */
    "\tdc.w\t$4e7a,$00e4\n\tmove.l\td0,(a0)+\n"   /* INSNCNTHI */
    "\tdc.w\t$4e7a,$00e3\n\tmove.l\td0,(a0)+\n"   /* INSNCNTLO */
    "\tdc.w\t$4e7a,$00e4\n\tmove.l\td0,(a0)+\n"
    "\tdc.w\t$4e7a,$00e6\n\tmove.l\td0,(a0)+\n"   /* ARMCNTHI */
    "\tdc.w\t$4e7a,$00e5\n\tmove.l\td0,(a0)+\n"   /* ARMCNTLO */
    "\tdc.w\t$4e7a,$00e6\n\tmove.l\td0,(a0)+\n"
    "\tdc.w\t$4e7a,$00e7\n\tmove.l\td0,(a0)+\n"   /* JITSIZE */
    "\tdc.w\t$4e7a,$00e8\n\tmove.l\td0,(a0)+\n"   /* JITFREE */
    "\tdc.w\t$4e7a,$00e9\n\tmove.l\td0,(a0)+\n"   /* JITCOUNT */
    "\tdc.w\t$4e7a,$00ea\n\tmove.l\td0,(a0)+\n"   /* JITSCFTHRESH */
    "\tdc.w\t$4e7a,$00eb\n\tmove.l\td0,(a0)+\n"   /* JITCTRL */
    "\tdc.w\t$4e7a,$00ec\n\tmove.l\td0,(a0)+\n"   /* JITCMISS */
    "\tdc.w\t$4e7a,$01e0\n\tmove.l\td0,(a0)+\n"   /* JITCTRL2 */
    "\tdc.w\t$4e7a,$0002\n\tmove.l\td0,(a0)";     /* CACR */
void emu68_movec_set(__reg("a0") ULONG *v) =
    "\tmove.l\t(a0)+,d0\n\tdc.w\t$4e7b,$00ea\n"   /* JITSCFTHRESH */
    "\tmove.l\t(a0)+,d0\n\tdc.w\t$4e7b,$00eb\n"   /* JITCTRL */
    "\tmove.l\t(a0),d0\n\tdc.w\t$4e7b,$01e0";     /* JITCTRL2 */
#else
#define EMU68_BUILT 0
#endif

#define E68_REGS 18
#define E68_SFTHRESH 13
#define E68_JITCTRL 14
#define E68_JITCTRL2 16
/* The fields Emu68 documents as settable: JITCTRL soft flush, loop count,
 * inline range, instruction depth; JITCTRL2 chip and DBF slowdown, CCR scan
 * depth, chip slowdown ratio, blitter wait. Debug registers are not offered. */
#define E68_CTRL_MASK 0xFFFFFFF1UL
#define E68_CTRL2_MASK 0x00000FFBUL

static APTR dt_base;

static APTR dt_prop(const char *key, const char *name, ULONG *len)
{
#if EMU68_BUILT
    APTR k, p, v = NULL;

    if (!dt_base || !(k = __DT_OpenKey(dt_base, key)))
        return NULL;
    if ((p = __DT_FindProperty(dt_base, k, name))) {
        v = __DT_GetPropValue(dt_base, p);
        if (len)
            *len = __DT_GetPropLen(dt_base, p);
    }
    __DT_CloseKey(dt_base, k);
    return v;
#else
    return NULL;
#endif
}

/* Emu68 for certain: its devicetree.resource has an /emu68 node with an idstring. */
static BOOL emu68_present(void)
{
#if EMU68_BUILT
    ULONG len = 0;

    if (!dt_base)
        dt_base = OpenResource("devicetree.resource");
    return dt_base && dt_prop("/emu68", "idstring", &len) && len > 1;
#else
    return FALSE;
#endif
}

/* All counters and settings; the 64 bit counters read again until their high half stands still. */
static void emu68_read(ULONG *r)
{
#if EMU68_BUILT
    LONG tries;
    APTR ssp;

    for (tries = 0; tries < 8; tries++) {
        ssp = SuperState();
        emu68_movec_all(r);
        if (ssp)
            UserState(ssp);
        if (r[1] == r[3] && r[4] == r[6] && r[7] == r[9])
            break;
    }
#endif
}

/* Set the documented fields; the other bits stay. ea, eb, 1e0 as reg, value, mask. */
static void emu68_write(const ULONG *want, ULONG n)
{
#if EMU68_BUILT
    ULONG r[E68_REGS], v[3], i;
    APTR ssp;

    Disable();  /* read, change, write in one go: no other tool in between */
    ssp = SuperState();
    emu68_movec_all(r);
    v[0] = r[E68_SFTHRESH];
    v[1] = r[E68_JITCTRL];
    v[2] = r[E68_JITCTRL2];
    for (i = 0; i < n; i++) {
        ULONG reg = want[i * 3], val = want[i * 3 + 1], mask = want[i * 3 + 2];
        LONG at = reg == 0xea ? 0 : reg == 0xeb ? 1 : reg == 0x1e0 ? 2 : -1;

        if (at < 0)
            continue;
        mask &= at == 0 ? 0xFFFFFFFFUL : at == 1 ? E68_CTRL_MASK : E68_CTRL2_MASK;
        v[at] = (v[at] & ~mask) | (val & mask);
    }
    emu68_movec_set(v);
    if (ssp)
        UserState(ssp);
    Enable();
    CacheClearU();  /* on Emu68 this flushes the JIT cache: the new settings apply from now */
#endif
}

/* A Pi firmware property request with one tag: value words in v (two at most).
 * Through mailbox.resource when there is one, else as EmuControl does it, at
 * the mailbox found in the device tree, under Forbid and with a time limit. */
static APTR mb_base;
static volatile ULONG *mb_regs;
static BOOL mb_looked;

static void mb_find(void)
{
#if EMU68_BUILT
    const char *alias;
    const ULONG *reg, *ranges, *cells;
    ULONG addr = 1, cpu = 1, len;

    mb_looked = TRUE;
    if ((mb_base = OpenResource("mailbox.resource")))
        return;
    if (!(alias = dt_prop("/aliases", "mailbox", NULL)))
        return;
    if ((cells = dt_prop("/soc", "#address-cells", NULL)))
        addr = *cells;
    if ((cells = dt_prop("/", "#address-cells", NULL)))
        cpu = *cells;
    if (addr < 1 || addr > 3 || cpu < 1 || cpu > 3)
        return;
    reg = dt_prop(alias, "reg", &len);
    ranges = dt_prop("/soc", "ranges", NULL);
    if (!reg || len < addr * 4 || !ranges)
        return;
    mb_regs = (volatile ULONG *)(reg[addr - 1] - ranges[addr - 1] + ranges[addr + cpu - 1]);
#endif
}

static ULONG le32(ULONG x)
{
    return x >> 24 | (x >> 8 & 0xFF00) | (x << 8 & 0xFF0000) | x << 24;
}

static BOOL mb_tag(ULONG tag, ULONG words, ULONG *v)
{
#if EMU68_BUILT
    /* In the Pi's own memory, never on a stack that might be Chip RAM across the bus */
    ULONG *raw = AllocMem(64, MEMF_FAST | MEMF_PUBLIC | MEMF_CLEAR);
    ULONG *m = (ULONG *)(((ULONG)raw + 15) & ~15UL), i, len = 32;
    BOOL ok = FALSE;

    if (!raw)
        return FALSE;
    if (!mb_looked)
        mb_find();
    m[0] = 32;
    m[1] = 0;
    m[2] = tag;
    m[3] = 8;
    m[4] = 0;
    m[5] = v[0];
    m[6] = words > 1 ? v[1] : 0;
    m[7] = 0;
    if (mb_base) {
        __MB_RawCommand(mb_base, m);
    } else if (mb_regs) {
        volatile ULONG *rd = mb_regs, *st = mb_regs + 6, *wr = mb_regs + 8;
        ULONG got = 0, spin;

        for (i = 0; i < 8; i++)
            m[i] = le32(m[i]);
        Forbid();
        CachePreDMA(m, &len, 0);
        for (spin = 0; spin < 200000 && (le32(*st) & 0x80000000UL); spin++)
            ;
        if (spin < 200000) {
            *wr = le32(((ULONG)m & ~15UL) | 8);
            for (spin = 0; spin < 2000000; spin++) {
                if (le32(*st) & 0x40000000UL)
                    continue;
                got = le32(*rd);
                if ((got & 15) == 8)
                    break;
            }
        }
        CachePostDMA(m, &len, 0);
        Permit();
        for (i = 0; i < 8; i++)
            m[i] = le32(m[i]);
        if ((got & 15) != 8)
            m[1] = 0;
    } else
        m[1] = 0;
    ok = m[1] == 0x80000000UL && (m[4] & 0x80000000UL);
    if (ok) {
        v[0] = m[5];
        if (words > 1)
            v[1] = m[6];
    }
    FreeMem(raw, 64);
    return ok;
#else
    return FALSE;
#endif
}

/* A device tree string, as long as it is (bootargs pass the 256 of t_str). */
static void t_prop(struct text *t, const char *out, const char *key, const char *name)
{
    ULONG len = 0, i;
    const UBYTE *v = dt_prop(key, name, &len);

    if (!v || !len)
        return;
    if (len > 4096)
        len = 4096;
    while (len && !v[len - 1])
        len--;
    /* not written as one literal: Version would take that for amigentd's own $VER */
    if (len >= 6 && v[0] == '$' && !strncmp((const char *)v + 1, "VER: ", 5)) {
        v += 6;
        len -= 6;
    }
    t_str(t, (const UBYTE *)out, FALSE);
    t_chr(t, '=');
    for (i = 0; i < len; i++)
        t_chr(t, v[i] < ' ' ? ' ' : v[i]);
    t_chr(t, '\n');
}

static void t_hexpair(struct text *t, const char *key, ULONG v)
{
    t_str(t, (const UBYTE *)key, FALSE);
    t_chr(t, '=');
    t_hex(t, v);
    t_chr(t, '\n');
}

static void t_mb(struct text *t, const char *key, ULONG tag, ULONG id)
{
    ULONG v[2];

    v[0] = id;
    v[1] = 0;
    if (mb_tag(tag, 2, v))
        t_pair(t, key, v[1]);
}

/* emu68=0 without Emu68 and nothing else. what: 0 all, 2 counters and settings only. */
static void emu68_text(struct text *t, UBYTE what)
{
    ULONG r[E68_REGS], v[2];
    const ULONG *ver;
    ULONG len = 0;

    if (!emu68_present()) {
        t_pair(t, "emu68", 0);
        if (!EMU68_BUILT)
            t_str(t, (const UBYTE *)"why=not in this amigentd build\n", FALSE);
        return;
    }
    t_pair(t, "emu68", 1);
    emu68_read(r);
    t_pair(t, "cntfrq", r[0]);
    t_hexpair(t, "cnt_hi", r[1]);
    t_hexpair(t, "cnt_lo", r[2]);
    t_hexpair(t, "insn_hi", r[4]);
    t_hexpair(t, "insn_lo", r[5]);
    t_hexpair(t, "arm_hi", r[7]);
    t_hexpair(t, "arm_lo", r[8]);
    t_pair(t, "jit_size", r[10]);
    t_pair(t, "jit_free", r[11]);
    t_pair(t, "jit_count", r[12]);
    t_pair(t, "jit_sfthresh", r[13]);
    t_hexpair(t, "jitctrl", r[14]);
    t_pair(t, "jit_cmiss", r[15]);
    t_hexpair(t, "jitctrl2", r[16]);
    t_hexpair(t, "cacr", r[17]);
    if (what == 2)
        return;
    t_prop(t, "idstring", "/emu68", "idstring");
    if ((ver = dt_prop("/emu68", "version", &len)) && len >= 12) {
        t_str(t, (const UBYTE *)"version=", FALSE);
        t_num(t, ver[0]);
        t_chr(t, '.');
        t_num(t, ver[1]);
        t_chr(t, '.');
        t_num(t, ver[2]);
        t_chr(t, '\n');
    }
    t_prop(t, "git", "/emu68", "git-hash");
    t_prop(t, "variant", "/emu68", "variant");
    t_prop(t, "model", "/", "model");
    t_prop(t, "compatible", "/", "compatible");
    t_prop(t, "bootargs", "/chosen", "bootargs");
    if ((ver = dt_prop("/emu68", "m68k-jit-size", &len)) && len >= 4)
        t_pair(t, "jit_size_mb", ver[0]);
    v[0] = 0;
    if (mb_tag(0x00010002, 1, v))  /* board revision: model and memory size */
        t_hexpair(t, "board_revision", v[0]);
    v[0] = 0;
    if (mb_tag(0x00000001, 1, v))
        t_pair(t, "firmware", v[0]);
    t_mb(t, "temp", 0x00030006, 0);  /* millidegrees Celsius */
    t_mb(t, "temp_max", 0x0003000a, 0);
    t_mb(t, "arm_clock", 0x00030002, 3);  /* Hz: set, measured, highest */
    t_mb(t, "arm_clock_measured", 0x00030047, 3);
    t_mb(t, "arm_clock_max", 0x00030004, 3);
    t_mb(t, "core_volt", 0x00030003, 1);  /* microvolts */
    v[0] = 0;
    if (mb_tag(0x00030046, 1, v))  /* under-voltage and throttling, now and since power on */
        t_hexpair(t, "throttled", v[0]);
    t_str(t, (const UBYTE *)(mb_base ? "mailbox=resource\n" : mb_regs ? "mailbox=direct\n" : "mailbox=none\n"), FALSE);
}

/* EMU68: c[1] 0 what runs and how it is, 2 counters only; 1 sets the JIT fields
 * sent as reg, value, mask (three longs each), then answers as 2. */
static BOOL c_emu68(struct link *l, UBYTE op, UWORD len)
{
    struct text t = {0};

    if (op == 1) {
        BOOL alive;
        ULONG *want = (ULONG *)c_name(l, len, &alive);

        if (!want)
            return alive ? c_fault(l, ERROR_NO_FREE_STORE) : FALSE;
        if (!emu68_present()) {
            FreeVec(want);
            return c_fail(l, 1, "not Emu68");
        }
        mon_say('C', "Emu68 JIT settings", NULL);
        emu68_write(want, len / 12 > 8 ? 8 : len / 12);
        FreeVec(want);
        op = 2;
    } else if (len && !l_skip(l, len, IO_SECONDS))
        return FALSE;
    else
        mon_say('C', op == 2 ? "Emu68 counters" : "what runs under Emu68", NULL);
    emu68_text(&t, op);
    return c_text(l, &t);
}

static ULONG hexval(const char **p)
{
    ULONG v = 0;
    char c;

    while ((c = **p) && ((c >= '0' && c <= '9') || ((c | 32) >= 'a' && (c | 32) <= 'f'))) {
        v = v << 4 | (c <= '9' ? c - '0' : (c | 32) - 'a' + 10);
        (*p)++;
    }
    return v;
}

/* amigentd EMU68JIT "eb:value:mask,1e0:value:mask" (hex): set the fields from a
 * job's script, around a program; on anything but Emu68 it does nothing. */
static int emu68_cli(const char *spec)
{
    ULONG want[24], n = 0;
    const char *p = spec;

    if (!emu68_present()) {
        PutStr("amigentd: not Emu68, nothing changed\n");
        return RETURN_WARN;
    }
    while (*p && n < 8) {
        want[n * 3] = hexval(&p);
        if (*p != ':')
            break;
        p++;
        want[n * 3 + 1] = hexval(&p);
        if (*p != ':')
            break;
        p++;
        want[n * 3 + 2] = hexval(&p);
        n++;
        if (*p != ',')
            break;
        p++;
    }
    if (*p || !n) {
        PutStr("amigentd: EMU68JIT wants reg:value:mask,... in hex\n");
        return RETURN_ERROR;
    }
    emu68_write(want, n);
    return RETURN_OK;
}

#ifdef AMIGENT_APOLLO
/* What send() accepted. A full window is not an error: the caller pumps again. */
static BOOL audio_send_some(struct link *l, UBYTE *buf, ULONG *sent, ULONG len)
{
    struct Library *SocketBase = l->base;
    LONG n;

    if (*sent >= len)
        return TRUE;
    n = send(l->fd, buf + *sent, len - *sent, 0);
    if (n < 0 && Errno() == EWOULDBLOCK)
        return TRUE;
    if (n <= 0)
        return FALSE;
    *sent += (ULONG)n;
    net_out += (ULONG)n;
    return TRUE;
}

/* 1: a 10-byte command has arrived (the stream's stop). -1: the socket died. */
static int audio_incoming(struct link *l, UBYTE *acc, ULONG *got)
{
    struct Library *SocketBase = l->base;
    LONG n;

    if (*got >= 10)
        return 1;
    n = recv(l->fd, acc + *got, 10 - *got, 0);
    if (n == 0)
        return -1;
    if (n < 0)
        return Errno() == EWOULDBLOCK ? 0 : -1;
    *got += (ULONG)n;
    return *got >= 10 ? 1 : 0;
}

/* One look, then a vblank. Nothing is playing, or a stop is waiting for the
 * last bytes to leave. */
static BOOL audio_idle(ULONG *rate)
{
    if (SetSignal(0, 0) & SIGBREAKF_CTRL_C)
        return FALSE;
    if (rate)
        apollo_audio_pump(rate);
    Delay(1);
    return (SetSignal(0, 0) & SIGBREAKF_CTRL_C) == 0;
}

/* Copy while the mix is moving, and keep sending what is already copied.
 * Stopping every couple of seconds to send was a spurt of sound and then a
 * break of the same length. A full window does not wait: the bytes stay
 * queued and the next look tries again. Quiet for a moment means it stopped. */
static BOOL audio_push(struct link *l, UBYTE *out, ULONG *out_len, ULONG *out_sent, ULONG rate,
                       ULONG budget);

/* A few milliseconds, this task asleep. The port is this task's, so the
 * timer signals here. The device is the one the main task already opened.
 * Opening timer.device again from the music task is what stopped the daemon. */
static struct timerequest *yield_io(struct MsgPort **port)
{
    struct timerequest *io;

    *port = NULL;
    if (!timer_io)
        return NULL;
    if (!(*port = CreateMsgPort()))
        return NULL;
    if (!(io = (struct timerequest *)CreateIORequest(*port, sizeof(*io)))) {
        DeleteMsgPort(*port);
        *port = NULL;
        return NULL;
    }
    io->tr_node.io_Device = timer_io->tr_node.io_Device;
    io->tr_node.io_Unit = timer_io->tr_node.io_Unit;
    return io;
}

static void yield_close(struct MsgPort *port, struct timerequest *io)
{
    if (io)
        DeleteIORequest((struct IORequest *)io);
    if (port)
        DeleteMsgPort(port);
}

/* Asleep until one of wake arrives or us have passed, whichever is first; the
 * timer request is back when this returns. Without a timer, one vblank. */
static ULONG yield_wait(struct MsgPort *port, struct timerequest *io, ULONG us, ULONG wake)
{
    ULONG sigs;

    if (!io) {
        Delay(1);
        return SetSignal(0, wake) & wake;  /* taken, as Wait takes them */
    }
    io->tr_node.io_Command = TR_ADDREQUEST;
    io->tr_time.tv_secs = us / 1000000;
    io->tr_time.tv_micro = us % 1000000;
    SendIO((struct IORequest *)io);
    sigs = Wait((1UL << port->mp_SigBit) | wake);
    if (!CheckIO((struct IORequest *)io))
        AbortIO((struct IORequest *)io);
    WaitIO((struct IORequest *)io);
    SetSignal(0, 1UL << port->mp_SigBit);
    return sigs & wake;
}

/* TRUE when the wait finished. FALSE when stop arrived during it. */
static BOOL yield_us(struct MsgPort *port, struct timerequest *io, ULONG us)
{
    return (yield_wait(port, io, us, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) == 0;
}

/* The TLS pump connects from this machine. A music socket from the PC does not. */
static BOOL peer_local(struct link *l)
{
    struct Library *SocketBase = l->base;
    struct sockaddr_in addr;
    LONG len = sizeof(addr);

    if (getpeername(l->fd, (struct sockaddr *)&addr, AMIGENT_SOCKLEN(&len)) != 0)
        return TRUE;
    return addr.sin_addr.s_addr == htonl(INADDR_LOOPBACK);
}

/* Between looks at the mix, as the plain path naps: a half lasts about 10 ms. */
#define AUDIO_LOOK_US 1000
/* At most this long for the pump to pass a batch on, one vblank. Then the
 * loop looks again; a session is never ended by it. */
#define AUDIO_PUMP_US 20000

static BOOL audio_follow(struct link *l, UBYTE *acc, ULONG *acc_n, ULONG *rate, ULONG *since,
                         UBYTE *out, ULONG *out_len, ULONG *out_sent)
{
    struct Task *me = FindTask(NULL);
    BYTE pri = me->tc_Node.ln_Pri;
    struct MsgPort *nap_port = NULL;
    struct timerequest *nap = NULL;
    ULONG look = now_ms(), click = look;
    BOOL plain = !peer_local(l);
    BOOL ok = TRUE;
    UBYTE heard = 0;

    /* Stay above the picture cipher and wait on the timer between looks.
     * Dropping to the bottom and giving the picture one turn let it keep
     * the CPU, and a pass went out only every couple of seconds. */
    nap = yield_io(&nap_port);
    audio_hold = 1;
    audio_task = me;
    SetTaskPri(me, 5);
    for (;;) {
        ULONG seq = apollo_audio_seq();
        ULONG now = 0, got;
        int stop;

        if (plain) {
            ULONG pending;
            int copied;

            apollo_audio_pump(rate);
            copied = apollo_audio_seq() != seq;
            pending = apollo_audio_pending();
            if (copied)
                heard = 1;
            if (pending >= 4) {
                if (!audio_push(l, out, out_len, out_sent, *rate, 4096)) {
                    ok = FALSE;
                    break;
                }
            }
            if (nap) {
                if (!yield_us(nap_port, nap, 1000)) {
                    ok = FALSE;
                    break;
                }
            } else
                Delay(1);
            now = now_ms();
            if (heard) {
                *since = now;
                heard = 0;
            }
            if (SetSignal(0, 0) & SIGBREAKF_CTRL_C) {
                ok = FALSE;
                break;
            }
            if (*since && (ULONG)(now - *since) > 600)
                break;
            if ((ULONG)(now - click) < 200)
                continue;
            stop = audio_incoming(l, acc, acc_n);
            if (stop) {
                if (stop < 0)
                    ok = FALSE;
                break;
            }
            click = now;
            continue;
        }
        apollo_audio_pump(rate);
        if (apollo_audio_seq() != seq)
            *since = now_ms();
        now = now_ms();
        if (apollo_audio_seq() == seq && (ULONG)(now - look) < 15) {
            if (!yield_us(nap_port, nap, AUDIO_LOOK_US)) {  /* asleep, not polling at 5 */
                ok = FALSE;
                break;
            }
            continue;
        }
        if (SetSignal(0, 0) & SIGBREAKF_CTRL_C) {
            ok = FALSE;
            break;
        }
        stop = audio_incoming(l, acc, acc_n);
        if (stop) {
            if (stop < 0)
                ok = FALSE;
            break;
        }
        if (*since && (ULONG)(now - *since) > 600)
            break;
        if (*out_len || apollo_audio_pending() >= 4) {
            if (!plain) {  /* armed first: the pump may pass this batch on at once */
                SetSignal(0, SIGBREAKF_CTRL_D);
                audio_wait = 1;
            }
            if (!audio_push(l, out, out_len, out_sent, *rate, 16384)) {
                ok = FALSE;
                break;
            }
            if (!plain) {
                /* The pump's CTRL_D, or AUDIO_PUMP_US: a pump that went with
                 * its client never signals, and the next look finds the
                 * socket closed. */
                SetTaskPri(me, 3);
                got = yield_wait(nap_port, nap, AUDIO_PUMP_US, SIGBREAKF_CTRL_C | SIGBREAKF_CTRL_D);
                audio_wait = 0;
                if (got & SIGBREAKF_CTRL_C) {
                    ok = FALSE;
                    break;
                }
            }
        }
        now = now_ms();
        if ((ULONG)(now - click) > 200) {
            if (plain && nap) {
                if (!yield_us(nap_port, nap, 3000)) {
                    ok = FALSE;
                    break;
                }
            } else {
                SetTaskPri(me, 3);
                Delay(1);
            }
            click = now_ms();
            now = click;
        }
        look = now_ms();
        SetTaskPri(me, 5);
    }
    yield_close(nap_port, nap);
    audio_wait = 0;
    audio_task = NULL;
    audio_hold = 0;
    SetTaskPri(me, pri);
    return ok;
}

/* What is already queued, up to a few halves. More than that in one wake
 * would sit on the picture's socket. A full window stops the burst. */
static BOOL audio_push(struct link *l, UBYTE *out, ULONG *out_len, ULONG *out_sent, ULONG rate,
                       ULONG budget)
{
    LONG n;

    for (;;) {
        if (*out_sent < *out_len) {
            ULONG before = *out_sent;

            if (!audio_send_some(l, out, out_sent, *out_len))
                return FALSE;
            if (*out_sent == before)
                return TRUE;
            if (*out_sent >= *out_len)
                *out_len = *out_sent = 0;
            continue;
        }
        if (budget < 4 || apollo_audio_pending() < 4)
            return TRUE;
        n = apollo_audio_take(out + 8, 4096);
        if (n < 4)
            return TRUE;
        be32(out, rate);
        be32(out + 4, (ULONG)n);
        *out_len = 8 + (ULONG)n;
        *out_sent = 0;
        budget = (ULONG)n >= budget ? 0 : budget - (ULONG)n;
    }
}

/* Everything copied during the tone. A full window waits one vblank and
 * tries again, so a slow reader does not throw the rest away. */
static BOOL audio_drain(struct link *l, UBYTE *out, ULONG *out_len, ULONG *out_sent, ULONG rate)
{
    ULONG stalls = 0;

    for (;;) {
        ULONG pending = apollo_audio_pending();
        ULONG sent = *out_sent;

        if (!audio_push(l, out, out_len, out_sent, rate, 16384))
            return FALSE;
        if (!*out_len && apollo_audio_pending() < 4)
            return TRUE;
        if (*out_sent == sent && apollo_audio_pending() == pending) {
            if (++stalls > 80)
                return TRUE;
            if (!audio_idle(NULL))
                return FALSE;
        } else
            stalls = 0;
    }
}

/* AUDIO with arg 1. Bytes follow the status byte until arg 2 arrives:
 * u32 rate, u32 length, then that many s16le bytes. Length 0 is a keepalive.
 * Length 0xffffffff ends the stream. The screen connection is not involved.
 * Each finished half is sent once, in the order the DAC played it. */
static BOOL c_audio_stream(struct link *l)
{
    UBYTE out[8 + 4096], acc[10];
    ULONG out_len = 0, out_sent = 0, acc_n = 0, rate = 0, spins = 0, since = 0;
    BOOL ok = TRUE;

    if (!apollo_audio_claim())
        return c_fail(l, 1, "audio is already streaming");
    if (!l_send(l, "\0", 1, IO_SECONDS)) {
        apollo_audio_release();
        return FALSE;
    }
    while (ok) {
        int stop;
        ULONG seq;

        stop = audio_incoming(l, acc, &acc_n);
        if (stop < 0) {
            ok = FALSE;
            break;
        }
        if (stop > 0) {
            ULONG stalls = 0;

            while (ok && out_sent < out_len) {
                ULONG before = out_sent;

                ok = audio_send_some(l, out, &out_sent, out_len);
                if (ok && out_sent == before) {
                    if (++stalls > 50) {
                        ok = FALSE;
                        break;
                    }
                    if (!audio_idle(NULL))
                        ok = FALSE;
                }
            }
            if (ok) {
                be32(out, 0);
                be32(out + 4, 0xFFFFFFFFUL);
                ok = l_send(l, out, 8, IO_SECONDS);
            }
            break;
        }
        seq = apollo_audio_seq();
        if (!since) {
            ULONG burst = 0;

            if (SetSignal(0, 0) & SIGBREAKF_CTRL_C) {
                ok = FALSE;
                break;
            }
            while (burst < 4000 && apollo_audio_seq() == seq) {
                apollo_audio_pump(&rate);
                burst++;
            }
            if (apollo_audio_seq() != seq)
                since = now_ms();
            else {
                if (++spins >= 10) {
                    be32(out, rate);
                    be32(out + 4, 0);
                    out_len = 8;
                    out_sent = 0;
                    spins = 0;
                    ok = audio_send_some(l, out, &out_sent, out_len);
                    if (out_sent >= out_len)
                        out_len = out_sent = 0;
                }
                if (ok && !audio_idle(NULL))
                    ok = FALSE;
                continue;
            }
        }
        spins = 0;
        if (!audio_follow(l, acc, &acc_n, &rate, &since, out, &out_len, &out_sent))
            ok = FALSE;
        if (ok && !audio_drain(l, out, &out_len, &out_sent, rate))
            ok = FALSE;
        if (since && (ULONG)(now_ms() - since) > 300) {
            since = 0;
            apollo_audio_breathe();
        }
    }
    apollo_audio_release();
    return ok;
}

/* AUDIO is its own answer, so a frame on the screen connection is not held up.
 * Nothing is read until a client sends the command. */
static BOOL c_audio(struct link *l, UWORD ms)
{
    UBYTE *buf, head[9];
    ULONG rate = 48000;
    LONG n;
    BOOL ok;

    if (!(buf = AllocVec(48000, MEMF_ANY)))
        return c_fault(l, ERROR_NO_FREE_STORE);
    n = apollo_audio(buf, 48000, &rate, ms);
    if (n < 0)
        n = 0;
    head[0] = 0;
    be32(head + 1, rate);
    be32(head + 5, (ULONG)n);
    ok = l_send(l, head, 9, IO_SECONDS) && (n == 0 || l_send(l, buf, n, IO_SECONDS));
    FreeVec(buf);
    return ok;
}
#endif

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
        return c_read(l, x, c[1] & 1 ? (ULONG)w << 16 | h : 0);
    case CMD_WRITE_FILE:
        return c_write(l, c[1] & 1, (c[1] & 2) != 0, x, (ULONG)w << 16 | h);
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
        mon_say('C', "sysinfo", NULL);
        return c_sysinfo(l);
    case CMD_PAUSE:
        mon_say('C', x ? "pause screen" : "resume screen", NULL);
        set_pause(x);
        return c_ok(l);
    case CMD_VOLUMES:
        mon_say('C', "volumes", NULL);
        return c_volumes(l);
    case CMD_TEXT:
        mon_say('V', "read screen text", NULL);
        return c_screen_text(l);
    case CMD_NOTE: {  /* no answer: the client doesn't wait for the monitor */
        BOOL alive;
        char *text = c_name(l, x, &alive);

        if (text)
            mon_say('A', text, NULL);
        FreeVec(text);
        return alive;
    }
    case CMD_WAIT:
        return c_wait(l, c[1], x, y, w);
    case CMD_COMPRESS:
        return c_compress(l, c[1]);
    case CMD_TREE:
        return c_tree(l, c[1] & 1, x, w);
    case CMD_DEBUGLOG:
        mon_say('C', "debug log", NULL);
        return c_debuglog(l, (ULONG)w << 16 | h);
    case CMD_LOADED:
        mon_say('C', "loaded libraries and programs", NULL);
        return c_loaded(l);
    case CMD_TASKS:
        mon_say('C', "tasks", NULL);
        return c_tasks(l);
    case CMD_TASKCTL:
        mon_say('C', c[1] == 1 ? "task priority" : "signal a task", NULL);
        return c_taskctl(l, c[1], x, y);
    case CMD_POKE:  /* address as hex in the name, then w:h bytes */
        return c_poke(l, x, (ULONG)w << 16 | h);
    case CMD_TRACE:
        return c_trace(l, c[1], x);
    case CMD_LOCKS:  /* a screen handle as hex in the name, none for the front one */
        mon_say('C', "who holds the layer locks", NULL);
        return c_locks(l, x);
    case CMD_CPU:  /* y seconds, every w ms */
        return c_cpu(l, y, w);
    case CMD_DRAG:  /* window as hex in the name; arg pattern, y steps, w pixels */
        return c_drag(l, c[1], x, y, w);
    case CMD_EMU68:  /* c[1]: 0 info, 2 counters, 1 set (reg, value, mask longs in x bytes) */
        return c_emu68(l, c[1], x);
    case CMD_QUIET:  /* the monitor draws nothing for y seconds; 0 ends it */
        quiet_monitor(y);
        return c_ok(l);
    case CMD_PEEK:  /* address as hex in the name, length in w:h */
        mon_say('C', "read memory", NULL);
        return c_peek(l, x, (ULONG)w << 16 | h);
#ifdef AMIGENT_APOLLO
    case CMD_AUDIO:
        if (c[1] & 1)
            return c_audio_stream(l);
        if (c[1] & 2)
            return c_ok(l); /* stop arrived with no stream on this channel */
        return c_audio(l, y);
#endif
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

static void AMIGENT_SAVEDS channel_main(void)
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

        l.zp = NULL;
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
        z_free(l.zp);
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

    static WORD mon_x, mon_y;
    static UBYTE mon_b;
    char at[24], ch[2];

    input_ms = now_ms();
    switch (c[0]) {
    case CMD_MOVE:
        move_to(x, y);
        mon_x = x;
        mon_y = y;
        break;
    case CMD_BUTTON:
        if ((c[1] & 7) & ~mon_b) {  /* only presses */
            mon_xy(at, mon_x, mon_y);
            mon_say('I', c[1] & 2 ? "mouse right at" : c[1] & 4 ? "mouse middle at" : "click at", at);
        }
        mon_b = c[1] & 7;
        set_buttons(c[1] & 7);
        break;
    case CMD_RAWKEY:
        if (c[1] & 1) {
            static const char hex[] = "0123456789ABCDEF";
            at[0] = '$';
            at[1] = hex[x >> 4 & 7];
            at[2] = hex[x & 15];
            at[3] = 0;
            mon_say('I', "key", at);
        }
        key(x & 0x7F, c[1] & 1, y);
        break;
    case CMD_CHAR:
        ch[0] = x < 256 ? (char)x : '?';
        ch[1] = 0;
        mon_say('T', ch, NULL);
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
        net_in += n;
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
        mon_say('C', x ? "pause screen" : "resume screen", NULL);
        set_pause(x);
        return reply(0);
    case CMD_FRAME:
    case CMD_FRAME_RGB:
        mon_say('V', "screen picture", NULL);  /* the monitor counts these on one line */
        break;
    case CMD_UITREE:
        mon_say('C', "window tree", NULL);
        break;
    case CMD_MENUS:
        mon_say('C', "menus", NULL);
        break;
    case CMD_SCREENS:
        mon_say('C', "screens", NULL);
        break;
    }
    switch (c[0]) {
    case CMD_FRAME:
        frame_level = c[1] & 16 ? 2 : c[1] & 8 ? 1 : 0;
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
        if (c[1] & 1) {  /* from 1.5.0 (0x8000 in SCREENS): aim FRAME_RGB, move nothing */
            aim = (ULONG)x << 16 | y;
            if (aim && !open_screen(aim)) {
                aim = 0;
                return reply(1);
            }
            return reply(0);
        }
        return screen_front((ULONG)x << 16 | y);
    case CMD_FRAME_RGB:
        return frame_rgb(c[1] & 1, x, y, w, h);
    case CMD_UITREE:
        return ui_tree((ULONG)x << 16 | y, (c[1] & 1) != 0);
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

        mon_stats();
        for (i = 0; i < PENDING && !(pend[i].fd >= 0 && pend[i].screen); i++)
            ;
        if (i < PENDING) {  /* the newcomer wins */
            drop_client();
            client = pend[i].fd;
            client_addr = pend[i].addr;
            client_since = now_ms();
            pend[i].fd = -1;
            authed = TRUE;
            mon_say('S', "screen link up", NULL);
            client_id = 0;
            conn_serial++;  /* a new face: pref is not its picture yet */
            heard = now_ms();
            if (!command(pend[i].cmd) || !send_acks())
                lose_client();
            continue;
        }
        if (client >= 0 && now_ms() - heard > IDLE_SECONDS * 1000)
            lose_client();  /* idle */
        if (client >= 0 && in_have == sizeof(in_cmd)) {  /* came in during the last reply */
            in_have = 0;
            if (!command(in_cmd) || !send_acks())
                lose_client();
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
            lose_client();
            continue;
        }
        heard = now_ms();
        in_have += n;
        if (in_have == sizeof(in_cmd)) {
            in_have = 0;
            if (!command(in_cmd) || !send_acks())
                lose_client();
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

#ifdef __VBCC__
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
#else
#define OpenAmiSSLTags(v, ...) ({ ULONG _t[] = {__VA_ARGS__}; \
    LP2(60, LONG, OpenAmiSSLTagList, LONG, (v), d0, struct TagItem *, (struct TagItem *)_t, a0, \
        struct Library *, AmiSSLMasterBase); })
#define CloseAmiSSL() LP0NR(42, CloseAmiSSL, struct Library *, AmiSSLMasterBase)
#define InitAmiSSL(...) ({ ULONG _t[] = {__VA_ARGS__}; \
    LP1(36, LONG, InitAmiSSLA, struct TagItem *, (struct TagItem *)_t, a0, struct Library *, AmiSSLBase); })
#define CleanupAmiSSL(...) ({ ULONG _t[] = {__VA_ARGS__}; \
    LP1(42, LONG, CleanupAmiSSLA, struct TagItem *, (struct TagItem *)_t, a0, struct Library *, AmiSSLBase); })
#define TLS_server_method() LP0(26946, const SSL_METHOD *, TLS_server_method, struct Library *, AmiSSLBase)
#define SSL_CTX_new(m) LP1(8208, SSL_CTX *, SSL_CTX_new, const SSL_METHOD *, (m), a0, struct Library *, AmiSSLBase)
#define SSL_CTX_free(a) LP1NR(8214, SSL_CTX_free, SSL_CTX *, (a), a0, struct Library *, AmiSSLBase)
#define SSL_CTX_use_certificate_chain_file(c, f) LP2(8508, int, SSL_CTX_use_certificate_chain_file, \
    SSL_CTX *, (c), a0, const char *, (f), a1, struct Library *, AmiSSLBase)
#define SSL_CTX_use_PrivateKey_file(c, f, t) LP3(8496, int, SSL_CTX_use_PrivateKey_file, \
    SSL_CTX *, (c), a0, const char *, (f), a1, int, (t), d0, struct Library *, AmiSSLBase)
#define SSL_CTX_check_private_key(c) LP1(8766, int, SSL_CTX_check_private_key, \
    const SSL_CTX *, (c), a0, struct Library *, AmiSSLBase)
#define SSL_new(c) LP1(8784, SSL *, SSL_new, SSL_CTX *, (c), a0, struct Library *, AmiSSLBase)
#define SSL_free(s) LP1NR(8820, SSL_free, SSL *, (s), a0, struct Library *, AmiSSLBase)
#define SSL_set_fd(s, fd) LP2(8358, int, SSL_set_fd, SSL *, (s), a0, int, (fd), d0, struct Library *, AmiSSLBase)
#define SSL_accept(s) LP1(8826, int, SSL_accept, SSL *, (s), a0, struct Library *, AmiSSLBase)
#define SSL_read(s, b, n) LP3(8838, int, SSL_read, SSL *, (s), a0, void *, (b), a1, int, (n), d0, \
    struct Library *, AmiSSLBase)
#define SSL_write(s, b, n) LP3(8850, int, SSL_write, SSL *, (s), a0, const void *, (b), a1, int, (n), d0, \
    struct Library *, AmiSSLBase)
#define SSL_shutdown(s) LP1(8994, int, SSL_shutdown, SSL *, (s), a0, struct Library *, AmiSSLBase)
#define SSL_pending(s) LP1(8352, int, SSL_pending, const SSL *, (s), a0, struct Library *, AmiSSLBase)
#endif

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
#ifdef AMIGENT_APOLLO
    /* The music task waits on this signal between halves instead of spinning. */
    if (audio_wait && audio_task)
        Signal(audio_task, SIGBREAKF_CTRL_D);
#endif
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

static void AMIGENT_SAVEDS pump_main(void)
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
        if (buf && InitAmiSSL(AmiSSL_SocketBase, (ULONG)SocketBase, AmiSSL_ErrNoPtr, (ULONG)&err, TAG_DONE) == 0) {
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

    if ((fd = accept(listen_fd, (struct sockaddr *)&addr, AMIGENT_SOCKLEN(&len))) < 0)
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
static void AMIGENT_SAVEDS tls_main(void)
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
    if (found && held()) {  /* ScreenToFront would wait for as long as that lasts */
        found = NULL;
        t->len = 0;
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

/* BUSY: why clicks and keys would not get through now. "menu" while a menu
 * is open, "held" while Intuition holds back new windows (a popup left open);
 * empty when neither. */
static void rexx_busy(struct text *t)
{
    char h = held();

    if (h)
        t_str(t, (const UBYTE *)(h == 'm' ? "menu" : "held"), FALSE);
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
        else if (is_word(s, "BUSY"))
            rexx_busy(&t);
        else if (is_word(s, "HELP"))
            t_str(&t, (const UBYTE *)"STATUS CLIENTS VERSION PAUSE RESUME ACTIVATEWINDOW TYPE KEY BUSY HELP QUIT", FALSE);
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

    /* After an amigentd that served many connections quits, the TCP stack can
     * keep its port taken (EADDRINUSE, SO_REUSEADDR or not) for about a minute
     * (seen with AmiNetXDuo): 90 s. */
    for (tries = 0; tries < 45 && !broken(); tries++) {
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
            if ((v = FindToolType((CONST_STRPTR *)tt, "PORT")) && StrToLong(v, &n) > 0)
                *port = n;
            if (FindToolType((CONST_STRPTR *)tt, "SETUP"))
                setup_mode = TRUE;
            if (FindToolType((CONST_STRPTR *)tt, "QUIET"))
                quiet = TRUE;
            if (FindToolType((CONST_STRPTR *)tt, "TLSONLY"))
                tls_only = TRUE;
            if (FindToolType((CONST_STRPTR *)tt, "NOTEXT"))
                no_text = TRUE;
            if (FindToolType((CONST_STRPTR *)tt, "NODEBUG"))
                no_debug = TRUE;
            if (FindToolType((CONST_STRPTR *)tt, "ALLOWPOKE"))
                allow_poke = TRUE;
            if ((v = FindToolType((CONST_STRPTR *)tt, "KEY")))
                set_key_file((const char *)v);
            FreeDiskObject(icon);
        }
        CurrentDir(old);
    }
    CloseLibrary(IconBase);
    IconBase = NULL;
}

/* FOLLOW pipe TO file: copy what a job writes into its pipe to a file, a piece at a
 * time with the file closed in between, so the PC can read it while the job runs
 * (a Shell redirection holds its file exclusively until the command ends). At the
 * end, file.rc (the job's return code, written by the job script) becomes
 * file.done, so the PC never sees "done" before the last piece. */
static int follow(const char *from, const char *to)
{
    UBYTE *buf = AllocVec(4096, MEMF_ANY);
    char *rc = AllocVec(strlen(to) + 8, MEMF_ANY), *done = AllocVec(strlen(to) + 8, MEMF_ANY);
    BPTR in = buf && rc && done ? Open(from, MODE_OLDFILE) : 0, out;
    LONG n, tries;

    if (!in) {
        FreeVec(buf);
        FreeVec(rc);
        FreeVec(done);
        return RETURN_FAIL;
    }
    strcpy(rc, to);
    strcpy(rc + strlen(rc) - 3, "rc");  /* x.out -> x.rc */
    strcpy(done, to);
    strcpy(done + strlen(done) - 3, "done");
    /* A pipe answers a read only when that much is there or the writer is done,
     * so a byte at a time (that costs little next to the job), and each line
     * goes out as soon as it is complete. */
    n = 0;
    for (;;) {
        LONG got = Read(in, buf + n, 1);

        if (got > 0)
            n++;
        if (n && (got <= 0 || buf[n - 1] == '\n' || n == 4096)) {
            for (tries = 0; tries < 250; tries++) {  /* the PC may be reading it: 5 s */
                if ((out = Open(to, MODE_READWRITE))) {
                    Seek(out, 0, OFFSET_END);
                    Write(out, buf, n);
                    Close(out);
                    break;
                }
                Delay(1);
            }
            n = 0;
        }
        if (got <= 0)
            break;
    }
    Close(in);
    for (tries = 0; tries < 1500; tries++) {  /* the script writes rc right after: 30 s */
        BPTR lock = Lock(rc, SHARED_LOCK);
        if (lock) {
            UnLock(lock);
            Rename(rc, done);
            break;
        }
        Delay(1);
    }
    FreeVec(buf);
    FreeVec(rc);
    FreeVec(done);
    return RETURN_OK;
}

int main(int argc, char **argv)
{
    LONG args[11] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    struct RDArgs *rda;
    UWORD port = DEFAULT_PORT;
    BOOL twin;
    int rc = RETURN_FAIL;
    LONG i, n;

    if (argc == 0) {  /* Workbench: argv is the WBStartup message */
        from_wb = TRUE;
        tool_types((struct WBStartup *)argv, &port);
    } else {
        rda = ReadArgs("PORT/K/N,SETUP/S,KEY/K,QUIET/S,TLSONLY/S,NOTEXT/S,NODEBUG/S,FOLLOW/K,TO/K,"
                       "ALLOWPOKE/S,EMU68JIT/K", args, NULL);
        if (!rda) {
            PrintFault(IoErr(), "amigentd");
            return RETURN_FAIL;
        }
        if (args[7] && args[8]) {  /* a job's helper, not the daemon */
            rc = follow((const char *)args[7], (const char *)args[8]);
            FreeArgs(rda);
            return rc;
        }
        if (args[10]) {  /* a job's helper: JIT settings around a program */
            rc = emu68_cli((const char *)args[10]);
            FreeArgs(rda);
            return rc;
        }
        if (args[0])
            port = *(LONG *)args[0];
        setup_mode = args[1] != 0;
        if (args[2])
            set_key_file((const char *)args[2]);
        quiet = args[3] != 0;
        tls_only = args[4] != 0;
        no_text = args[5] != 0;
        no_debug = args[6] != 0;
        allow_poke = args[9] != 0;
        FreeArgs(rda);
    }

    server = FindTask(NULL);
    find_self(argc, argv);
    cpu_cache = SysBase->AttnFlags & (AFF_68040 | AFF_68060) ? 2 :
                SysBase->AttnFlags & (AFF_68020 | AFF_68030) ? 1 : 0;
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
    tables_init();
    zz_open();  /* a ZZ9000 looks at its own screen */
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
    probe_sig = AllocSignal(-1);  /* without it there are no probes, nothing else lacks */
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
    /* Without layers.library the text of closed windows would stay: no text then. */
    if (!(LayersBase = OpenLibrary("layers.library", 37)))
        no_text = TRUE;
    hooks_start();
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
    hooks_stop();  /* no channel looks at the text any more */
    traces_off();
    z_free(frame_z);
    if (LayersBase)
        CloseLibrary(LayersBase);
    rexx_close();
    if (chan_gone >= 0)
        FreeSignal(chan_gone);
    if (probe_sig >= 0)
        FreeSignal(probe_sig);
    if (reader) {
        reader_quit = TRUE;
        Signal(reader, SIGBREAKF_CTRL_C);
        Wait(1UL << reader_gone);
    }
    if (reader_gone >= 0)
        FreeSignal(reader_gone);
    ((struct Process *)server)->pr_WindowPtr = old_window;
    free_frame();
    free_planar();
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
    zz_close();
    if (CyberGfxBase) CloseLibrary(CyberGfxBase);
    if (KeymapBase) CloseLibrary(KeymapBase);
    if (GfxBase) CloseLibrary((struct Library *)GfxBase);
    if (IntuitionBase) CloseLibrary((struct Library *)IntuitionBase);
    return rc;
}
