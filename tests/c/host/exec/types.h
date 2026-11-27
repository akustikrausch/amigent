/* Host-only stubs so apollo_mmio.c can be compiled by the PC/gcc harness.
 * Widths match Amiga exec/types.h (32-bit ULONG), including on LP64 hosts.
 * Not Amiga NDK headers; not used by ApolloCrossDev builds. */
#ifndef AMIGENT_HOST_EXEC_TYPES_H
#define AMIGENT_HOST_EXEC_TYPES_H

#include <stdint.h>

typedef uint8_t UBYTE;
typedef uint16_t UWORD;
typedef uint32_t ULONG;
typedef int32_t LONG;
typedef int BOOL;

#ifndef TRUE
#define TRUE 1
#endif
#ifndef FALSE
#define FALSE 0
#endif

#endif
