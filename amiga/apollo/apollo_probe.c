/* Run from amigent-setup. Prints ApolloOS or AmigaOS; the exit code is 0 or 5.
 * Reads the board register and exec's id. Does not use 68080 instructions. */
#include <proto/dos.h>

BOOL apollo_os_here(void);

int main(void)
{
    if (apollo_os_here()) {
        PutStr("ApolloOS\n");
        return RETURN_OK;
    }
    PutStr("AmigaOS\n");
    return RETURN_WARN;
}
