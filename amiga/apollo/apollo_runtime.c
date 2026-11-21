/* Startup and the C routines for amigentd.apollo.
 *
 * A Shell start (S:User-Startup's Run, or an AmigaShell) would otherwise stay
 * the boot console's process for as long as the daemon runs. The same hand-off
 * libnix's detach uses applies here: the Shell process starts a second process
 * with no console and returns, so EndCLI can close the window. The second
 * process is the one that reaches main. A Workbench start has no Shell, so
 * main is called with the WBStartup message, as MindLink does, and the message
 * is replied when main returns. This file does not link libnix. */
#include <exec/types.h>
#include <exec/execbase.h>
#include <exec/memory.h>
#include <dos/dos.h>
#include <dos/dosextens.h>
#include <dos/dostags.h>
#include <workbench/startup.h>
#include <proto/exec.h>
#include <proto/dos.h>

struct ExecBase *SysBase;
struct DosLibrary *DOSBase;

extern int main(int argc, char **argv);
void *memcpy(void *dst, const void *src, unsigned long n);

/* Set once the Shell process has spawned the daemon. The child sees it and
 * does not spawn again. Shared, because both processes use this seglist. */
static struct SignalSemaphore *handed;

/* ReadArgs reads Input(), and the daemon process does not keep the Shell's
 * input. The Shell process writes the command line and hands that file to the
 * daemon. It never closes the Shell's own NIL:, which the Shell closes when
 * this process returns; closing it here as well is the recoverable alert
 * "filehandle closed more than once". */
static BPTR command_file(char *cmdline, long len)
{
    static const char *const names[] = {"T:amigentd.args", "RAM:amigentd.args", 0};
    long n = len < 0 ? 0 : len;
    int i;

    if (n > 4000)
        n = 4000;
    for (i = 0; names[i]; i++) {
        BPTR out = Open((CONST_STRPTR)names[i], MODE_NEWFILE);
        BPTR in;

        if (!out)
            continue;
        if (n)
            Write(out, cmdline, n);
        if (!n || cmdline[n - 1] != '\n')
            Write(out, "\n", 1);
        Close(out);
        in = Open((CONST_STRPTR)names[i], MODE_OLDFILE);
        if (in)
            return in;
    }
    return 0;
}

static int run_daemon(void)
{
    static char *argv[] = {"amigentd", 0};
    int rc;

    if (!(DOSBase = (struct DosLibrary *)OpenLibrary("dos.library", 37)))
        return RETURN_FAIL;
    rc = main(1, argv);
    CloseLibrary((struct Library *)DOSBase);
    DOSBase = 0;
    return rc;
}

/* The Shell process. Returns 0 once the daemon process exists; the seglist
 * then belongs to that process (NP_FreeSeglist), not to this Shell. */
static int handoff(char *cmdline, long len)
{
    struct Process *me = (struct Process *)FindTask(NULL);
    struct CommandLineInterface *cli = BADDR(me->pr_CLI);
    struct SignalSemaphore *sema;
    struct DosLibrary *dos;
    struct TagItem tags[12];
    struct Process *child;
    BPTR in, out;

    if (!(sema = AllocMem(sizeof(*sema), MEMF_PUBLIC | MEMF_CLEAR)))
        return run_daemon();
    InitSemaphore(sema);
    ObtainSemaphore(sema);
    handed = sema;
    if (!(dos = (struct DosLibrary *)OpenLibrary("dos.library", 37))) {
        ReleaseSemaphore(sema);
        FreeMem(sema, sizeof(*sema));
        handed = 0;
        return RETURN_FAIL;
    }
    DOSBase = dos;
    if (me->pr_Arguments && me->pr_Arguments[0]) {
        cmdline = me->pr_Arguments;
        len = 0;
        while (cmdline[len] && len < 4000)
            len++;
    }
    in = command_file(cmdline, len);
    out = Open("RAM:amigentd.out", MODE_NEWFILE);
    if (!in || !out) {
        if (in)
            Close(in);
        if (out)
            Close(out);
        CloseLibrary((struct Library *)dos);
        DOSBase = 0;
        ReleaseSemaphore(sema);
        FreeMem(sema, sizeof(*sema));
        handed = 0;
        return run_daemon();
    }
    tags[0].ti_Tag = NP_Seglist;
    tags[0].ti_Data = cli->cli_Module;
    tags[1].ti_Tag = NP_FreeSeglist;
    tags[1].ti_Data = TRUE;
    tags[2].ti_Tag = NP_Cli;
    tags[2].ti_Data = TRUE;
    tags[3].ti_Tag = NP_StackSize;
    tags[3].ti_Data = 65536;
    tags[4].ti_Tag = NP_Name;
    tags[4].ti_Data = (ULONG)"amigentd";
    tags[5].ti_Tag = NP_Input;
    tags[5].ti_Data = in; /* the child closes this; this process must not */
    tags[6].ti_Tag = NP_CloseInput;
    tags[6].ti_Data = TRUE;
    tags[7].ti_Tag = NP_Output;
    tags[7].ti_Data = out;
    tags[8].ti_Tag = NP_CloseOutput;
    tags[8].ti_Data = TRUE;
    tags[9].ti_Tag = NP_ConsoleTask;
    tags[9].ti_Data = 0; /* do not inherit the Shell's console */
    tags[10].ti_Tag = NP_WindowPtr;
    tags[10].ti_Data = (ULONG)-1;
    tags[11].ti_Tag = TAG_END;
    child = CreateNewProc(tags);
    if (!child) {
        Close(out);
        SelectInput(in);
        CloseLibrary((struct Library *)dos);
        DOSBase = 0;
        ReleaseSemaphore(sema);
        FreeMem(sema, sizeof(*sema));
        handed = 0;
        return run_daemon();
    }
    cli->cli_Module = 0;
    CloseLibrary((struct Library *)dos);
    DOSBase = 0;
    ReleaseSemaphore(sema);
    return 0;
}

int apollo_enter(char *cmdline, long len)
{
    struct Process *me = (struct Process *)FindTask(NULL);
    struct WBStartup *wb;
    struct SignalSemaphore *sema;
    int rc;

    if ((sema = handed)) {
        ObtainSemaphore(sema);
        ReleaseSemaphore(sema);
        FreeMem(sema, sizeof(*sema));
        handed = 0;
        return run_daemon();
    }
    if (me->pr_CLI)
        return handoff(cmdline, len);
    if (!(DOSBase = (struct DosLibrary *)OpenLibrary("dos.library", 37)))
        return RETURN_FAIL;
    WaitPort(&me->pr_MsgPort);
    wb = (struct WBStartup *)GetMsg(&me->pr_MsgPort);
    rc = main(0, (char **)wb);
    Forbid();
    ReplyMsg((struct Message *)wb);
    CloseLibrary((struct Library *)DOSBase);
    DOSBase = 0;
    return rc;
}

typedef unsigned long size_t;

void *memcpy(void *dst, const void *src, size_t n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;

    while (n--)
        *d++ = *s++;
    return dst;
}

void *memmove(void *dst, const void *src, size_t n)
{
    unsigned char *d = dst;
    const unsigned char *s = src;

    if (d < s) {
        while (n--)
            *d++ = *s++;
    } else {
        d += n;
        s += n;
        while (n--)
            *--d = *--s;
    }
    return dst;
}

void *memset(void *dst, int c, size_t n)
{
    unsigned char *d = dst;

    while (n--)
        *d++ = (unsigned char)c;
    return dst;
}

int memcmp(const void *a, const void *b, size_t n)
{
    const unsigned char *p = a, *q = b;

    while (n--) {
        if (*p != *q)
            return *p - *q;
        p++;
        q++;
    }
    return 0;
}

size_t strlen(const char *s)
{
    size_t n = 0;

    while (s[n])
        n++;
    return n;
}

int strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return (unsigned char)*a - (unsigned char)*b;
}

char *strcpy(char *dst, const char *src)
{
    char *d = dst;

    while ((*d++ = *src++))
        ;
    return dst;
}

char *strcat(char *dst, const char *src)
{
    char *d = dst;

    while (*d)
        d++;
    while ((*d++ = *src++))
        ;
    return dst;
}

/* libnix's string.h turns memmove into this. Arguments are source, then destination. */
void __bcopz(const void *from, void *to, size_t n)
{
    memmove(to, from, n);
}

char *strchr(const char *s, int c)
{
    unsigned char want = (unsigned char)c;

    while (*s) {
        if ((unsigned char)*s == want)
            return (char *)s;
        s++;
    }
    return want ? 0 : (char *)s;
}

char *strncpy(char *dst, const char *src, size_t n)
{
    size_t i = 0;

    while (i < n && src[i]) {
        dst[i] = src[i];
        i++;
    }
    while (i < n)
        dst[i++] = 0;
    return dst;
}

static void memswap(unsigned char *a, unsigned char *b, size_t n)
{
    while (n--) {
        unsigned char t = *a;
        *a++ = *b;
        *b++ = t;
    }
}

static void sift(unsigned char *base, size_t root, size_t n, size_t size,
                 int (*cmp)(const void *, const void *))
{
    for (;;) {
        size_t child = root * 2 + 1;

        if (child >= n)
            return;
        if (child + 1 < n &&
            cmp(base + (child + 1) * size, base + child * size) > 0)
            child++;
        if (cmp(base + root * size, base + child * size) >= 0)
            return;
        memswap(base + root * size, base + child * size, size);
        root = child;
    }
}

void qsort(void *base, size_t n, size_t size, int (*cmp)(const void *, const void *))
{
    unsigned char *b = base;
    size_t i;

    if (n < 2 || !size)
        return;
    i = n / 2;
    while (i-- > 0)
        sift(b, i, n, size, cmp);
    while (--n > 0) {
        memswap(b, b + n * size, size);
        sift(b, 0, n, size, cmp);
    }
}
