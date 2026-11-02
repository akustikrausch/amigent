/*
 * amigent-status: a small window that shows what amigentd is up to. It asks
 * the ARexx port every two seconds, so it needs nothing but amigentd running
 * and rexxsyslib.library; RexxMast can stay at home. OS 2.04 and up.
 *
 *   amigent-status [PORT <name>]
 *
 * PORT names the ARexx port, default AMIGENT. From Workbench: tool type PORT.
 * MIT licence, see LICENSE.
 */

#include <exec/types.h>
#include <exec/memory.h>
#include <dos/dos.h>
#include <devices/timer.h>
#include <intuition/intuition.h>
#include <libraries/gadtools.h>
#include <rexx/storage.h>
#include <workbench/startup.h>
#include <workbench/workbench.h>
#include <clib/alib_protos.h>
#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/graphics.h>
#include <proto/gadtools.h>
#include <proto/rexxsyslib.h>
#include <proto/icon.h>
#include <string.h>

const char version[] = "$VER: amigent-status 1.1.0 (02.11.2026)";

struct IntuitionBase *IntuitionBase;
struct GfxBase *GfxBase;
struct Library *GadToolsBase, *IconBase;
struct RxsLib *RexxSysBase;

#define LINES 24        /* status and connections, more than any Amiga has */
#define LINE_LEN 64
#define REFRESH_SECS 2

static char port_name[32] = "AMIGENT";
static char lines[LINES][LINE_LEN];
static struct Node nodes[LINES];
static struct List list;

/* One question to amigentd's port; the answer string into out. FALSE when
 * nobody is home. The port is looked up and used under Forbid, so it can't
 * vanish between the two; amigentd answers whatever is queued as it leaves. */
static BOOL ask(const char *cmd, char *out, LONG size)
{
    struct MsgPort *reply, *port;
    struct RexxMsg *m;
    BOOL ok = FALSE;

    out[0] = 0;
    if (!(reply = CreateMsgPort()))
        return FALSE;
    if (!(m = CreateRexxMsg(reply, NULL, NULL))) {
        DeleteMsgPort(reply);
        return FALSE;
    }
    m->rm_Args[0] = (STRPTR)CreateArgstring((STRPTR)cmd, strlen(cmd));
    m->rm_Action = RXCOMM | RXFF_RESULT;
    Forbid();
    if ((port = FindPort((STRPTR)port_name)))
        PutMsg(port, (struct Message *)m);
    Permit();
    if (port) {
        WaitPort(reply);
        GetMsg(reply);
        if (m->rm_Result1 == 0 && m->rm_Result2) {
            strncpy(out, (char *)m->rm_Result2, size - 1);
            out[size - 1] = 0;
            DeleteArgstring((UBYTE *)m->rm_Result2);
            ok = TRUE;
        }
    }
    if (m->rm_Args[0])
        DeleteArgstring((UBYTE *)m->rm_Args[0]);
    DeleteRexxMsg(m);
    DeleteMsgPort(reply);
    return ok;
}

/* key=value out of STATUS; "" if it isn't there. */
static const char *field(const char *text, const char *key, char *buf, LONG size)
{
    LONG n = strlen(key), i;
    const char *p = text;

    buf[0] = 0;
    while (*p) {
        if (!strncmp(p, key, n) && p[n] == '=') {
            for (p += n + 1, i = 0; *p && *p != '\n' && i < size - 1; p++)
                buf[i++] = *p;
            buf[i] = 0;
            break;
        }
        while (*p && *p != '\n')
            p++;
        if (*p)
            p++;
    }
    return buf;
}

/* A list line from fmt, %s and %ld only: a printf that fits in a pocket. */
static void add_line(LONG *n, const char *fmt, LONG *args)
{
    if (*n >= LINES)
        return;
    {
        char *d = lines[*n];
        const char *f = fmt;
        LONG a = 0, len = 0;
        while (*f && len < LINE_LEN - 1) {
            if (*f == '%' && f[1] == 's') {
                const char *s = (const char *)args[a++];
                while (s && *s && len < LINE_LEN - 1)
                    d[len++] = *s++;
                f += 2;
            } else if (*f == '%' && f[1] == 'l' && f[2] == 'd') {
                char tmp[12];
                LONG v = args[a++], k = 0;
                ULONG u = v < 0 ? -v : v;
                if (v < 0 && len < LINE_LEN - 1)
                    d[len++] = '-';
                do
                    tmp[k++] = '0' + u % 10;
                while (u /= 10);
                while (k && len < LINE_LEN - 1)
                    d[len++] = tmp[--k];
                f += 3;
            } else
                d[len++] = *f++;
        }
        d[len] = 0;
    }
    nodes[*n].ln_Name = lines[*n];
    AddTail(&list, &nodes[*n]);
    (*n)++;
}

static LONG to_num(const char *s)
{
    LONG v = 0;

    while (*s >= '0' && *s <= '9')
        v = v * 10 + *s++ - '0';
    return v;
}

/* 3725 seconds -> "1 h 2 min"; the demo effect of the evening. */
static void span(LONG secs, char *out)
{
    char *d = out;

    if (secs < 60) {
        LONG k = 0;
        char t[12];
        do t[k++] = '0' + secs % 10; while (secs /= 10);
        while (k) *d++ = t[--k];
        strcpy(d, " s");
        return;
    }
    if (secs >= 3600) {
        LONG h = secs / 3600, k = 0;
        char t[12];
        do t[k++] = '0' + h % 10; while (h /= 10);
        while (k) *d++ = t[--k];
        strcpy(d, " h "), d += 3;
    }
    {
        LONG m = secs % 3600 / 60, k = 0;
        char t[12];
        do t[k++] = '0' + m % 10; while (m /= 10);
        while (k) *d++ = t[--k];
        strcpy(d, " min");
    }
}

/* Asks STATUS and CLIENTS and turns the answers into list lines. */
static void gather(void)
{
    static char status[1024], clients[2048];
    char a[24], b[24], c[24], up[24];
    LONG n = 0, args[4];
    const char *p;

    NewList(&list);
    if (!ask("STATUS", status, sizeof(status))) {
        args[0] = (LONG)port_name;
        add_line(&n, "No answer on ARexx port %s:", args);
        add_line(&n, "amigentd isn't running.", args);
        return;
    }
    ask("CLIENTS", clients, sizeof(clients));
    args[0] = (LONG)field(status, "version", a, sizeof(a));
    args[1] = to_num(field(status, "port", b, sizeof(b)));
    args[2] = to_num(field(status, "protocol", c, sizeof(c)));
    add_line(&n, "amigentd %s on port %ld, protocol %ld", args);
    span(to_num(field(status, "uptime", a, sizeof(a))), up);
    args[0] = (LONG)up;
    add_line(&n, "Running for %s", args);
    args[0] = (LONG)(!strcmp(field(status, "tls", a, sizeof(a)), "on") ?
                     (!strcmp(field(status, "tls_only", b, sizeof(b)), "on") ? "on, plain only for localhost" : "on")
                     : "off");
    args[1] = (LONG)(!strcmp(field(status, "key", c, sizeof(c)), "on") ? "required" : "none");
    add_line(&n, "TLS %s, key %s", args);
    if (!strcmp(field(status, "setup", a, sizeof(a)), "on"))
        add_line(&n, "SETUP mode: waiting for amigent-setup", args);
    args[0] = to_num(field(status, "screen", a, sizeof(a)));
    args[1] = to_num(field(status, "channels", b, sizeof(b)));
    args[2] = to_num(field(status, "tls_clients", c, sizeof(c)));
    add_line(&n, "Screen %ld, command channels %ld, TLS %ld", args);
    add_line(&n, "", args);
    if (!clients[0]) {
        add_line(&n, "Nobody connected.", args);
        return;
    }
    add_line(&n, "Connections:", args);
    for (p = clients; *p && n < LINES; ) {  /* kind \t address \t seconds */
        char kind[12], addr[20], secs[12];
        LONG i;
        for (i = 0; *p && *p != '\t' && i < 11; ) kind[i++] = *p++;
        kind[i] = 0; if (*p == '\t') p++;
        for (i = 0; *p && *p != '\t' && i < 19; ) addr[i++] = *p++;
        addr[i] = 0; if (*p == '\t') p++;
        for (i = 0; *p && *p != '\n' && i < 11; ) secs[i++] = *p++;
        secs[i] = 0; if (*p == '\n') p++;
        span(to_num(secs), up);
        args[0] = (LONG)kind, args[1] = (LONG)addr, args[2] = (LONG)up;
        add_line(&n, "  %s  %s  %s", args);
    }
}

static void tool_types(struct WBStartup *wb)
{
    struct DiskObject *icon;
    BPTR old;
    STRPTR v;

    if (!(IconBase = OpenLibrary("icon.library", 37)))
        return;
    old = CurrentDir(wb->sm_ArgList[0].wa_Lock);
    if ((icon = GetDiskObject(wb->sm_ArgList[0].wa_Name))) {
        if ((v = FindToolType((STRPTR *)icon->do_ToolTypes, "PORT"))) {
            strncpy(port_name, (char *)v, sizeof(port_name) - 1);
        }
        FreeDiskObject(icon);
    }
    CurrentDir(old);
    CloseLibrary(IconBase);
}

int main(int argc, char **argv)
{
    struct Screen *scr = NULL;
    APTR vi = NULL;
    struct Gadget *glist = NULL, *gad, *lv;
    struct NewGadget ng;
    struct Window *win = NULL;
    struct MsgPort *tport = NULL;
    struct timerequest *treq = NULL;
    BOOL timer_on = FALSE, done = FALSE;
    LONG args[1] = {0};
    struct RDArgs *rda = NULL;
    UWORD fh, width, height;
    int rc = RETURN_FAIL;

    if (argc == 0)
        tool_types((struct WBStartup *)argv);
    else if ((rda = ReadArgs("PORT/K", args, NULL))) {
        if (args[0])
            strncpy(port_name, (char *)args[0], sizeof(port_name) - 1);
        FreeArgs(rda);
    }
    IntuitionBase = (struct IntuitionBase *)OpenLibrary("intuition.library", 37);
    GfxBase = (struct GfxBase *)OpenLibrary("graphics.library", 37);
    GadToolsBase = OpenLibrary("gadtools.library", 37);
    RexxSysBase = (struct RxsLib *)OpenLibrary("rexxsyslib.library", 36);
    if (!IntuitionBase || !GfxBase || !GadToolsBase || !RexxSysBase) {
        if (argc)
            PutStr("amigent-status: needs OS 2.04 and rexxsyslib.library\n");
        goto out;
    }
    if (!(scr = LockPubScreen(NULL)) || !(vi = GetVisualInfo(scr, TAG_END)))
        goto out;
    fh = scr->Font->ta_YSize;
    width = 46 * scr->RastPort.Font->tf_XSize + 24;  /* 46 columns of the screen font */
    height = (fh + 2) * 12 + 10;
    gad = CreateContext(&glist);
    ng.ng_LeftEdge = scr->WBorLeft + 4;
    ng.ng_TopEdge = scr->WBorTop + fh + 1 + 2;
    ng.ng_Width = width - 8;
    ng.ng_Height = height;
    ng.ng_GadgetText = NULL;
    ng.ng_TextAttr = scr->Font;
    ng.ng_GadgetID = 1;
    ng.ng_Flags = 0;
    ng.ng_VisualInfo = vi;
    ng.ng_UserData = NULL;
    NewList(&list);
    lv = gad = CreateGadget(LISTVIEW_KIND, gad, &ng, GTLV_Labels, (ULONG)&list, GTLV_ReadOnly, TRUE, TAG_END);
    if (!gad)
        goto out;
    win = OpenWindowTags(NULL,
                         WA_Title, (ULONG)"Amigent",
                         WA_ScreenTitle, (ULONG)(version + 6),
                         WA_PubScreen, (ULONG)scr,
                         WA_InnerWidth, width,
                         WA_InnerHeight, height + 4,
                         WA_Gadgets, (ULONG)glist,
                         WA_DragBar, TRUE, WA_DepthGadget, TRUE, WA_CloseGadget, TRUE,
                         WA_Activate, TRUE, WA_SimpleRefresh, TRUE,
                         WA_IDCMP, IDCMP_CLOSEWINDOW | IDCMP_REFRESHWINDOW | IDCMP_VANILLAKEY | LISTVIEWIDCMP,
                         TAG_END);
    UnlockPubScreen(NULL, scr);
    scr = NULL;
    if (!win)
        goto out;
    GT_RefreshWindow(win, NULL);
    if ((tport = CreateMsgPort()) &&
        (treq = (struct timerequest *)CreateIORequest(tport, sizeof(*treq))) &&
        OpenDevice(TIMERNAME, UNIT_VBLANK, (struct IORequest *)treq, 0) == 0)
        timer_on = TRUE;

    while (!done) {
        struct IntuiMessage *im;
        ULONG got;

        /* new numbers in, detached list first: GadTools wants it that way */
        GT_SetGadgetAttrs(lv, win, NULL, GTLV_Labels, ~0UL, TAG_END);
        gather();
        GT_SetGadgetAttrs(lv, win, NULL, GTLV_Labels, (ULONG)&list, TAG_END);
        if (timer_on) {
            treq->tr_node.io_Command = TR_ADDREQUEST;
            treq->tr_time.tv_secs = REFRESH_SECS;
            treq->tr_time.tv_micro = 0;
            SendIO((struct IORequest *)treq);
        }
        for (;;) {
            got = Wait((1UL << win->UserPort->mp_SigBit) | SIGBREAKF_CTRL_C |
                       (timer_on ? 1UL << tport->mp_SigBit : 0));
            while ((im = GT_GetIMsg(win->UserPort))) {
                ULONG cls = im->Class;
                UWORD code = im->Code;
                GT_ReplyIMsg(im);
                if (cls == IDCMP_CLOSEWINDOW || (cls == IDCMP_VANILLAKEY && (code == 27 || code == 'q')))
                    done = TRUE;
                else if (cls == IDCMP_REFRESHWINDOW) {
                    GT_BeginRefresh(win);
                    GT_EndRefresh(win, TRUE);
                }
            }
            if (got & SIGBREAKF_CTRL_C)
                done = TRUE;
            if (done || !timer_on || CheckIO((struct IORequest *)treq))
                break;
        }
        if (timer_on) {
            if (!CheckIO((struct IORequest *)treq))
                AbortIO((struct IORequest *)treq);
            WaitIO((struct IORequest *)treq);
        }
    }
    rc = RETURN_OK;

out:
    if (timer_on)
        CloseDevice((struct IORequest *)treq);
    if (treq)
        DeleteIORequest((struct IORequest *)treq);
    if (tport)
        DeleteMsgPort(tport);
    if (win) {
        GT_SetGadgetAttrs(lv, win, NULL, GTLV_Labels, ~0UL, TAG_END);
        CloseWindow(win);
    }
    FreeGadgets(glist);
    if (vi)
        FreeVisualInfo(vi);
    if (scr)
        UnlockPubScreen(NULL, scr);
    if (RexxSysBase) CloseLibrary((struct Library *)RexxSysBase);
    if (GadToolsBase) CloseLibrary(GadToolsBase);
    if (GfxBase) CloseLibrary((struct Library *)GfxBase);
    if (IntuitionBase) CloseLibrary((struct Library *)IntuitionBase);
    return rc;
}
