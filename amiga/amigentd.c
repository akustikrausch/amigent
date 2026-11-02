/* amigentd - eyes and hands for amigent.
 *
 * Usage: amigentd [SETUP]
 * SETUP also accepts files and commands, for the first install from the PC.
 * Build: see build-amigentd.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <exec/io.h>
#include <devices/input.h>
#include <devices/inputevent.h>
#include <intuition/intuitionbase.h>
#include <intuition/screens.h>
#include <dos/dos.h>
#include <dos/dostags.h>
#include <utility/tagitem.h>
#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/graphics.h>
#include <proto/keymap.h>
#include <inline/cybergraphics_protos.h>
#include <proto/socket.h>
#include <sys/socket.h>
#include <devices/timer.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <string.h>

#define RECTFMT_RGB 0 /* cybergraphics.library, header not in the NDK */
#define PORT 6801
#define VERSION 1
#define TILE 64
#define IO_TIMEOUT_SECS 10
#define IDLE_TIMEOUT_SECS 120
#define PRIORITY 2
#define STARTUP_TRIES 60

enum { CMD_INFO = 1, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING, CMD_CHAR,
       CMD_PUT, CMD_RUN, CMD_SETUP_END };

const char version[] = "$VER: amigentd 0.1 (02.11.2026)";

struct Library *SocketBase;
struct Library *CyberGfxBase;
struct Library *KeymapBase;
struct IntuitionBase *IntuitionBase;
struct GfxBase *GfxBase;

static struct MsgPort *input_port;
static struct IOStdReq *input_req;
static UBYTE *last_frame, *tile_rgb, *tile_332, *tile_rle;
static UWORD screen_w, screen_h;
static UBYTE buttons;
static UWORD pointer_x, pointer_y;
static BOOL quit;
static BOOL setup_mode;
static UBYTE io_buffer[8192];

static BOOL send_all(LONG s, const void *data, LONG len)
{
    const UBYTE *p = data;
    while (len > 0) {
        LONG n = send(s, (APTR)p, len, 0);
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
    }
    return TRUE;
}

static BOOL recv_all(LONG s, void *data, LONG len)
{
    UBYTE *p = data;
    while (len > 0) {
        LONG n = recv(s, p, len, 0);
        if (n <= 0)
            return FALSE;
        p += n;
        len -= n;
    }
    return TRUE;
}

static void put16(UBYTE *p, UWORD v)
{
    p[0] = v >> 8;
    p[1] = v;
}

static void put32(UBYTE *p, ULONG v)
{
    p[0] = v >> 24;
    p[1] = v >> 16;
    p[2] = v >> 8;
    p[3] = v;
}

static struct Screen *front_screen(void)
{
    return IntuitionBase->FirstScreen;
}

static BOOL update_size(void)
{
    struct Screen *scr = front_screen();
    ULONG size;

    if (scr->Width == screen_w && scr->Height == screen_h)
        return TRUE;
    if (last_frame)
        FreeVec(last_frame);
    screen_w = scr->Width;
    screen_h = scr->Height;
    size = (ULONG)screen_w * screen_h;
    last_frame = AllocVec(size, MEMF_ANY | MEMF_CLEAR);
    return last_frame != NULL;
}

/* Read one tile as RGB332. */
static void read_tile(struct Screen *scr, UWORD x, UWORD y, UWORD w, UWORD h)
{
    ULONG i, count = (ULONG)w * h;
    UBYTE *src = tile_rgb, *dst = tile_332;

    ReadPixelArray(tile_rgb, 0, 0, w * 3, &scr->RastPort, x, y, w, h, RECTFMT_RGB);
    for (i = 0; i < count; i++, src += 3)
        *dst++ = (src[2] >> 6) << 6 | (src[1] >> 5) << 3 | (src[0] >> 5);
}

static ULONG rle_encode(UWORD w, UWORD h)
{
    ULONG count = (ULONG)w * h, i = 0, out = 0;

    while (i < count) {
        UBYTE colour = tile_332[i];
        ULONG run = 1;
        while (i + run < count && run < 256 && tile_332[i + run] == colour)
            run++;
        if (out + 2 > count)
            return count;
        tile_rle[out++] = run - 1;
        tile_rle[out++] = colour;
        i += run;
    }
    return out;
}

static BOOL tile_changed(UWORD x, UWORD y, UWORD w, UWORD h)
{
    UWORD row;
    BOOL changed = FALSE;

    for (row = 0; row < h; row++) {
        UBYTE *old = last_frame + (ULONG)(y + row) * screen_w + x;
        UBYTE *new = tile_332 + (ULONG)row * w;
        if (memcmp(old, new, w)) {
            memcpy(old, new, w);
            changed = TRUE;
        }
    }
    return changed;
}

/* Frame replies buffer the tile list so the count can lead. */
static BOOL send_frame(LONG s, UBYTE force, UWORD rx, UWORD ry, UWORD rw, UWORD rh)
{
    struct Screen *scr = front_screen();
    ULONG tiles = 0;
    UBYTE head[12];
    UWORD x, y;
    ULONG capacity = (ULONG)(rw / TILE + 2) * (rh / TILE + 2);
    UBYTE *flags;

    if (!update_size())
        return FALSE;
    if (rx >= screen_w || ry >= screen_h)
        rw = rh = 0;
    if (rx + rw > screen_w)
        rw = screen_w - rx;
    if (ry + rh > screen_h)
        rh = screen_h - ry;

    flags = AllocVec(capacity, MEMF_ANY | MEMF_CLEAR);
    if (!flags)
        return FALSE;

    /* First pass finds the changed tiles and updates the reference image. */
    {
        ULONG index = 0;
        for (y = ry; y < ry + rh; y += TILE) {
            for (x = rx; x < rx + rw; x += TILE, index++) {
                UWORD w = rx + rw - x < TILE ? rx + rw - x : TILE;
                UWORD h = ry + rh - y < TILE ? ry + rh - y : TILE;
                read_tile(scr, x, y, w, h);
                if (tile_changed(x, y, w, h) || force) {
                    flags[index] = 1;
                    tiles++;
                }
            }
        }
    }

    put32(head, tiles);
    if (!send_all(s, head, 4)) {
        FreeVec(flags);
        return FALSE;
    }

    {
        ULONG index = 0;
        for (y = ry; y < ry + rh; y += TILE) {
            for (x = rx; x < rx + rw; x += TILE, index++) {
                UWORD w = rx + rw - x < TILE ? rx + rw - x : TILE;
                UWORD h = ry + rh - y < TILE ? ry + rh - y : TILE;
                ULONG size;
                UWORD row;
                const UBYTE *payload;

                if (!flags[index])
                    continue;
                for (row = 0; row < h; row++)
                    memcpy(tile_332 + (ULONG)row * w,
                           last_frame + (ULONG)(y + row) * screen_w + x, w);
                size = rle_encode(w, h);
                payload = size < (ULONG)w * h ? tile_rle : tile_332;
                put16(head, x);
                put16(head + 2, y);
                put16(head + 4, w);
                put16(head + 6, h);
                put32(head + 8, size);
                if (!send_all(s, head, 12) || !send_all(s, payload, size)) {
                    FreeVec(flags);
                    return FALSE;
                }
            }
        }
    }
    FreeVec(flags);
    return TRUE;
}

static void write_event(struct InputEvent *ev)
{
    input_req->io_Command = IND_WRITEEVENT;
    input_req->io_Data = ev;
    input_req->io_Length = sizeof(*ev);
    DoIO((struct IORequest *)input_req);
}

static void send_pointer(UWORD x, UWORD y)
{
    struct IEPointerPixel pos;
    struct InputEvent ev;

    memset(&ev, 0, sizeof(ev));
    pos.iepp_Screen = front_screen();
    pos.iepp_Position.X = x;
    pos.iepp_Position.Y = y;
    ev.ie_Class = IECLASS_NEWPOINTERPOS;
    ev.ie_SubClass = IESUBCLASS_PIXEL;
    ev.ie_Code = IECODE_NOBUTTON;
    ev.ie_EventAddress = &pos;
    write_event(&ev);
    pointer_x = x;
    pointer_y = y;
}

static void send_button(UBYTE mask)
{
    static const struct { UBYTE bit; UWORD code; } map[] = {
        { 1, IECODE_LBUTTON }, { 2, IECODE_RBUTTON }, { 4, IECODE_MBUTTON }
    };
    UBYTE changed = mask ^ buttons;
    int i;

    for (i = 0; i < 3; i++) {
        struct InputEvent ev;
        if (!(changed & map[i].bit))
            continue;
        memset(&ev, 0, sizeof(ev));
        ev.ie_Class = IECLASS_RAWMOUSE;
        ev.ie_Code = map[i].code | (mask & map[i].bit ? 0 : IECODE_UP_PREFIX);
        write_event(&ev);
    }
    buttons = mask;
}

static void send_key(UBYTE down, UWORD code, UWORD qualifier)
{
    struct InputEvent ev;

    memset(&ev, 0, sizeof(ev));
    ev.ie_Class = IECLASS_RAWKEY;
    ev.ie_Code = code | (down ? 0 : IECODE_UP_PREFIX);
    ev.ie_Qualifier = qualifier;
    write_event(&ev);
}

/* Type one Latin-1 character through the active keymap. */
static UBYTE send_char(UBYTE ch, UWORD extra)
{
    UBYTE text = ch, events[8];
    LONG count = MapANSI(&text, 1, events, 4, NULL), i;

    if (count <= 0)
        return 1;
    for (i = 0; i < count; i++) {
        UWORD qualifier = events[i * 2 + 1] | extra;
        send_key(1, events[i * 2], qualifier);
        send_key(0, events[i * 2], qualifier);
    }
    return 0;
}

/* Setup mode only: store a file. The path follows, then the data. */
static BOOL handle_put(LONG s, UWORD path_len, ULONG size, UBYTE *status)
{
    char path[256];
    BPTR file;
    BOOL write_ok = TRUE;

    if (path_len >= sizeof(path) || !recv_all(s, path, path_len))
        return FALSE;
    path[path_len] = 0;
    file = Open(path, MODE_NEWFILE);
    write_ok = file != 0;
    while (size > 0) {
        LONG chunk = size < sizeof(io_buffer) ? size : sizeof(io_buffer);
        if (!recv_all(s, io_buffer, chunk))
            return FALSE;
        if (write_ok && Write(file, io_buffer, chunk) != chunk)
            write_ok = FALSE;
        size -= chunk;
    }
    if (file)
        Close(file);
    *status = write_ok ? 0 : 1;
    return TRUE;
}

/* Setup mode only: run a command line, answer with its return code. */
static BOOL handle_run(LONG s, UWORD len, UBYTE *status)
{
    char command[512];
    LONG rc;

    if (len >= sizeof(command) || !recv_all(s, command, len))
        return FALSE;
    command[len] = 0;
    rc = SystemTags(command, TAG_DONE);
    *status = rc < 0 ? 255 : rc > 254 ? 254 : rc;
    return TRUE;
}

/* Returns FALSE to drop this client: a new one is waiting, it idled out, or Ctrl-C. */
static BOOL wait_command(LONG server, LONG client)
{
    struct timeval idle = { IDLE_TIMEOUT_SECS, 0 };
    ULONG signals = SIGBREAKF_CTRL_C;
    fd_set fds;
    LONG top = (server > client ? server : client) + 1;

    FD_ZERO(&fds);
    FD_SET(server, &fds);
    FD_SET(client, &fds);
    if (WaitSelect(top, &fds, NULL, NULL, &idle, &signals) <= 0) {
        if (signals & SIGBREAKF_CTRL_C)
            quit = TRUE;
        return FALSE;
    }
    if (FD_ISSET(server, &fds))
        return FALSE;
    return (BOOL)FD_ISSET(client, &fds);
}

static void serve_client(LONG server, LONG s)
{
    UBYTE cmd[10], reply[8];

    memcpy(reply, "AMGT", 4);
    put16(reply + 4, VERSION);
    put16(reply + 6, 0);
    if (!send_all(s, reply, 8))
        return;

    while (wait_command(server, s) && recv_all(s, cmd, 10)) {
        UWORD a = cmd[2] << 8 | cmd[3], b = cmd[4] << 8 | cmd[5];
        UWORD c = cmd[6] << 8 | cmd[7], d = cmd[8] << 8 | cmd[9];
        UBYTE status = 0;

        switch (cmd[0]) {
        case CMD_INFO:
            update_size();
            put16(reply, screen_w);
            put16(reply + 2, screen_h);
            put16(reply + 4, TILE);
            put16(reply + 6, 0);
            if (!send_all(s, reply, 8))
                return;
            continue;
        case CMD_FRAME:
            if (!send_frame(s, cmd[1], a, b, c, d))
                return;
            continue;
        case CMD_MOVE:
            send_pointer(a, b);
            break;
        case CMD_BUTTON:
            send_button(cmd[1]);
            break;
        case CMD_RAWKEY:
            send_key(cmd[1], a, b);
            break;
        case CMD_CHAR:
            status = send_char(a, b);
            break;
        case CMD_PING:
            break;
        case CMD_PUT:
            if (!setup_mode)
                status = 1;
            else if (!handle_put(s, a, (ULONG)c << 16 | d, &status))
                return;
            break;
        case CMD_RUN:
            if (!setup_mode)
                status = 1;
            else if (!handle_run(s, a, &status))
                return;
            break;
        case CMD_SETUP_END:
            setup_mode = FALSE;
            break;
        default:
            status = 1;
        }
        if (!send_all(s, &status, 1))
            return;
    }
}

static BOOL open_all(void)
{
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 39);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 39);
    CyberGfxBase = OpenLibrary("cybergraphics.library", 41);
    KeymapBase = OpenLibrary("keymap.library", 37);
    SocketBase = OpenLibrary("bsdsocket.library", 4);
    if (!IntuitionBase || !GfxBase || !CyberGfxBase || !KeymapBase || !SocketBase)
        return FALSE;
    input_port = CreateMsgPort();
    if (!input_port)
        return FALSE;
    input_req = (struct IOStdReq *)CreateIORequest(input_port, sizeof(struct IOStdReq));
    if (!input_req)
        return FALSE;
    if (OpenDevice("input.device", 0, (struct IORequest *)input_req, 0)) {
        input_req->io_Device = NULL;
        return FALSE;
    }
    tile_rgb = AllocVec(TILE * TILE * 3, MEMF_ANY);
    tile_332 = AllocVec(TILE * TILE, MEMF_ANY);
    tile_rle = AllocVec(TILE * TILE, MEMF_ANY);
    return tile_rgb && tile_332 && tile_rle;
}

static void close_all(void)
{
    if (last_frame) FreeVec(last_frame);
    if (tile_rgb) FreeVec(tile_rgb);
    if (tile_332) FreeVec(tile_332);
    if (tile_rle) FreeVec(tile_rle);
    last_frame = tile_rgb = tile_332 = tile_rle = NULL;
    screen_w = screen_h = 0;
    if (input_req) {
        if (input_req->io_Device)
            CloseDevice((struct IORequest *)input_req);
        DeleteIORequest((struct IORequest *)input_req);
        input_req = NULL;
    }
    if (input_port) DeleteMsgPort(input_port);
    input_port = NULL;
    if (SocketBase) CloseLibrary(SocketBase);
    if (KeymapBase) CloseLibrary(KeymapBase);
    if (CyberGfxBase) CloseLibrary(CyberGfxBase);
    if (GfxBase) CloseLibrary((struct Library *)GfxBase);
    if (IntuitionBase) CloseLibrary((struct Library *)IntuitionBase);
    SocketBase = KeymapBase = CyberGfxBase = NULL;
    GfxBase = NULL;
    IntuitionBase = NULL;
}

/* At boot the network and graphics libraries may not be there yet. */
static BOOL open_with_retry(void)
{
    int tries;

    for (tries = 0; tries < STARTUP_TRIES; tries++) {
        if (open_all())
            return TRUE;
        close_all();
        if (SetSignal(0, 0) & SIGBREAKF_CTRL_C)
            break;
        Delay(100);
    }
    return FALSE;
}

int main(int argc, char **argv)
{
    struct sockaddr_in addr;
    struct timeval timeout = { IO_TIMEOUT_SECS, 0 };
    struct timeval idle = { IDLE_TIMEOUT_SECS, 0 };
    LONG server, one = 1;
    int rc = RETURN_FAIL;

    /* Stay responsive while a heavy pri 0 command such as List ALL runs. */
    SetTaskPri(FindTask(NULL), PRIORITY);
    setup_mode = argc > 1 && (argv[1][0] | 32) == 's';
    if (!open_with_retry()) {
        PutStr("amigentd: cannot open libraries or input.device\n");
        close_all();
        return rc;
    }

    server = socket(AF_INET, SOCK_STREAM, 0);
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = PORT;
    addr.sin_addr.s_addr = INADDR_ANY;
    if (bind(server, (struct sockaddr *)&addr, sizeof(addr)) < 0 || listen(server, 2) < 0) {
        PutStr("amigentd: cannot bind port\n");
        CloseSocket(server);
        close_all();
        return rc;
    }

    PutStr("amigentd: ready\n");
    while (!quit) {
        ULONG signals = SIGBREAKF_CTRL_C;
        fd_set fds;
        LONG client;

        FD_ZERO(&fds);
        FD_SET(server, &fds);
        if (WaitSelect(server + 1, &fds, NULL, NULL, NULL, &signals) < 0)
            break;
        if (signals & SIGBREAKF_CTRL_C)
            break;
        if (!FD_ISSET(server, &fds))
            continue;

        client = accept(server, NULL, NULL);
        if (client < 0)
            continue;
        setsockopt(client, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
        setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &idle, sizeof(idle));
        serve_client(server, client);
        send_button(0);
        CloseSocket(client);
    }

    CloseSocket(server);
    close_all();
    return RETURN_OK;
}
