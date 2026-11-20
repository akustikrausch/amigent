/* Which machine this is. Shared by amigentd.apollo and the setup probe.
 * 68000-clean: the probe runs before the Apollo binary is chosen. */
#include <exec/types.h>
#include <exec/execbase.h>
#include <proto/exec.h>

#ifndef AFF_FPGA
#define AFF_FPGA (1L << 10)
#endif

#define VREG_BOARD 0xDFF3FC

extern struct ExecBase *SysBase;

UWORD apollo_board(void)
{
    return *(volatile UWORD *)VREG_BOARD;
}

const char *apollo_board_name(UWORD board)
{
    /* High byte is the model, from the ApolloOS SAGA header. Ids past this
     * list (an A6000, for one) are reported by the raw value, not guessed. */
    static const char *const names[] = {
        "unknown", "V2-V600", "V2-V500", "V4-V500", "V4-V1200",
        "V4-SA", "V2-V1200", "V4-V600", "V4-UNI", "future"
    };
    UBYTE model = board >> 8;

    if (model < sizeof(names) / sizeof(names[0]))
        return names[model];
    return "unlisted";
}

static BOOL contains(const char *s, const char *needle)
{
    LONG i, j, n;

    if (!s || !needle)
        return FALSE;
    for (n = 0; needle[n]; n++)
        ;
    for (i = 0; s[i] && i < 160; i++) {
        for (j = 0; j < n && s[i + j] == needle[j]; j++)
            ;
        if (j == n)
            return TRUE;
    }
    return FALSE;
}

BOOL apollo_os_here(void)
{
    const char *id = (const char *)SysBase->LibNode.lib_IdString;
    UBYTE model;

    if (contains(id, "ApolloOS"))
        return TRUE;
    /* ApolloOS in the public tree is exec 51. AmigaOS on a Vampire stays
     * below that. AFF_FPGA, or a board id the SAGA header lists, keeps a
     * generic AROS build on exec 51 from taking the 68080 binary. */
    if (SysBase->LibNode.lib_Version < 51)
        return FALSE;
    if (SysBase->AttnFlags & AFF_FPGA)
        return TRUE;
    model = apollo_board() >> 8;
    return model >= 1 && model <= 9;
}
