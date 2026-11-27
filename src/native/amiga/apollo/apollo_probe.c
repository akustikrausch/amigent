/* Run from amigent-setup. Prints the OS, then 68080 when the Apollo core bit
 * is set. Exit 0 only when ApolloOS and that bit are both present; exit 5
 * otherwise. Does not use 68080 instructions. */
#include <exec/execbase.h>
#include <proto/dos.h>
#include <proto/exec.h>

#ifndef AFF_FPGA
#define AFF_FPGA (1L << 10)
#endif

BOOL apollo_os_here(void);

extern struct ExecBase *SysBase;

static void say_core(void)
{
    /* AFF_FPGA is the Apollo NDK attention bit for the Vampire/V4 68080 core. */
    if (SysBase->AttnFlags & AFF_FPGA)
        PutStr("68080\n");
}

int main(void)
{
    if (apollo_os_here()) {
        PutStr("ApolloOS\n");
        say_core();
        /* Exit 0 only with both ApolloOS and the FPGA core bit, so SETUP (which
         * sees only the exit code) does not treat the OS name alone as a 68080. */
        if (SysBase->AttnFlags & AFF_FPGA)
            return RETURN_OK;
        return RETURN_WARN;
    }
    PutStr("AmigaOS\n");
    say_core();
    return RETURN_WARN;
}
