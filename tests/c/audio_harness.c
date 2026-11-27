/* Runs amigentd's AUDIO stream (audio_send_some up to c_audio_stream, copied
 * from amigentd.c by the test into audio_extract.inc) against a simulated
 * Exec, timer.device, TLS pump, socket and ARNE mix. No Amiga, no sound.
 *
 * Time only moves inside Wait() and Delay(), and 2 us per look at the mix,
 * so a loop that never sleeps shows up as many looks in a row, and a Wait()
 * that nothing can ever end is reported as a hang instead of blocking.
 *
 *   audio key=value ...
 *     tls=1          the PC comes through the TLS pump (loopback peer)
 *     play=A:B       the mix plays from A to B ms (a half every 10 ms)
 *     stop=T         the stop command arrives at T ms
 *     gone=T         the PC goes away at T ms; the pump passes nothing on
 *     close=D        and closes the loopback socket D ms later (0)
 *     brk=T          CTRL_C at T ms
 *     pump=U         the pump passes a batch on U us after it was sent (2000)
 *     notimer=1      no timer.device request to borrow
 *     cycles=N       N streams, one after the other, times per stream
 *     limit=T        give up after T simulated ms (300000)
 * Prints one line per stream: key=value.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <exec/types.h>

typedef int8_t BYTE;
#define EWOULDBLOCK_SIM 35
#undef EWOULDBLOCK  /* MinGW's stdlib.h brings errno.h with its own */
#define EWOULDBLOCK EWOULDBLOCK_SIM

struct Node { BYTE ln_Pri; };
struct Task { struct Node tc_Node; };
struct MsgPort { UBYTE mp_SigBit; };
struct Device { int unused; };
struct Unit { int unused; };
struct IORequest { struct Device *io_Device; struct Unit *io_Unit; UWORD io_Command; };
struct timerequest {
    struct IORequest tr_node;
    struct { ULONG tv_secs, tv_micro; } tr_time;
};
#define TR_ADDREQUEST 9
#define SIGBREAKF_CTRL_C (1UL << 12)
#define SIGBREAKF_CTRL_D (1UL << 13)
#define IO_SECONDS 30

struct Library { int unused; };
struct link { struct Library *base; LONG fd; };
struct sim_in_addr { ULONG s_addr; };
struct sim_sockaddr_in { UBYTE sin_len, sin_family; UWORD sin_port; struct sim_in_addr sin_addr; };
struct sim_sockaddr { UBYTE sa_len, sa_family; };
#define sockaddr_in sim_sockaddr_in
#define sockaddr sim_sockaddr
#undef htonl
#define htonl(x) (x)
#undef INADDR_LOOPBACK
#define INADDR_LOOPBACK 0x7f000001UL
#define AMIGENT_SOCKLEN(p) (p)

/* ---- the world ---- */

#define PORT_BIT 20
static unsigned long long t_us, base_us, limit_us = 300000000ULL;
static ULONG sig;
static struct Task me = {{0}}, other = {{0}};
static struct Task *running = &me;
static int tls, notimer, ports, ios, io_out, io_done, faults, last_errno;
static unsigned long long io_due;
static long play_from = -1, play_to = -1, stop_at = -1, gone_at = -1, close_after, brk_at = -1;
static long pump_us = 2000;
static int stop_taken, brk_sent;
static ULONG seq, pending, halves, sent;
static unsigned long long fwd[256];
static int fwd_n;
static ULONG run5, busy5, sleeps, looks5;  /* looks at priority 5 since the last sleep, most */
static struct Task *owner;
static struct Device timer_dev;
static struct timerequest timer_main;
static struct timerequest *timer_io;
static ULONG net_out;
static volatile LONG audio_hold;
static volatile LONG audio_wait;
static struct Task *volatile audio_task;

static long ms(void)
{
    return (long)((t_us - base_us) / 1000);
}

static void fail(const char *what)
{
    printf("fault=%s at_ms=%ld\n", what, ms());
    faults++;
}

static int gone(void)
{
    return gone_at >= 0 && ms() >= gone_at;
}

static int closed(void)
{
    return gone_at >= 0 && ms() >= gone_at + close_after;
}

/* The next thing that happens on its own, or ~0. */
static unsigned long long next_event(void)
{
    unsigned long long next = ~0ULL;
    int i;

    for (i = 0; i < fwd_n; i++)
        if (fwd[i] < next)
            next = fwd[i];
    if (io_out && !io_done && io_due < next)
        next = io_due;
    if (brk_at >= 0 && !brk_sent && base_us + (unsigned long long)brk_at * 1000 < next)
        next = base_us + (unsigned long long)brk_at * 1000;
    return next;
}

static void happen(void)
{
    int i;

    for (i = 0; i < fwd_n;) {
        if (fwd[i] <= t_us) {
            if (audio_wait && audio_task && !gone())
                sig |= SIGBREAKF_CTRL_D;  /* ssl_write_all's signal */
            fwd[i] = fwd[--fwd_n];
        } else
            i++;
    }
    if (io_out && !io_done && t_us >= io_due) {
        io_done = 1;
        sig |= 1UL << PORT_BIT;
    }
    if (brk_at >= 0 && !brk_sent && ms() >= brk_at) {
        brk_sent = 1;
        sig |= SIGBREAKF_CTRL_C;
    }
}

static void advance(unsigned long long to)
{
    unsigned long long e;

    while ((e = next_event()) <= to) {
        t_us = e > t_us ? e : t_us;
        happen();
    }
    t_us = to > t_us ? to : t_us;
    happen();
    if (t_us - base_us > limit_us) {
        printf("fault=timelimit at_ms=%ld\n", ms());
        exit(4);
    }
}

static void slept(void)
{
    sleeps++;
    run5 = 0;
}

/* ---- exec ---- */

static struct Task *FindTask(const char *name)
{
    (void)name;
    return running;
}

static BYTE SetTaskPri(struct Task *t, LONG pri)
{
    BYTE old = t->tc_Node.ln_Pri;

    t->tc_Node.ln_Pri = (BYTE)pri;
    return old;
}

static ULONG SetSignal(ULONG on, ULONG mask)
{
    ULONG old;

    happen();
    old = sig;
    sig = (sig & ~mask) | (on & mask);
    return old;
}

static ULONG Wait(ULONG mask)
{
    ULONG got;

    slept();
    for (;;) {
        happen();
        if ((got = sig & mask)) {
            sig &= ~got;
            return got;
        }
        if (next_event() == ~0ULL) {
            printf("fault=hang at_ms=%ld\n", ms());
            exit(3);
        }
        advance(next_event());
    }
}

static void Delay(LONG ticks)
{
    slept();
    advance(t_us + (unsigned long long)ticks * 20000);
}

static struct MsgPort *CreateMsgPort(void)
{
    static struct MsgPort port = {PORT_BIT};

    ports++;
    return &port;
}

static void DeleteMsgPort(struct MsgPort *port)
{
    (void)port;
    ports--;
}

static struct IORequest *CreateIORequest(struct MsgPort *port, ULONG size)
{
    (void)port;
    ios++;
    return calloc(1, size);
}

static void DeleteIORequest(struct IORequest *io)
{
    if (io_out)
        fail("deleted_in_flight");
    ios--;
    free(io);
}

static void SendIO(struct IORequest *io)
{
    struct timerequest *tr = (struct timerequest *)io;

    if (io_out)
        fail("sent_twice");
    if (io->io_Command != TR_ADDREQUEST || tr->tr_time.tv_micro >= 1000000)
        fail("bad_timerequest");
    io_out = 1;
    io_done = 0;
    io_due = t_us + (unsigned long long)tr->tr_time.tv_secs * 1000000 + tr->tr_time.tv_micro;
}

static struct IORequest *CheckIO(struct IORequest *io)
{
    happen();
    return io_done ? io : NULL;
}

static void AbortIO(struct IORequest *io)
{
    (void)io;
    if (io_out && !io_done) {
        io_done = 1;
        sig |= 1UL << PORT_BIT;
    }
}

static void WaitIO(struct IORequest *io)
{
    (void)io;
    if (!io_out)
        fail("waitio_idle");
    while (!io_done)
        advance(io_due);
    io_out = 0;
}

/* ---- bsdsocket and the pump ---- */

static LONG sim_send(LONG fd, const void *buf, LONG len, LONG flags)
{
    (void)fd;
    (void)buf;
    (void)flags;
    if (closed()) {
        last_errno = 32;  /* EPIPE */
        return -1;
    }
    if (gone()) {
        last_errno = EWOULDBLOCK;  /* the pump is stuck on the dead PC */
        return -1;
    }
    if (tls && fwd_n < 256)
        fwd[fwd_n++] = t_us + (unsigned long long)pump_us;
    sent += (ULONG)len;
    return len;
}

static LONG sim_recv(LONG fd, void *buf, LONG len, LONG flags)
{
    (void)fd;
    (void)flags;
    if (stop_at >= 0 && ms() >= stop_at && !stop_taken && !gone()) {
        stop_taken = 1;
        memset(buf, 0, (size_t)len);
        return len;  /* the rest of the 10-byte stop command, at once */
    }
    if (closed())
        return 0;
    last_errno = EWOULDBLOCK;
    return -1;
}

static LONG sim_getpeername(LONG fd, struct sockaddr *addr, LONG *len)
{
    (void)fd;
    (void)len;
    ((struct sockaddr_in *)addr)->sin_addr.s_addr = tls ? INADDR_LOOPBACK : 0x0a000002UL;
    return 0;
}

#define send(fd, b, n, f) ((void)SocketBase, sim_send(fd, b, n, f))
#define recv(fd, b, n, f) ((void)SocketBase, sim_recv(fd, b, n, f))
#define getpeername(fd, a, n) ((void)SocketBase, sim_getpeername(fd, a, n))
#define Errno() last_errno

static ULONG now_ms(void)
{
    return (ULONG)(t_us / 1000);
}

static void be32(UBYTE *p, ULONG v)
{
    p[0] = (UBYTE)(v >> 24);
    p[1] = (UBYTE)(v >> 16);
    p[2] = (UBYTE)(v >> 8);
    p[3] = (UBYTE)v;
}

static BOOL l_send(struct link *l, const void *data, ULONG len, LONG secs)
{
    (void)l;
    (void)data;
    (void)len;
    (void)secs;
    return !closed();
}

static BOOL c_fail(struct link *l, UBYTE status, const char *text)
{
    (void)l;
    (void)status;
    printf("refused=%s\n", text);
    return TRUE;
}

/* ---- the mix ---- */

static BOOL apollo_audio_pump(ULONG *rate)
{
    long now;

    t_us += 2;  /* a look costs a little */
    happen();
    if (running->tc_Node.ln_Pri >= 5) {
        looks5++;
        if (++run5 > busy5)
            busy5 = run5;
    } else
        run5 = 0;
    now = ms();
    if (rate)
        *rate = 48000;
    if (play_from >= 0 && now >= play_from && now < play_to) {
        ULONG half = (ULONG)((now - play_from) / 10) + 1;

        if (half > halves) {
            pending += 1920 * (half - halves);
            seq += half - halves;
            halves = half;
        }
    }
    return TRUE;
}

static ULONG apollo_audio_seq(void)
{
    return seq;
}

static ULONG apollo_audio_pending(void)
{
    return pending;
}

static LONG apollo_audio_take(UBYTE *dst, LONG cap)
{
    ULONG n = pending < (ULONG)cap ? pending : (ULONG)cap;

    n &= ~3UL;
    memset(dst, 0, n);
    pending -= n;
    return (LONG)n;
}

static BOOL apollo_audio_claim(void)
{
    if (owner && owner != FindTask(NULL))
        return FALSE;
    owner = FindTask(NULL);
    pending = 0;
    return TRUE;
}

static void apollo_audio_release(void)
{
    if (owner == FindTask(NULL))
        owner = NULL;
}

static void apollo_audio_breathe(void)
{
    SetTaskPri(FindTask(NULL), -1);
    SetTaskPri(FindTask(NULL), 0);
}

#include "audio_extract.inc"

static long arg(int argc, char **argv, const char *key, long dflt)
{
    size_t k = strlen(key);
    int i;

    for (i = 1; i < argc; i++)
        if (!strncmp(argv[i], key, k) && argv[i][k] == '=')
            return strtol(argv[i] + k + 1, NULL, 10);
    return dflt;
}

int main(int argc, char **argv)
{
    struct Library base;
    struct link l = {&base, 3};
    long cycles, c;
    const char *play = NULL;
    int i;

    for (i = 1; i < argc; i++)
        if (!strncmp(argv[i], "play=", 5))
            play = argv[i] + 5;
    tls = (int)arg(argc, argv, "tls", 0);
    notimer = (int)arg(argc, argv, "notimer", 0);
    stop_at = arg(argc, argv, "stop", -1);
    gone_at = arg(argc, argv, "gone", -1);
    close_after = arg(argc, argv, "close", 0);
    brk_at = arg(argc, argv, "brk", -1);
    pump_us = arg(argc, argv, "pump", 2000);
    cycles = arg(argc, argv, "cycles", 1);
    limit_us = (unsigned long long)arg(argc, argv, "limit", 300000) * 1000;
    if (play && sscanf(play, "%ld:%ld", &play_from, &play_to) != 2)
        return 2;
    timer_main.tr_node.io_Device = &timer_dev;
    timer_io = notimer ? NULL : &timer_main;
    t_us = 5000000;  /* not zero: now_ms() is never 0 once the mix moves */
    for (c = 0; c < cycles; c++) {
        BOOL ok;
        int free_again;

        base_us = t_us;
        stop_taken = brk_sent = 0;
        seq = pending = halves = sent = 0;
        run5 = busy5 = sleeps = looks5 = 0;
        fwd_n = 0;
        sig &= ~(SIGBREAKF_CTRL_C | SIGBREAKF_CTRL_D);
        me.tc_Node.ln_Pri = 0;
        running = &me;
        ok = c_audio_stream(&l);
        running = &other;  /* someone else asks for the mix now */
        free_again = apollo_audio_claim();
        if (free_again)
            apollo_audio_release();
        running = &me;
        printf("cycle=%ld ret=%d end_ms=%ld free=%d ports=%d ios=%d io_out=%d hold=%ld wait=%ld "
               "task=%d pri=%d portsig=%d busy5=%lu looks5=%lu sleeps=%lu made=%lu sent=%lu faults=%d\n",
               c, ok, ms(), free_again, ports, ios, io_out, (long)audio_hold, (long)audio_wait,
               audio_task != NULL, me.tc_Node.ln_Pri, (sig & (1UL << PORT_BIT)) != 0,
               (unsigned long)busy5, (unsigned long)looks5, (unsigned long)sleeps,
               (unsigned long)(halves * 1920), (unsigned long)sent, faults);
        advance(t_us + 1000000);  /* a second between streams */
    }
    return 0;
}
