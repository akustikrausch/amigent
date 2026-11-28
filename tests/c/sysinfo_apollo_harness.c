/* Host-only harness exercising the verbatim CPU/Apollo/AUDIO/EMU68 capability
 * block extracted from c_sysinfo() in src/native/amiga/daemon/amigentd.c,
 * paired with the verbatim apollo_os_here() and apollo_board_name() from
 * src/native/amiga/apollo/apollo_detect.c.
 *
 * apollo_board() is stubbed to count invocations and return a caller-supplied
 * board id without touching 0xDFF3FC. No driver lookup or MMIO occurs. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <exec/types.h>

#define AFF_68010 (1L << 0)
#define AFF_68020 (1L << 1)
#define AFF_68030 (1L << 2)
#define AFF_68040 (1L << 3)
#define AFF_68060 (1L << 7)
#define AFF_68080 (1L << 10)

#define CMD_EMU68 48
#define CMD_AUDIO 49

struct FakeLibNode {
    UWORD lib_Version;
};

struct ExecBase {
    struct FakeLibNode LibNode;
    UWORD AttnFlags;
};

static struct ExecBase fake_exec;
struct ExecBase *SysBase = &fake_exec;

static UWORD mock_board_id = 0x0500;
static int board_calls = 0;
static int mock_emu68 = 0;

UWORD apollo_board(void)
{
    board_calls++;
    return mock_board_id;
}

static BOOL emu68_present(void)
{
    return mock_emu68 ? TRUE : FALSE;
}

/* Verbatim apollo_board_name() and apollo_os_here() from apollo_detect.c */
#include "apollo_detect_extract.inc"

struct text {
    char buf[4096];
    size_t len;
};

static void t_chr(struct text *t, char c)
{
    if (t->len + 1 < sizeof(t->buf)) {
        t->buf[t->len++] = c;
        t->buf[t->len] = '\0';
    }
}

static void t_str(struct text *t, const UBYTE *s, BOOL marks)
{
    (void)marks;
    while (*s)
        t_chr(t, (char)*s++);
}

#ifdef AMIGENT_APOLLO
static void t_hex(struct text *t, ULONG v)
{
    int i;

    for (i = 28; i >= 0; i -= 4)
        t_chr(t, "0123456789abcdef"[(v >> i) & 15]);
}
#endif

static void t_pair(struct text *t, const char *key, ULONG v)
{
    char d[16];
    int n = 0;

    do
        d[n++] = (char)('0' + (v % 10));
    while ((v /= 10) != 0);
    t_str(t, (const UBYTE *)key, FALSE);
    t_chr(t, '=');
    while (n)
        t_chr(t, d[--n]);
    t_chr(t, '\n');
}

int main(int argc, char **argv)
{
    struct text t;

    if (argc != 5) {
        fprintf(stderr, "usage: %s <exec_version> <attn_flags> <board_id> <emu68>\n", argv[0]);
        return 2;
    }

    memset(&t, 0, sizeof(t));
    fake_exec.LibNode.lib_Version = (UWORD)strtoul(argv[1], NULL, 0);
    fake_exec.AttnFlags = (UWORD)strtoul(argv[2], NULL, 0);
    mock_board_id = (UWORD)strtoul(argv[3], NULL, 0);
    mock_emu68 = (int)strtol(argv[4], NULL, 0);
    board_calls = 0;

    /* Verbatim CPU/Apollo/AUDIO block extracted from c_sysinfo() in amigentd.c */
#include "sysinfo_apollo_extract.inc"

    /* Verbatim EMU68 capability line extracted from c_sysinfo() in amigentd.c */
#include "sysinfo_emu68_extract.inc"

    fputs(t.buf, stdout);
    printf("board_calls=%d\n", board_calls);
    return 0;
}
