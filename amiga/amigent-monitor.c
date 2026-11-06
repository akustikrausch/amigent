/*
 * amigent-monitor: a window that shows, line by line, what the agent does on
 * this Amiga (the tools it calls, the commands, files, clicks and keys that
 * reach amigentd) and next to it the link: data rates with the last minute
 * as a graph, totals, how much the agent did, free memory.
 *
 *   amigent-monitor [LEFT <n>] [TOP <n>] [WIDTH <n>] [HEIGHT <n>]
 *                   [NAME <your name>] [FONT <name>] [SIZE <n>]
 *
 * amigentd sends its lines to the public port "Amigent.monitor" while it is
 * open, and once a second its byte counters; nothing goes out while no monitor
 * runs. The window never takes the input focus, so what the agent types still
 * goes where it should. Esc or the close gadget ends it, C clears the log.
 * From Workbench the same come as tool types.
 *
 * OS 2.04 and up, any screen: with 16 colours or more on OS 3 it takes its own
 * shades of green, on a 4 or 8 colour Workbench the screen's pens. The window
 * fits itself to the screen and leaves out what has no room. It draws only
 * what changed, five times a second at most. MIT licence, see LICENSE.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <exec/ports.h>
#include <dos/dos.h>
#include <intuition/intuition.h>
#include <intuition/screens.h>
#include <graphics/gfx.h>
#include <graphics/gfxbase.h>
#include <graphics/rastport.h>
#include <graphics/text.h>
#include <graphics/view.h>
#include <workbench/startup.h>
#include <workbench/workbench.h>
#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/graphics.h>
#include <proto/diskfont.h>
#include <proto/icon.h>
#include <string.h>

const char version[] = "$VER: amigent-monitor 1.3.0 (06.11.2026)";

struct IntuitionBase *IntuitionBase;
struct GfxBase *GfxBase;
struct Library *DiskfontBase, *IconBase;

#define AUTHOR "AKUSTIKRAUSCH"
#define MON_PORT "Amigent.monitor"
#define MON_TEXT 160
#define LINES 256  /* kept; more than any window shows */
#define LINE_LEN (MON_TEXT + 32)
#define HISTORY 60  /* seconds in the graphs */
#define TICK 10     /* the window looks five times a second */

struct monmsg {  /* as amigentd sends it */
    struct Message m;
    UBYTE kind;
    UBYTE pad;
    char text[2];
};

struct line {
    UBYTE kind;
    UWORD count;
    char time[9];
    char text[LINE_LEN];
};

static LONG opt_left = -1, opt_top = -1, opt_w = 760, opt_h = 480, opt_size = 8;
static char opt_name[48] = "";  /* who watches: the greeting */
static char opt_font[48] = "topaz.font";

/* ---- pens -------------------------------------------------------------- */

static struct ColorMap *cmap;
static LONG pens[16], npens;
static BOOL own_pens;
static LONG black, panel, dim, mid, green, bright, white, cyan, yellow, red, gray;

static LONG pen(ULONG rgb)
{
    LONG p = ObtainBestPen(cmap, (rgb >> 16 & 255) * 0x01010101UL, (rgb >> 8 & 255) * 0x01010101UL,
                           (rgb & 255) * 0x01010101UL, OBP_Precision, PRECISION_IMAGE, TAG_END);
    if (p >= 0 && npens < 16)
        pens[npens++] = p;
    return p < 0 ? 1 : p;
}

/* Shades of green where there are colours to spare; else the Workbench's own
 * four, which every screen has: black background, white text, blue accents. */
static void get_pens(struct Screen *scr)
{
    struct DrawInfo *di = GetScreenDrawInfo(scr);
    LONG depth = scr->RastPort.BitMap->Depth, fg = 1, bg = 0, hi = 2, fill = 3;

    if (GfxBase->LibNode.lib_Version >= 39 && depth >= 4) {
        own_pens = TRUE;
        black = pen(0x000000);
        panel = pen(0x050F08);
        dim = pen(0x1C5A2A);
        mid = pen(0x2A9A45);
        green = pen(0x33FF66);
        bright = pen(0xA8FFB8);
        white = pen(0xEFFFEF);
        cyan = pen(0x5FE8D0);
        yellow = pen(0xFFE060);
        red = pen(0xFF5050);
        gray = pen(0x6A7A6E);
    } else {
        if (di && di->dri_NumPens > FILLPEN) {
            fg = di->dri_Pens[TEXTPEN];
            bg = di->dri_Pens[BACKGROUNDPEN];
            hi = di->dri_Pens[SHINEPEN];
            fill = di->dri_Pens[FILLPEN];
        }
        /* OS 2/3 Workbench: 0 grey, 1 black, 2 white, 3 blue. The text pen is
         * the dark one; it becomes the background. */
        black = panel = fg;
        white = bright = green = yellow = hi;
        dim = mid = gray = cyan = red = fill;
        if (fill == fg || fill == hi)
            dim = mid = gray = cyan = red = bg;
    }
    if (di)
        FreeScreenDrawInfo(scr, di);
}

/* ---- numbers ----------------------------------------------------------- */

static char *put_dec(char *p, ULONG v)
{
    char tmp[12];
    LONG n = 0;

    do tmp[n++] = '0' + v % 10; while ((v /= 10));
    while (n)
        *p++ = tmp[--n];
    *p = 0;
    return p;
}

/* 812 B, 12.3 KB, 4.56 MB, 1.20 GB */
static void put_bytes(char *p, ULONG v)
{
    static const char *const unit[] = {" KB", " MB", " GB"};
    ULONG whole, frac;
    LONG u = 0;

    if (v < 1024) {
        strcpy(put_dec(p, v), " B");
        return;
    }
    while (v >= 1024UL * 1024 && u < 2) {
        v >>= 10;
        u++;
    }
    whole = v >> 10;
    frac = (v & 1023) * 100 >> 10;
    p = put_dec(p, whole);
    if (whole < 100) {
        *p++ = '.';
        *p++ = '0' + frac / 10;
        if (whole < 10)
            *p++ = '0' + frac % 10;
    }
    strcpy(p, unit[u]);
}

static void put_clock(char *p, ULONG secs)
{
    ULONG m = secs / 60 % 60, s = secs % 60;

    p = put_dec(p, secs / 3600);
    *p++ = ':';
    *p++ = '0' + m / 10;
    *p++ = '0' + m % 10;
    *p++ = ':';
    *p++ = '0' + s / 10;
    *p++ = '0' + s % 10;
    *p = 0;
}

static ULONG seconds_now(void)
{
    struct DateStamp ds;

    DateStamp(&ds);
    return (ULONG)ds.ds_Days * 86400 + ds.ds_Minute * 60 + ds.ds_Tick / TICKS_PER_SECOND;
}

/* ---- what came in ------------------------------------------------------ */

static struct line lines[LINES];
static LONG nlines;  /* ever added; lines[n % LINES] */

static struct {
    ULONG tools, commands, clicks, keys, typed, screens, errors;
    ULONG in, out, last_in, last_out, peak_out;
    UWORD rate_in[HISTORY], rate_out[HISTORY], work[HISTORY];  /* 100 B/s units, requests */
    LONG at;  /* history slot of this second */
    LONG chans;
    BOOL screen, heard;
    ULONG started, link_since, link_seen;
} st;

static void now_text(char *out)
{
    ULONG s = seconds_now() % 86400;

    out[0] = '0' + s / 36000;
    out[1] = '0' + s / 3600 % 10;
    out[2] = ':';
    out[3] = '0' + s / 600 % 6;
    out[4] = '0' + s / 60 % 10;
    out[5] = ':';
    out[6] = '0' + s % 60 / 10;
    out[7] = '0' + s % 10;
    out[8] = 0;
}

static UWORD units(ULONG bytes)
{
    bytes /= 100;
    return bytes > 65535 ? 65535 : (UWORD)bytes;
}

/* "in out channels screen" from amigentd, once a second. */
static void counters(const char *text)
{
    ULONG v[4] = {0, 0, 0, 0}, d_in, d_out;
    LONG i;
    const char *p = text;

    for (i = 0; *p && i < 4; i++) {
        while (*p == ' ')
            p++;
        while (*p >= '0' && *p <= '9')
            v[i] = v[i] * 10 + (*p++ - '0');
    }
    if (!st.heard || v[0] < st.last_in || v[1] < st.last_out) {  /* first, or amigentd started again */
        st.last_in = v[0];
        st.last_out = v[1];
    }
    d_in = v[0] - st.last_in;
    d_out = v[1] - st.last_out;
    st.last_in = v[0];
    st.last_out = v[1];
    st.in += d_in;
    st.out += d_out;
    if (d_out > st.peak_out)
        st.peak_out = d_out;
    st.at = (st.at + 1) % HISTORY;
    st.rate_in[st.at] = units(d_in);
    st.rate_out[st.at] = units(d_out);
    st.work[st.at] = 0;
    st.chans = v[2];
    if (v[2] || v[3]) {
        if (!st.link_since)
            st.link_since = seconds_now();
    } else
        st.link_since = 0;
    st.screen = v[3] != 0;
    st.heard = TRUE;
    st.link_seen = seconds_now();
}

static void add_line(UBYTE kind, const char *text)
{
    struct line *last = nlines ? &lines[(nlines - 1) % LINES] : NULL;
    LONG n;

    switch (kind) {
    case 'K': counters(text); return;
    case 'A': st.tools++; break;
    case 'C': st.commands++; break;
    case 'I': if (text[0] == 'k') st.keys++; else st.clicks++; break;
    case 'T': st.typed++; break;
    case 'V': st.screens++; break;
    case 'E': st.errors++; break;
    }
    if (kind != 'S' && st.work[st.at] < 65535)
        st.work[st.at]++;
    if (last && kind == 'T' && last->kind == 'T' && (n = strlen(last->text)) < 64) {
        strncat(last->text, text, LINE_LEN - n - 1);
        now_text(last->time);
        return;
    }
    if (last && kind == last->kind && !strcmp(text, last->text) && last->count < 65000) {
        last->count++;
        now_text(last->time);
        return;
    }
    last = &lines[nlines++ % LINES];
    last->kind = kind;
    last->count = 1;
    now_text(last->time);
    if (kind == 'T') {
        strcpy(last->text, "typed: ");
        strncat(last->text, text, LINE_LEN - 8);
    } else {
        strncpy(last->text, text, LINE_LEN - 1);
        last->text[LINE_LEN - 1] = 0;
    }
}

static const char *tag_of(UBYTE kind)
{
    switch (kind) {
    case 'A': return "AI  >";
    case 'C': return "CMD >";
    case 'I': return "INPT>";
    case 'T': return "KEYS>";
    case 'V': return "SCRN>";
    case 'S': return "LINK>";
    case 'E': return "ERR!>";
    }
    return "    >";
}

static LONG pen_of(UBYTE kind, LONG age)
{
    BOOL old = age > 5, older = age > 18;

    switch (kind) {
    case 'A': return age == 0 ? white : old ? green : bright;
    case 'I':
    case 'T': return older ? mid : cyan;
    case 'V': return older ? dim : mid;
    case 'S': return older ? mid : yellow;
    case 'E': return red;
    }
    return age == 0 ? bright : older ? mid : green;
}

/* ---- drawing ----------------------------------------------------------- */

static struct Window *win;
static struct TextFont *font, *big;
static LONG fw, fh, base;  /* font cell and baseline */
static LONG W, H, head_h, log_x, log_y, log_w, log_h, side_x, side_w, side_end, stamp;

static void text_at(LONG x, LONG y, LONG fg, LONG bg, const char *s, LONG n)
{
    struct RastPort *rp = win->RPort;

    SetAPen(rp, fg);
    SetBPen(rp, bg);
    SetDrMd(rp, JAM2);
    Move(rp, win->BorderLeft + x, win->BorderTop + y);
    Text(rp, s, n);
}

static void box(LONG x0, LONG y0, LONG x1, LONG y1, LONG p)
{
    if (x1 < x0 || y1 < y0)
        return;
    SetAPen(win->RPort, p);
    RectFill(win->RPort, win->BorderLeft + x0, win->BorderTop + y0, win->BorderLeft + x1,
             win->BorderTop + y1);
}

static void draw_head(void)
{
    struct RastPort *rp = win->RPort;
    char buf[80];
    LONG n, x, y;

    box(0, 0, W - 1, head_h - 1, black);
    SetFont(rp, big);
    if (big == font)
        SetSoftStyle(rp, FSF_BOLD, FSF_BOLD);
    y = 4 + big->tf_Baseline;
    text_at(8, y, green, black, "AMIGENT", 7);
    SetSoftStyle(rp, 0, FSF_BOLD);
    x = 8 + 7 * big->tf_XSize + 2 * fw;
    SetFont(rp, font);
    text_at(x, y, mid, black, "agent link monitor", 18);
    x += 19 * fw;
    strcpy(buf, "coded by " AUTHOR "  1.3.0");
    n = strlen(buf);
    if (W - 8 - n * fw < x)
        n = strlen(strcpy(buf, AUTHOR));
    if (W - 8 - n * fw >= x)
        text_at(W - 8 - n * fw, y, gray, black, buf, n);
    box(6, head_h - 3, W - 7, head_h - 3, dim);
}

static void draw_log(void)
{
    LONG rows = log_h / fh, cols = log_w / fw, r;
    char buf[LINE_LEN + 48];

    if (cols > LINE_LEN + 40)
        cols = LINE_LEN + 40;
    for (r = 0; r < rows; r++) {
        LONG idx = nlines - rows + r, y = log_y + r * fh + base, n, at = stamp + 6;
        struct line *l;

        if (idx < 0 || idx < nlines - LINES) {
            memset(buf, ' ', cols);
            text_at(log_x, y, black, black, buf, cols);
            continue;
        }
        l = &lines[idx % LINES];
        if (stamp)
            text_at(log_x, y, dim, black, l->time, 8);
        text_at(log_x + stamp * fw, y, pen_of(l->kind, nlines - 1 - idx), black, tag_of(l->kind), 5);
        n = strlen(l->text);
        memcpy(buf, l->text, n);
        if (l->count > 1) {
            char num[12];
            LONG v = l->count, k = 11;

            num[k] = 0;
            do num[--k] = '0' + v % 10; while ((v /= 10) && k > 2);
            num[--k] = 'x';
            num[--k] = ' ';
            memcpy(buf + n, num + k, 11 - k);
            n += 11 - k;
        }
        if (n > cols - at)
            n = cols - at;
        memset(buf + n, ' ', cols - at - n);
        text_at(log_x + at * fw, y, pen_of(l->kind, nlines - 1 - idx), black, buf, cols - at);
    }
}

static void draw_cursor(BOOL on)
{
    LONG rows = log_h / fh, cols = log_w / fw, n, x, y;
    struct line *l;

    if (!nlines)
        return;
    l = &lines[(nlines - 1) % LINES];
    n = stamp + 6 + strlen(l->text) + (l->count > 1 ? 7 : 0);
    if (n >= cols)
        return;
    x = log_x + n * fw + 2;
    y = log_y + (rows - 1) * fh;
    box(x, y + 1, x + fw - 2, y + fh - 2, on ? green : black);
}

/* One line of the side panel, label left and value right; nothing past the end. */
static LONG row(LONG y, const char *label, const char *value, LONG fg)
{
    LONG cols = (side_w - 12) / fw, n = strlen(value), l = strlen(label);
    char buf[64];

    if (y + fh > side_end)
        return y;
    if (cols > 63)
        cols = 63;
    if (l > cols)
        l = cols;
    memset(buf, ' ', cols);
    memcpy(buf, label, l);
    if (n <= cols - l - 1)
        memcpy(buf + cols - n, value, n);
    text_at(side_x + 6, y + base, gray, panel, buf, l);
    text_at(side_x + 6 + l * fw, y + base, fg, panel, buf + l, cols - l);
    return y + fh;
}

static LONG title(LONG y, const char *text)
{
    LONG n = strlen(text);

    if (y + fh + 6 > side_end)
        return side_end;
    y += 3;
    box(side_x + 6, y + fh / 2, side_x + side_w - 7, y + fh / 2, dim);
    {
        char buf[24];

        buf[0] = ' ';
        strcpy(buf + 1, text);
        strcat(buf, " ");
        text_at(side_x + 12, y + base, green, panel, buf, n + 2);
    }
    return y + fh + 1;
}

/* The last minute as bars, newest on the right; scaled to its own peak. */
static LONG graph(LONG y, const UWORD *v, LONG fg, const char *label)
{
    LONG gx = side_x + 8, gw = side_w - 16, gh = fh * 2 + 2, i, top = 1, bw;
    char buf[32];

    if (y + gh + 3 > side_end)
        return y;
    for (i = 0; i < HISTORY; i++)
        if (v[i] > top)
            top = v[i];
    box(gx, y, gx + gw - 1, y + gh - 1, black);
    box(gx, y + gh, gx + gw - 1, y + gh, dim);
    bw = gw / HISTORY > 0 ? gw / HISTORY : 1;
    for (i = 0; i < HISTORY; i++) {
        LONG val = v[(st.at + 1 + i) % HISTORY], h = val * (gh - 1) / top;
        LONG x = gx + gw - (HISTORY - i) * bw;

        if (val && !h)
            h = 1;
        if (h && x >= gx)
            box(x, y + gh - h, x + (bw > 2 ? bw - 2 : 0), y + gh - 1, fg);
    }
    put_bytes(buf, (ULONG)top * 100);
    strcat(buf, "/s");
    text_at(gx + 2, y + base, gray, black, label, strlen(label));
    text_at(gx + gw - strlen(buf) * fw - 2, y + base, gray, black, buf, strlen(buf));
    return y + gh + 3;
}

static void draw_side(void)
{
    char buf[40];
    LONG y = log_y - 3, i;
    ULONG now = seconds_now(), sum = 0;

    box(side_x, log_y - 4, side_x + side_w - 1, side_end + 1, panel);
    y = title(y, "LINK");
    if (!st.heard || now - st.link_seen > 3)
        y = row(y, "amigentd", "not heard", red);
    else if (st.link_since) {
        put_clock(buf, now - st.link_since);
        y = row(y, "agent here for", buf, yellow);
    } else
        y = row(y, "agent", "not connected", gray);
    put_dec(buf, st.chans);
    if (st.screen)
        strcat(buf, " + screen");
    y = row(y, "links", buf, bright);

    y = title(y, "TRAFFIC");
    put_bytes(buf, (ULONG)st.rate_out[st.at] * 100);
    strcat(buf, "/s");
    y = row(y, "to the PC", buf, green);
    put_bytes(buf, (ULONG)st.rate_in[st.at] * 100);
    strcat(buf, "/s");
    y = row(y, "from the PC", buf, cyan);
    y = graph(y + 1, st.rate_out, green, "out");
    y = graph(y, st.rate_in, cyan, "in");
    put_bytes(buf, st.out);
    y = row(y, "sent", buf, green);
    put_bytes(buf, st.in);
    y = row(y, "received", buf, cyan);
    put_bytes(buf, st.peak_out);
    strcat(buf, "/s");
    y = row(y, "fastest out", buf, mid);

    y = title(y, "AGENT");
    for (i = 0; i < HISTORY; i++)
        sum += st.work[i];
    put_dec(buf, sum);
    y = row(y, "requests a minute", buf, bright);
    put_dec(buf, st.tools);
    y = row(y, "tools called", buf, white);
    put_dec(buf, st.commands);
    y = row(y, "commands", buf, green);
    put_dec(buf, st.clicks);
    strcat(buf, " / ");
    put_dec(buf + strlen(buf), st.keys);
    y = row(y, "clicks / keys", buf, cyan);
    put_dec(buf, st.typed);
    y = row(y, "typed", buf, cyan);
    put_dec(buf, st.screens);
    y = row(y, "screen reads", buf, mid);
    put_dec(buf, st.errors);
    y = row(y, "failures", buf, st.errors ? red : mid);

    y = title(y, "AMIGA");
    put_bytes(buf, AvailMem(MEMF_CHIP));
    y = row(y, "chip free", buf, bright);
    put_bytes(buf, AvailMem(MEMF_FAST));
    y = row(y, "fast free", buf, bright);
    put_clock(buf, now - st.started);
    row(y, "watching", buf, mid);
}

static void draw_all(void)
{
    box(0, 0, W - 1, H - 1, black);
    draw_head();
    draw_log();
    draw_side();
}

/* ---- options ----------------------------------------------------------- */

static void tool_types(struct WBStartup *wb)
{
    struct DiskObject *dob;
    char *v;
    BPTR old;

    if (!(IconBase = OpenLibrary("icon.library", 36)))
        return;
    old = CurrentDir(wb->sm_ArgList[0].wa_Lock);
    if ((dob = GetDiskObject(wb->sm_ArgList[0].wa_Name))) {
        if ((v = FindToolType(dob->do_ToolTypes, "LEFT"))) StrToLong(v, &opt_left);
        if ((v = FindToolType(dob->do_ToolTypes, "TOP"))) StrToLong(v, &opt_top);
        if ((v = FindToolType(dob->do_ToolTypes, "WIDTH"))) StrToLong(v, &opt_w);
        if ((v = FindToolType(dob->do_ToolTypes, "HEIGHT"))) StrToLong(v, &opt_h);
        if ((v = FindToolType(dob->do_ToolTypes, "SIZE"))) StrToLong(v, &opt_size);
        if ((v = FindToolType(dob->do_ToolTypes, "NAME")))
            strncpy(opt_name, v, sizeof(opt_name) - 1);
        if ((v = FindToolType(dob->do_ToolTypes, "FONT")))
            strncpy(opt_font, v, sizeof(opt_font) - 1);
        FreeDiskObject(dob);
    }
    CurrentDir(old);
    CloseLibrary(IconBase);
}

static BOOL shell_args(void)
{
    LONG a[7] = {0};
    struct RDArgs *rd = ReadArgs("LEFT/K/N,TOP/K/N,WIDTH/K/N,HEIGHT/K/N,NAME/K,FONT/K,SIZE/K/N", a, NULL);

    if (!rd) {
        PrintFault(IoErr(), "amigent-monitor");
        return FALSE;
    }
    if (a[0]) opt_left = *(LONG *)a[0];
    if (a[1]) opt_top = *(LONG *)a[1];
    if (a[2]) opt_w = *(LONG *)a[2];
    if (a[3]) opt_h = *(LONG *)a[3];
    if (a[4]) strncpy(opt_name, (char *)a[4], sizeof(opt_name) - 1);
    if (a[5]) strncpy(opt_font, (char *)a[5], sizeof(opt_font) - 1);
    if (a[6]) opt_size = *(LONG *)a[6];
    FreeArgs(rd);
    return TRUE;
}

/* ---- main -------------------------------------------------------------- */

int main(int argc, char **argv)
{
    struct MsgPort *port = NULL;
    struct Screen *scr = NULL;
    struct TextAttr ta, tb;
    struct monmsg *m;
    ULONG ticks = 0;
    BOOL running = TRUE, changed;
    LONG rc = 20, room_w, room_h, side_cols;

    if (argc == 0)
        tool_types((struct WBStartup *)argv);
    else if (!shell_args())
        return 10;
    if (!(IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 37)) ||
        !(GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 37)))
        goto done;
    DiskfontBase = OpenLibrary("diskfont.library", 36);

    Forbid();
    if (FindPort(MON_PORT)) {
        Permit();
        if (argc)
            PutStr("amigent-monitor runs already\n");
        rc = 5;
        goto done;
    }
    if ((port = CreateMsgPort())) {
        port->mp_Node.ln_Name = MON_PORT;
        port->mp_Node.ln_Pri = 0;
        AddPort(port);
    }
    Permit();
    if (!port)
        goto done;

    ta.ta_Name = opt_font;
    ta.ta_YSize = opt_size;
    ta.ta_Style = 0;
    ta.ta_Flags = 0;
    font = DiskfontBase ? OpenDiskFont(&ta) : OpenFont(&ta);
    if (!font || (font->tf_Flags & FPF_PROPORTIONAL)) {  /* columns need a fixed width */
        if (font)
            CloseFont(font);
        ta.ta_Name = "topaz.font";
        ta.ta_YSize = 8;
        font = OpenFont(&ta);
    }
    if (!font)
        goto done;
    fw = font->tf_XSize;
    fh = font->tf_YSize + 1;
    base = font->tf_Baseline;

    if (!(scr = LockPubScreen(NULL)))
        goto done;
    /* What the screen has room for: a 640x256 Workbench gets a smaller window,
     * the time column goes first, then the side panel narrows. */
    room_w = scr->Width - scr->WBorLeft - scr->WBorRight - 8;
    room_h = scr->Height - scr->BarHeight - 1 - scr->WBorTop - scr->Font->ta_YSize - 1 - scr->WBorBottom - 4;
    W = opt_w < room_w ? opt_w : room_w;
    H = opt_h < room_h ? opt_h : room_h;
    if (W < 40 * fw || H < 12 * fh)
        goto done;
    big = NULL;
    if (H >= 300 && DiskfontBase) {  /* a larger title where there is room */
        tb = ta;
        tb.ta_YSize = ta.ta_YSize < 11 ? 11 : ta.ta_YSize + 3;
        tb.ta_Style = FSF_BOLD;
        if ((big = OpenDiskFont(&tb)) && (big->tf_Flags & FPF_PROPORTIONAL)) {
            CloseFont(big);
            big = NULL;
        }
    }
    if (!big)
        big = OpenFont(&ta);  /* the same, drawn bold */
    head_h = big->tf_YSize + 10;
    side_cols = W / fw >= 90 ? 30 : W / fw >= 72 ? 26 : 22;
    side_w = side_cols * fw + 12;
    side_x = W - side_w - 6;
    log_x = 8;
    log_y = head_h + 5;
    log_w = side_x - log_x - 8;
    log_h = (H - 6 - log_y) / fh * fh;
    side_end = H - 6;
    stamp = log_w / fw >= 52 ? 9 : 0;
    win = OpenWindowTags(NULL,
        WA_Left, opt_left >= 0 ? opt_left : scr->Width - W - scr->WBorLeft - scr->WBorRight - (scr->Width > 1000 ? 40 : 0),
        WA_Top, opt_top >= 0 ? opt_top : scr->BarHeight + 1 + (scr->Height > 600 ? 24 : 0),
        WA_InnerWidth, W, WA_InnerHeight, H,
        WA_Title, (ULONG)"Amigent Monitor",
        WA_ScreenTitle, (ULONG)"Amigent Monitor: what the agent does",
        WA_DragBar, TRUE, WA_DepthGadget, TRUE, WA_CloseGadget, TRUE,
        WA_SmartRefresh, TRUE, WA_Activate, FALSE, WA_RMBTrap, TRUE,
        WA_IDCMP, IDCMP_CLOSEWINDOW | IDCMP_REFRESHWINDOW | IDCMP_VANILLAKEY,
        WA_PubScreen, (ULONG)scr, TAG_END);
    if (!win)
        goto done;
    cmap = scr->ViewPort.ColorMap;
    get_pens(scr);
    UnlockPubScreen(NULL, scr);
    scr = NULL;
    SetFont(win->RPort, font);
    st.started = seconds_now();
    add_line('S', "monitor up, listening on " MON_PORT);
    {
        char hello[80];

        strcpy(hello, "hello");
        if (opt_name[0]) {
            strcat(hello, " ");
            strcat(hello, opt_name);
        }
        strcat(hello, ", watching the agent ...");
        add_line('A', hello);
        st.tools = 0;
    }
    draw_all();
    rc = 0;

    while (running) {
        struct IntuiMessage *im;

        Delay(TICK);
        ticks++;
        if (SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C)
            running = FALSE;
        while ((im = (struct IntuiMessage *)GetMsg(win->UserPort))) {
            ULONG cls = im->Class;
            UWORD code = im->Code;

            ReplyMsg(&im->ExecMessage);
            if (cls == IDCMP_CLOSEWINDOW || (cls == IDCMP_VANILLAKEY && code == 27))
                running = FALSE;
            else if (cls == IDCMP_VANILLAKEY && (code == 'c' || code == 'C')) {
                nlines = 0;
                draw_log();
            } else if (cls == IDCMP_REFRESHWINDOW) {
                BeginRefresh(win);
                EndRefresh(win, TRUE);
                draw_all();
            }
        }
        changed = FALSE;
        while ((m = (struct monmsg *)GetMsg(port))) {
            if (m->kind != 'K')
                changed = TRUE;
            add_line(m->kind, m->text);
            FreeVec(m);
        }
        if (changed)
            draw_log();
        if (changed || ticks % 5 == 0)
            draw_side();
        draw_cursor((ticks / 3) & 1);
    }

done:
    if (port) {
        Forbid();
        RemPort(port);
        while ((m = (struct monmsg *)GetMsg(port)))
            FreeVec(m);
        Permit();
        DeleteMsgPort(port);
    }
    if (win) {
        if (own_pens)
            while (npens > 0)
                ReleasePen(cmap, pens[--npens]);
        CloseWindow(win);
    }
    if (scr)
        UnlockPubScreen(NULL, scr);
    if (big)  /* may be font itself, opened twice: closed twice */
        CloseFont(big);
    if (font)
        CloseFont(font);
    if (DiskfontBase)
        CloseLibrary(DiskfontBase);
    if (GfxBase)
        CloseLibrary((struct Library *)GfxBase);
    if (IntuitionBase)
        CloseLibrary((struct Library *)IntuitionBase);
    return rc;
}
