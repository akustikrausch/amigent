/* Runs amigentd's POKE receive (l_wait_until, l_recv_until, l_skip_until and
 * poke_budget_ms, copied from amigentd.c by the test into poke_extract.inc)
 * against a simulated sender and a simulated clock. No socket, no Amiga:
 * WaitSelect and recv are the sender, now_ms() is the clock it moves.
 *
 *   poke budget N                     prints poke_budget_ms(N)
 *   poke recv|skip N RATE STOP [T0]   receives N bytes from a sender that sends
 *                                     RATE bytes a second (0: all at once) and
 *                                     stops after STOP bytes; the clock starts
 *                                     at T0. Prints "ok|timeout MS BYTES".
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <exec/types.h>

#ifndef EWOULDBLOCK
#define EWOULDBLOCK EAGAIN
#endif

/* bsdsocket's types, without a host socket header (MinGW has no sys/select.h) */
struct sim_timeval { long tv_sec; long tv_usec; };
typedef struct { unsigned long bits; } sim_fd_set;
#define timeval sim_timeval
#define fd_set sim_fd_set
#undef FD_ZERO
#undef FD_SET
#define FD_ZERO(s) ((s)->bits = 0)
#define FD_SET(fd, s) ((s)->bits |= 1UL << (fd))

struct Library { int unused; };
struct link { struct Library *base; LONG fd; };
#define SIGBREAKF_CTRL_C (1UL << 12)

static ULONG clock_ms, start_ms, net_in;
static ULONG total, rate, stop_at, delivered;
static int last_errno;

static ULONG now_ms(void)
{
    return clock_ms;
}

/* bytes the sender has put on the line by clock_ms + ahead */
static ULONG sent_by(ULONG ahead)
{
    unsigned long long b = rate ? (unsigned long long)(clock_ms - start_ms + ahead) * rate / 1000
                                : total;
    if (b > stop_at)
        b = stop_at;
    return b > total ? total : (ULONG)b;
}

static LONG sim_wait(struct Library *base, LONG nfds, fd_set *r, fd_set *w, fd_set *e,
                     struct timeval *tv, ULONG *sigs)
{
    ULONG timeout = (ULONG)tv->tv_sec * 1000 + (ULONG)tv->tv_usec / 1000, next;

    (void)base;
    (void)nfds;
    (void)r;
    (void)w;
    (void)e;
    *sigs = 0;
    if (sent_by(0) > delivered)
        return 1;
    if (!rate || delivered >= stop_at || delivered >= total) {
        clock_ms += timeout;  /* nothing more is coming */
        return 0;
    }
    next = (ULONG)(((unsigned long long)(delivered + 1) * 1000 + rate - 1) / rate)
           - (clock_ms - start_ms);
    if (next > timeout) {
        clock_ms += timeout;
        return 0;
    }
    clock_ms += next;
    return 1;
}

static LONG sim_recv(struct Library *base, LONG fd, void *p, ULONG len, int flags)
{
    ULONG n = sent_by(0) - delivered;

    (void)base;
    (void)fd;
    (void)flags;
    if (!n) {
        last_errno = EWOULDBLOCK;
        return -1;
    }
    if (n > len)
        n = len;
    memset(p, 0xA5, n);
    delivered += n;
    return (LONG)n;
}

#define WaitSelect(n, r, w, e, tv, sigs) sim_wait(SocketBase, n, r, w, e, tv, sigs)
#define recv(fd, p, len, flags) sim_recv(SocketBase, fd, p, len, flags)  /* as bsdsocket's macros */
#define Errno() last_errno
#define SetSignal(a, b) ((void)0)

#include "poke_extract.inc"

int main(int argc, char **argv)
{
    static UBYTE buf[PEEK_MAX];
    struct Library base;
    struct link l = { &base, 3 };
    ULONG n;
    BOOL ok;

    if (argc == 3 && !strcmp(argv[1], "budget")) {
        printf("%lu\n", (unsigned long)poke_budget_ms(strtoul(argv[2], NULL, 0)));
        return 0;
    }
    if ((argc != 5 && argc != 6) || (strcmp(argv[1], "recv") && strcmp(argv[1], "skip")))
        return 2;
    n = strtoul(argv[2], NULL, 0);
    if (n > PEEK_MAX)
        return 2;
    total = n;
    rate = strtoul(argv[3], NULL, 0);
    stop_at = strtoul(argv[4], NULL, 0);
    clock_ms = start_ms = argc == 6 ? strtoul(argv[5], NULL, 0) : 1000;
    if (!strcmp(argv[1], "recv"))
        ok = l_recv_until(&l, buf, n, now_ms() + poke_budget_ms(n));
    else
        ok = l_skip_until(&l, n, now_ms() + poke_budget_ms(n));
    printf("%s %lu %lu\n", ok ? "ok" : "timeout", (unsigned long)(clock_ms - start_ms),
           (unsigned long)delivered);
    return 0;
}
