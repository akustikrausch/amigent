/* Host stand-in for the Amiga types apollo_mmio.c includes.
 * ULONG stays 32 bits, so the overflow test matches the 68000. */
typedef unsigned int ULONG;
typedef int BOOL;
#define TRUE 1
#define FALSE 0
