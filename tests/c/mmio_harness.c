/* Calls the real apollo_mmio_hit. The allow command is a simulation of the
 * early part of peek_allowed: invalid lengths first, then the register
 * guard, and only then a MemHeader that claims to cover the range.
 * Nothing here reads or writes a hardware address. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int apollo_mmio_hit(unsigned int at, unsigned int n);

static int simulated_allowed(unsigned int at, unsigned int n, int covered)
{
    unsigned int end;

    if (!n || n > 65536u)
        return 0;
    end = at + n - 1u;
    if (end < at)
        return 0;
    if (apollo_mmio_hit(at, n))
        return 0;
    if (at >= 0x00F80000u && end <= 0x00FFFFFFu)
        return 1;
    if (end < 0x400u)
        return 1;
    return covered ? 1 : 0;
}

int main(int argc, char **argv)
{
    unsigned long at, n;

    if (argc == 4 && strcmp(argv[1], "hit") == 0) {
        at = strtoul(argv[2], NULL, 0);
        n = strtoul(argv[3], NULL, 0);
        printf("%d\n", apollo_mmio_hit((unsigned int)at, (unsigned int)n));
        return 0;
    }
    if (argc == 5 && strcmp(argv[1], "allow") == 0) {
        at = strtoul(argv[2], NULL, 0);
        n = strtoul(argv[3], NULL, 0);
        printf("%d\n", simulated_allowed((unsigned int)at, (unsigned int)n, atoi(argv[4])));
        return 0;
    }
    return 2;
}
